"""
tagger_engine.py — D-liner Tagger Backend 管理
===============================================
セッション8 アーキテクチャ:
  D-liner → TaggerEngine → ITaggerBackend → 推論実行

セッション9 で追加:
  StandaloneWD14Backend → StandaloneTaggerBackend に一般化。
  SessionEntry による Lazy Load + LRU 管理を導入（_MyEXT_ComfyUI_Tagger_Worker
  の知見を反映: npu_tagger_memory_report.md 参照）。

  現状方針:
    - 合議制（複数モデル同時ロード）は採用しない。
      ComfyUI worker 側の計測で、joytag+camie+anima の3モデル合議制が
      3,676 MB（NPU・ov_cache有効時）に達することが確認されており、
      D-liner の軽量ビューワー用途には過剰と判断。
    - LRU 上限は 1（1モデルずつ切り替え）。MAX_LOADED_MODELS で変更可能。
    - モデルを切り替えると既存のタグ付け結果との一貫性が崩れるため、
      TaggerEngine.pending_model_switch_warning で検出フラグを公開する。
      実際にユーザーへダイアログ表示するかは main_window.py 側の判断に委ねる
      （タグ付けをやり直すかどうかはユーザー判断）。

  既知の未解決課題（このセッションでは対応しない。引き継ぎ事項として記録）:
    - セッション7で観測された「ComfyUI piggyback 時に D-liner 側が +3GB
      消費する」問題について、本セッションの standalone 検証では
      WD14 単体で CPU=587MB / NPU=870MB（最大瞬間 ~1.4GB）と
      正常範囲に収まることを確認した。
      一方 ComfyUI worker 側ログでは合議制3モデルで 3,676MB に達しており、
      これは worker 自体の設計上の重さであり D-liner のバグではない
      可能性が高いと判明した。
      ただし「piggyback 接続時に D-liner が worker に相乗りするつもりが、
      実際には別途自前でモデルをロードしていた」という、当初疑われていた
      二重ロードの可能性そのものは本セッションでは検証できていない
      （ComfyWorkerBackend が凍結中のため）。
      ComfyWorkerBackend 再実装時（Phase 3）に、D-liner 側が本当に
      worker のセッションへ相乗りできているか（新規セッションを
      自前で作っていないか）を別途確認すること。

現在有効なバックエンド:
  StandaloneTaggerBackend … onnxruntime でタガーモデルを直接実行
    実装済みモデル: wd14
    将来実装予定:   camie, anima, joytag（UI 選択肢には既に存在）

修正済み（実行用venv移行時に発覚）:
  add_openvino_libs_to_path() が docstring 上の説明のみで実際には
  どこからも呼ばれておらず、NPU/GPU 要求時に常に CPUExecutionProvider へ
  サイレントフォールバックしていた。モジュール読み込み時
  （onnxruntime インポート前）に実際に呼び出すよう修正した。
  worker.py（ComfyUI 側）と同じパターン。

【凍結中 — 削除不可】
以下のコードは ComfyUI piggyback / standalone worker.py 連携に関するもの。
3GB問題の原因切り分けのため、セッション8で動作経路から除外している。
将来の再統合時に ComfyWorkerBackend として ITaggerBackend を実装する計画。
  - _find_comfyui_pid_file()
  - _find_worker_script()
  - _find_venv_python()
  - _launch_worker()
  - _try_upgrade_to_piggyback()
  - _try_fallback_to_standalone()
  - check_and_rebalance()  （main_window のタイマーも停止済み）
"""

from __future__ import annotations

import csv
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# OpenVINO DLL パス解決（onnxruntime インポート前に実行する必要がある）
# ---------------------------------------------------------------------------
# onnxruntime-openvino の Windows wheel は openvino.dll 等のランタイム本体を
# 同梱しないため、add_openvino_libs_to_path() を呼んで DLL 検索パスに
# OpenVINO ランタイムを追加しておく必要がある。
# worker.py（ComfyUI 側）と同じパターンをここでも踏襲する。
#
# これを呼ばないと、OpenVINOExecutionProvider を要求しても例外を投げずに
# 黙って CPUExecutionProvider にフォールバックすることがある
# （_try_openvino_session() 内のログはあくまで「フォールバックしたことの検出」
#  であり、この呼び出し自体の代わりにはならない）。
#
# 以前このモジュールには「D-liner 起動時に1回呼んでおく必要がある」という
# docstring のみが残っており、実際の呼び出し箇所が存在しなかった
# （NPU モードが常に CPU へフォールバックしていた根本原因）。ここで実際に呼ぶ。
#
# セッション18 追記: onnxruntime-openvino が入っていない環境（directml系統
# のvenv等）では、このブロック自体を完全にスキップする。以前は
# 「import に失敗したら諦める」形だったが、import自体は（onnxruntimeの
# tools サブパッケージが変種間で共通のため）成功してしまい、その上で
# add_openvino_libs_to_path() が実行される可能性があった。検証機で
# DirectMLが正しくインストールされているにも関わらずアプリ内では
# 認識されない不具合があり、原因の一つとしてこの呼び出しがDLL検索パス
# （PATH）に何らかの干渉を与えている可能性を排除しきれないため、
# 実際に onnxruntime-openvino がインストールされている場合のみ実行する
# よう明示的にガードする。
_has_onnxruntime_openvino = False
if sys.platform == "win32":
    try:
        import importlib.metadata as _ilm
        _ilm.version("onnxruntime-openvino")
        _has_onnxruntime_openvino = True
    except Exception:
        _has_onnxruntime_openvino = False

if sys.platform == "win32" and _has_onnxruntime_openvino:
    try:
        import onnxruntime.tools.add_openvino_win_libs as _ov_libs
        _ov_libs.add_openvino_libs_to_path()
    except Exception:
        # onnxruntime-openvino が入っているはずなのにこのヘルパーが
        # 見つからない/失敗した場合。CPU実行には影響しないため静かに無視する。
        # NPU/GPU が必要な環境で失敗している場合は
        # _try_openvino_session() 内のフォールバック検出ログで気付ける。
        pass


def is_npu_capable() -> bool:
    """
    このvenvでNPU推論が原理的に成立しうるかどうかを返す。

    D-linerのNPU推論はOpenVINO ExecutionProvider経由でのみ行われる
    （tagger_engine.py _create_session() 参照）。onnxruntime-directml系統
    でセットアップされたvenv（NPU非搭載機向け、setup_runtime_env.py参照）
    には onnxruntime-openvino が入っておらず、NPUを選んでも常に
    フォールバックするだけの意味の無い選択肢になる。
    main_window.py のタグ付け設定ダイアログで、この関数がFalseを返す
    環境ではデバイス選択肢からNPUそのものを除外する。
    """
    return _has_onnxruntime_openvino


# ---------------------------------------------------------------------------
# サブプロセス出力デコード（Windows対策）
# ---------------------------------------------------------------------------
def _decode_subprocess_bytes(data: bytes) -> str:
    """
    サブプロセス出力のデコード。

    バグ修正: 従来 subprocess.run(..., text=True) はencoding未指定のため
    Windows既定ロケール（日本語環境ではcp932）でデコードされていた。
    子プロセス（PowerShell / model_downloader.py）側の出力エンコーディング
    との不一致でUnicodeDecodeErrorや文字化けが起こり得る。
    setup_runtime_env.py の _decode_subprocess_bytes と同一方針（ロジックを
    変更する場合は両方を同期させること）: まずUTF-8を試し、失敗したら
    cp932にフォールバックする。
    """
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("cp932")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# GPU 列挙ユーティリティ
# ---------------------------------------------------------------------------
def get_gpu_names() -> list[str]:
    """Windows PowerShell経由でインストールされているビデオカード（GPU）の名前リストを返す。"""
    import subprocess
    import json
    import re
    if sys.platform != "win32":
        return []
    try:
        # CREATE_NO_WINDOW = 0x08000000 を指定して黒いコンソールウインドウの発生を防ぐ
        cmd = ["powershell", "-NoProfile", "-Command", 
               "Get-CimInstance Win32_VideoController | Select-Object Name, CurrentHorizontalResolution, DeviceID | ConvertTo-Json"]
        res = subprocess.run(cmd, capture_output=True, check=True, creationflags=0x08000000)

        stdout = _decode_subprocess_bytes(res.stdout).strip()
        if not stdout:
            return []
            
        data = json.loads(stdout)
        if isinstance(data, dict):
            data = [data]
            
        # DXGI (onnxruntime) が認識する優先順位（ディスプレイ出力を持つプライマリGPUが先頭、以降はDeviceIDのハードウェア順）に一致させるためのソートキー
        def sort_key(item):
            res_val = item.get("CurrentHorizontalResolution")
            has_res = res_val is not None and str(res_val).strip() != "" and str(res_val).strip() != "0"
            dev_id_str = item.get("DeviceID", "VideoController99")
            num_match = re.search(r'\d+', dev_id_str)
            dev_num = int(num_match.group()) if num_match else 99
            return (0 if has_res else 1, dev_num)
            
        data.sort(key=sort_key)
        return [item["Name"] for item in data if item.get("Name")]
    except Exception as e:
        print(f"[TaggerEngine] GPU 列挙エラー: {e}", flush=True)
        return []


_gpu_names_cache: list[str] | None = None


def _get_gpu_names_cached() -> list[str]:
    """
    get_gpu_names() の結果をプロセス内でキャッシュして返す。

    セッション18 追記（重大な性能退行の修正）: _get_or_load_session() が
    （既存セッションを使い回す場合も含め）tag() 呼び出しのたびに
    get_gpu_names() を無条件で呼んでいたため、画像1枚タグ付けするごとに
    PowerShellプロセスが起動/終了を繰り返し、タグ付け速度低下と
    powershell.exe/conhost.exeの無駄なプロセス起動を招いていた
    （実機のProcess Explorerで確認済み）。
    GPU構成は実行中に変化しないため、プロセス内で一度だけ取得して
    キャッシュする。
    """
    global _gpu_names_cache
    if _gpu_names_cache is None:
        _gpu_names_cache = get_gpu_names()
    return _gpu_names_cache


# ---------------------------------------------------------------------------
# 定数（凍結中の旧エンジン用 — 参照のみ保持）
# ---------------------------------------------------------------------------

_PID_FILE_CANDIDATES = ["worker.pid.json"]
_WORKER_SCRIPT_NAME  = "worker.py"
_CONNECT_TIMEOUT     = 2.0
_RECV_TIMEOUT        = 120.0


# ---------------------------------------------------------------------------
# Lazy Load / LRU 設定
# ---------------------------------------------------------------------------
# _MyEXT_ComfyUI_Tagger_Worker の改善知見（npu_tagger_memory_report.md）を
# D-liner 向けに移植したもの。

# 同時ロード可能なモデル数の上限。
# D-liner は合議制を採用しないため既定値は 1（1モデルずつ切り替え）。
# 将来的に合議制を検討する場合のみ増やすこと。
MAX_LOADED_MODELS = 1

# この秒数以上未使用のセッションをバックグラウンドで解放する。
# セッション10: 「何もしていないタガーエンジンを30分待機させる理由がない」
# との判断により、ComfyUI worker 由来の30分から大幅短縮。
# タグ付けキューが空になってから約60秒でモデルセッション（NPU/GPU/CPU
# 推論用メモリ）を解放する。main_window.py 側はこれとは別に、
# タグ付けキューが空になった瞬間に即座に60秒の単発タイマーを仕掛けて
# release_idle_sessions() を呼ぶイベント駆動方式を併用する
# （_schedule_tagger_idle_release() 参照）。このポーリング値は
# その仕組みが働かなかった場合の保険として機能する。
IDLE_TIMEOUT_SECONDS = 60

# Idle チェックの実行間隔（秒）。IDLE_TIMEOUT_SECONDS を大幅短縮したため
# ポーリング間隔もあわせて短縮（5分のままだと60秒アイドルの検出が
# 最大5分遅れてしまう）。
IDLE_CHECK_INTERVAL_SECONDS = 20


@dataclass
class SessionEntry:
    """
    ロード済みタガーモデルの状態を保持する。
    _MyEXT_ComfyUI_Tagger_Worker の SessionEntry と同一設計。

    last_used は推論実行のたびに touch() で更新し、
    Idle Unload の判定に使う。
    """
    session:    object          # ort.InferenceSession
    tags:       list[dict]      # [{name, category}, ...]
    model_id:   str             # "wd14", "camie", "anima", "joytag" など
    device:     str             # "NPU", "GPU", "CPU"
    actual_ep:  str             # 実際に使われた ExecutionProvider 名
    loaded_at:  float = field(default_factory=time.time)
    last_used:  float = field(default_factory=time.time)

    def touch(self) -> None:
        """最終使用時刻を更新する。推論実行のたびに呼ぶこと。"""
        self.last_used = time.time()

    def idle_seconds(self) -> float:
        return time.time() - self.last_used


# ===========================================================================
# ITaggerBackend — 抽象インターフェース
# ===========================================================================

class ITaggerBackend:
    """
    タガーバックエンドの抽象基底クラス。

    将来追加予定:
      ComfyWorkerBackend … 現在凍結中の worker.py 連携を再実装
    """

    def tag(
        self,
        image_path: str,
        model: str = "wd14",
        device: str = "CPU",
        threshold: float = 0.35,
        threshold_character: float = 0.75,
        threshold_copyright: float = 0.50,
        replace_underscores: bool = False,
        **kwargs,
    ) -> dict | None:
        """
        単画像をタグ付けして結果辞書を返す。失敗時は None。

        返却形式（TaggerEngine の既存呼び出し形式に合わせる）:
        {
            "status":         "ok",
            "general_tags":   "tag1, tag2, ...",
            "character_tags": "char1, ...",
            "copyright_tags": "copy1, ...",
            "artist_tags":    "",
            "rating_tags":    "safe",
            "meta_tags":      "",
        }
        """
        raise NotImplementedError

    def is_available(self) -> bool:
        """バックエンドが使用可能な状態か確認する。"""
        raise NotImplementedError

    def shutdown(self) -> None:
        """リソースを解放する。"""
        pass


# ===========================================================================
# StandaloneTaggerBackend
# ===========================================================================
# セッション9: StandaloneWD14Backend を一般化し、SessionEntry による
# Lazy Load + LRU 管理を導入。WD14 以外のモデル（camie/anima/joytag）は
# UI 選択肢には存在するが未実装。要求されると明確なエラーを返す。

class StandaloneTaggerBackend(ITaggerBackend):
    """
    onnxruntime でタガーモデルを直接実行するバックエンド。
    外部プロセス・Socket 通信は一切不使用。

    Lazy Load: モデルは初回 tag() 呼び出し時に初めてロードする。
               TaggerEngine.connect_or_launch() の時点ではロードしない。
    LRU:       MAX_LOADED_MODELS（既定 1）を超えたら最長未使用モデルを解放する。
               D-liner は合議制を採用しないため既定では実質「1モデルだけ保持」。
    Idle Unload: IDLE_TIMEOUT_SECONDS 以上未使用のセッションを解放できるよう
               release_idle_sessions() を公開する。呼び出しは TaggerEngine /
               main_window.py 側のタイマーに委ねる（このクラス自身はスレッドを
               起動しない＝D-liner の既存スレッド管理方針に合わせる）。

    モデル探索順（model_id ごとに同じパターンを適用）:
      1. QSettings["tagger/model_path"] で明示指定されたパス（wd14 のみ。
         複数モデル運用時は per-model キーへの拡張が必要 — 未実装）
      2. {d_liner_dir}/models/{model_id}/model.onnx
      3. ComfyUI カスタムノード以下の同名モデル（間借り）
    """

    # Danbooru カテゴリ番号 → D-liner カテゴリ名（全モデル共通フォーマットと仮定）
    _CATEGORY_MAP = {
        0: "general",
        1: "artist",
        3: "copyright",
        4: "character",
        9: "meta",
    }

    _RATING_TAGS = {"rating:general", "rating:sensitive", "rating:questionable", "rating:explicit",
                    "safe", "sensitive", "questionable", "explicit"}

    # 実装済みモデル一覧。
    # "implemented": False のモデルは UI 選択肢には出るが tag() 時にエラーを返す。
    #
    # 注: anima（pixai-tagger）はモノクロ画像でハルシネーションを起こす既知の
    # 不具合があり、合議制（他モデルとの突き合わせ）を前提とした運用でのみ
    # 安全に使えるモデルである。D-liner は合議制を採用しない方針のため、
    # 単独タグ付けの選択肢からは意図的に除外している。
    # 将来合議制を検討する場合のみ再検討すること。
    #
    # セッション10: camie / joytag を実装。wd14 は版権キャラクターに弱い
    # （学習データが古い）ため、camie / joytag への切り替えを可能にする。
    # 前処理・出力仕様は worker.py（_MyEXT_ComfyUI_Tagger_Worker）の
    # preprocess_camie() / preprocess_joytag() / tag アクション実行部を移植。
    #
    # フィールド説明:
    #   input_size    前処理でリサイズする一辺のピクセル数
    #   layout        "NHWC"（wd14）または "NCHW"（camie/joytag）
    #   color         "rgb" または "bgr"（wd14 のみ BGR 変換が必要）
    #   normalize     "none"（[0,255]のまま） / "scale01"（[0,1]） /
    #                 "imagenet"（[0,1] 後に ImageNet mean/std 正規化）
    #   output_name   ONNX出力テンソル名を明示指定する場合に設定
    #                 （camie の refined_predictions 選択用）。None なら outputs[0]。
    #   apply_sigmoid 出力が生のロジットで sigmoid が必要な場合 True
    #                 （camie の refined_predictions はロジット出力のため必須）
    #   tags_filename タグ定義ファイル名（モデルごとにフォーマットが異なる）
    #   download      自動ダウンロード元（HuggingFace）。None なら自動DL非対応
    #                 （wd14 は wd-eva02-large-tagger-v3 のみ自動DL対応。
    #                 他のwd14系リポジトリ〈convnextv2/swinv2/vit等〉は非対応）
    _SUPPORTED_MODELS: dict[str, dict] = {
        "wd14": {
            "implemented": True,
            "input_size":  448,
            "layout":      "NHWC",
            "color":       "bgr",
            "normalize":   "none",
            "output_name": None,
            "apply_sigmoid": False,
            "tags_filename": "selected_tags.csv",
            # wd-eva02-large-tagger-v3 のみ対応。ONNXモデルはv2系向けに
            # 書かれた前処理コードと互換（モデルカード・公式Space app.py の
            # preprocess_image が v2/v3 共通であることで確認済み）のため、
            # 上記の NHWC / BGR / normalize:none をそのまま流用できる。
            "download": {
                "repo_id":        "SmilingWolf/wd-eva02-large-tagger-v3",
                "model_filename": "model.onnx",
                "tags_filename":  "selected_tags.csv",
            },
        },
        "camie": {
            "implemented": True,
            "input_size":  512,     # image_sorter.py の知見: Camie 512×512
            "layout":      "NCHW",
            "color":       "rgb",
            "normalize":   "imagenet",
            "output_name": "refined_predictions",
            "apply_sigmoid": True,
            "tags_filename": "camie-tagger-v2-metadata.json",
            "download": {
                "repo_id":        "Camais03/camie-tagger-v2",
                "model_filename": "camie-tagger-v2.onnx",
                "tags_filename":  "camie-tagger-v2-metadata.json",
            },
        },
        "joytag": {
            "implemented": True,
            "input_size":  448,
            "layout":      "NCHW",
            "color":       "rgb",
            "normalize":   "scale01",
            "output_name": None,
            "apply_sigmoid": False,
            "tags_filename": "top_tags.txt",
            "download": {
                "repo_id":        "fancyfeast/joytag",
                "model_filename": "model.onnx",
                "tags_filename":  "top_tags.txt",
            },
        },
    }

    def __init__(self, settings=None) -> None:
        self._settings = settings
        # model_id → SessionEntry。Lazy Load されたモデルのみ存在する。
        self._sessions: dict[str, SessionEntry] = {}
        self._available: bool = False
        # 最後に tag() で実際に使用した model_id。
        # モデル切り替え検出（一貫性警告）に使う。
        self._last_used_model_id: str | None = None
        # モデルが切り替わったことを TaggerEngine 側へ伝えるフラグ。
        # consume_model_switch_warning() で取得すると同時にクリアされる。
        self._pending_switch_warning: tuple[str, str] | None = None  # (旧model, 新model)

        # セッション11 追加: ダウンロードに失敗した model_id を記録する。
        # 以前はここが無く、フォルダ一括タグ付け中に毎画像ごと同じ失敗する
        # ダウンロードを再試行していた（ネットワーク不通時に非常に遅い/
        # 応答不能に見える原因のひとつ）。一度失敗したら接続が生きている間は
        # 再試行しない（ユーザーが再接続する、またはアプリを再起動すれば
        # クリアされる）。
        self._failed_downloads: set[str] = set()

    # ------------------------------------------------------------------
    # ITaggerBackend API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """
        バックエンド自体が機能する状態か（= onnxruntime が import できるか、
        実装済みモデルが最低1つ存在するか）の軽量チェック。
        個別モデルがロード済みかどうかは問わない（Lazy Load のため）。
        """
        return self._available

    def tag(
        self,
        image_path: str,
        model: str = "wd14",
        device: str = "CPU",
        threshold: float = 0.35,
        threshold_character: float = 0.75,
        threshold_copyright: float = 0.50,
        replace_underscores: bool = False,
        **kwargs,
    ) -> dict | None:
        if not self._available:
            return None

        model_id = model.lower().strip()
        model_spec = self._SUPPORTED_MODELS.get(model_id)

        if model_spec is None:
            print(f"[StandaloneTaggerBackend] 未知のモデル: {model_id}", flush=True)
            return {"status": "error", "message": f"unknown model: {model_id}"}

        if not model_spec["implemented"]:
            print(f"[StandaloneTaggerBackend] {model_id} は未実装です。", flush=True)
            return {"status": "error",
                    "message": f"{model_id} backend not implemented yet"}

        # --- モデル切り替え検出 ---
        # 前回 tag() で使ったモデルと異なる場合、既存のタグ付け結果との
        # 一貫性が崩れる可能性があるため警告フラグを立てる。
        # 実際にユーザーへダイアログ表示するかは main_window.py 側の判断。
        if self._last_used_model_id is not None and self._last_used_model_id != model_id:
            self._pending_switch_warning = (self._last_used_model_id, model_id)
            print(f"[StandaloneTaggerBackend] モデル切り替え検出: "
                  f"{self._last_used_model_id} → {model_id}", flush=True)
        self._last_used_model_id = model_id

        # --- Lazy Load: セッション取得（未ロードならここで初めてロード） ---
        entry = self._get_or_load_session(model_id, device)
        if entry is None:
            return None
        entry.touch()

        try:
            from PIL import Image, UnidentifiedImageError
            import numpy as np

            # バグ修正: PillowはデフォルトでMAX_IMAGE_PIXELS(≈1.79億px)を
            # 超える画像を「デコンプレッションボムの可能性あり」として
            # 例外にする。これは外部から受け取る未信頼画像を想定した安全
            # 装置だが、D-linerはユーザー自身のローカルフォルダを対象と
            # するため、高解像度スキャン画像やイラストが普通に該当し
            # うる。誤爆すると tag() が None を返し、呼び出し側
            # (BackgroundTaggerWorker) では「サーバ無応答」と区別できず、
            # 5件連続で誤爆すると「接続断」とみなして未タグ付け画像全体の
            # 処理を打ち切ってしまっていた。ローカル信頼画像である前提で
            # 上限チェックを無効化する。
            Image.MAX_IMAGE_PIXELS = None

            img = Image.open(image_path).convert("RGB")
            inp = self._preprocess(img, model_spec)

            input_name = entry.session.get_inputs()[0].name

            # --- 推論実行 ---
            # camie は refined_predictions を名指しで取り出す必要がある
            # （初期予測 initial_predictions と2出力あるため）。
            # wd14 / joytag は outputs[0] をそのまま使う。
            output_name = model_spec.get("output_name")
            if output_name:
                available_outputs = [o.name for o in entry.session.get_outputs()]
                chosen = output_name if output_name in available_outputs else available_outputs[0]
                raw = entry.session.run([chosen], {input_name: inp})[0][0]
            else:
                raw = entry.session.run(None, {input_name: inp})[0][0]

            # camie の出力は生のロジットのため sigmoid で確率に変換する必要がある
            # （wd14 / joytag の ONNX は sigmoid 適用済みの確率をそのまま出力する）。
            if model_spec.get("apply_sigmoid"):
                scores = 1.0 / (1.0 + np.exp(-np.clip(raw, -88, 88)))
            else:
                scores = raw

            # --- カテゴリ別しきい値判定 ---
            # entry.tags は全モデル共通で [{"name": str, "category": str}, ...] に
            # 正規化済み（category は "general"/"character"/"copyright"/"artist"/
            # "meta"/"rating"/"year" のいずれか。joytag はカテゴリ情報を持たない
            # ため全タグ "general" 扱いになる）。
            thresholds = {
                "general":   threshold,
                "character": threshold_character,
                "copyright": threshold_copyright,
                "artist":    threshold,
                "meta":      threshold,
                "year":      threshold,
            }
            categorized: dict[str, list[tuple[str, float]]] = {c: [] for c in thresholds}
            rating_candidates: list[tuple[str, float]] = []

            for i, score in enumerate(scores):
                if i >= len(entry.tags):
                    break
                tag_info = entry.tags[i]
                name = tag_info["name"]
                if not name:
                    continue
                cat = tag_info["category"]

                # rating はしきい値判定せず、最高スコアのタグを採用する
                # （wd14 の従来仕様を全モデル共通で踏襲）
                if cat == "rating":
                    if score > 0.0:
                        rating_candidates.append((name, float(score)))
                    continue

                if cat not in categorized:
                    cat = "general"

                if score < thresholds[cat]:
                    continue

                tag_name = name.replace("_", " ") if replace_underscores else name
                categorized[cat].append((tag_name, float(score)))

            rating_str = ""
            if rating_candidates:
                rating_str = max(rating_candidates, key=lambda x: x[1])[0]
                if replace_underscores:
                    rating_str = rating_str.replace("_", " ")

            def _joined(cat: str) -> str:
                items = sorted(categorized[cat], key=lambda x: x[1], reverse=True)
                return ", ".join(t for t, _ in items)

            return {
                "status":         "ok",
                "general_tags":   _joined("general"),
                "character_tags": _joined("character"),
                "copyright_tags": _joined("copyright"),
                "artist_tags":    _joined("artist"),
                "rating_tags":    rating_str,
                "meta_tags":      _joined("meta"),
            }

        except (UnidentifiedImageError, OSError) as e:
            # バグ修正: 破損ファイル・非対応フォーマット等、その画像固有の
            # 恒久的なエラー（リトライしても直らない）。呼び出し側が
            # 「サーバ無応答／接続断」と誤認しないよう区別可能な
            # error_type を返す。
            print(f"[StandaloneTaggerBackend] tag() image error: {e}", flush=True)
            return {"status": "error", "error_type": "image_error", "message": str(e)}
        except Exception as e:
            print(f"[StandaloneTaggerBackend] tag() error: {e}", flush=True)
            return None

    def shutdown(self) -> None:
        for model_id in list(self._sessions.keys()):
            self._unload_session(model_id)
        self._available = False
        print("[StandaloneTaggerBackend] shutdown complete.", flush=True)

    # ------------------------------------------------------------------
    # モデル切り替え警告（一貫性チェック）
    # ------------------------------------------------------------------

    def consume_model_switch_warning(self) -> tuple[str, str] | None:
        """
        保留中のモデル切り替え警告を取得し、同時にクリアする。
        (旧 model_id, 新 model_id) のタプル、なければ None。

        main_window.py 側はこれをポーリングし、None でなければ
        ユーザーにダイアログ表示すること（タグ付けをやり直すかは
        ユーザー判断に委ねる。このメソッドは検出のみ行う）。
        """
        w = self._pending_switch_warning
        self._pending_switch_warning = None
        return w

    # ------------------------------------------------------------------
    # 初期化（バックグラウンドスレッドから呼ぶ）
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        """
        バックエンドの利用可能性のみを確認する（Lazy Load のためモデルは
        ここではロードしない）。

        Returns:
            True:  onnxruntime が import でき、実装済みモデルが
                   最低1つ存在する（探索可能な状態にある）
            False: onnxruntime がない、または実装済みモデルが1つも
                   見つからない
        """
        try:
            import onnxruntime as ort  # noqa: F401
        except ImportError:
            print("[StandaloneTaggerBackend] onnxruntime がインストールされていません。", flush=True)
            print("  pip install onnxruntime  または  pip install onnxruntime-openvino", flush=True)
            return False

        # 実装済みモデルのうち、ファイルが実在する、または自動ダウンロード
        # 可能なものが1つでもあるか確認。
        # （実ロード・実DLはしない。あくまで「使える可能性があるか」のチェック。
        #   実際のダウンロードは初回 tag() 時の _get_or_load_session() で行う）
        found_any = False
        for model_id, spec in self._SUPPORTED_MODELS.items():
            if not spec["implemented"]:
                continue
            model_path = self._resolve_model_path(model_id)
            if model_path is not None and model_path.is_file():
                found_any = True
                print(f"[StandaloneTaggerBackend] {model_id}: {model_path}", flush=True)
            elif spec.get("download"):
                found_any = True
                print(f"[StandaloneTaggerBackend] {model_id}: 未ダウンロード "
                      f"(初回使用時に自動取得します)", flush=True)

        if not found_any:
            print("[StandaloneTaggerBackend] 実装済みモデルのファイルが見つかりません。", flush=True)
            self._print_model_search_hint("wd14")
            return False

        self._available = True
        print("[StandaloneTaggerBackend] Ready (Lazy Load — モデルは初回 tag() 時にロード).",
              flush=True)
        return True

    # ------------------------------------------------------------------
    # Lazy Load / LRU / Idle Unload
    # ------------------------------------------------------------------

    def _get_or_load_session(self, model_id: str, device: str) -> SessionEntry | None:
        """
        model_id のセッションを返す。ロード済みならキャッシュを、
        未ロードならここで初めてロードする（Lazy Load）。

        セッション10: model.onnx / タグ定義ファイルが見つからず、かつ
        _SUPPORTED_MODELS[model_id]["download"] が設定されている場合、
        HuggingFace から自動ダウンロードを試みる（_try_auto_download()）。
        """
        existing = self._sessions.get(model_id)
        gpu_device_id = 0
        if self._settings is not None:
            try:
                gpu_device_id = int(self._settings.value("tagger/gpu_device_id", 0))
            except (ValueError, TypeError):
                gpu_device_id = 0

        if existing is not None:
            existing_gpu_id = getattr(existing, "gpu_device_id", 0)
            if existing.device == device and existing_gpu_id == gpu_device_id:
                return existing
            else:
                print(f"[StandaloneTaggerBackend] デバイスまたはGPU ID変更検出: "
                      f"{existing.device}(ID:{existing_gpu_id}) → {device}(ID:{gpu_device_id}). "
                      f"旧セッションを解放します。", flush=True)
                self._unload_session(model_id)

        # セッション18 追記: OpenVINOのGPU EPはIntel GPU専用であり、
        # NVIDIA/AMDのGPUは認識できない。しかも「存在しないGPU番号」を
        # 要求した場合に明確なエラーを出さず、検出できたIntel GPU
        # （多くの場合iGPU）へ静かに読み替えてしまうケースがあることが
        # 実機（RTX3060選択時に一瞬動いた後Arc iGPUへ切り替わる）で判明した。
        # これを避けるため、選択GPUがIntel製かどうかを事前に判定し、
        # Intel製でなければ最初からOpenVINO GPUを試さずDirectMLへ直行する。
        # ここでの判定は実際にセッションを(再)作成する場合のみ行う
        # （キャッシュ済みセッションを使い回す上のearly returnより後ろに
        # 置くことで、tag()呼び出しのたびにPowerShellが起動する性能退行を
        # 避ける。GPU名一覧自体も _get_gpu_names_cached() でプロセス内
        # キャッシュ済み）。
        is_intel_gpu = True
        try:
            gpu_names = _get_gpu_names_cached()
            if 0 <= gpu_device_id < len(gpu_names):
                is_intel_gpu = "intel" in gpu_names[gpu_device_id].lower()
        except Exception:
            is_intel_gpu = True  # 判定できない場合は従来通りOpenVINOも試す

        # --- 初回ロード ---
        import onnxruntime as ort

        model_spec = self._SUPPORTED_MODELS[model_id]

        model_path = self._resolve_model_path(model_id)
        tags_path  = self._resolve_tags_path(model_id, model_path)

        need_download = (
            model_path is None or not model_path.is_file()
            or tags_path is None or not tags_path.is_file()
        )
        if need_download and model_spec.get("download"):
            # セッション11: 一度ダウンロードに失敗したモデルは、接続が生きている
            # 間は再試行しない。これが無いと BackgroundTaggerWorker が
            # フォルダ内の全画像に対して毎回同じダウンロードを再試行し、
            # ネットワーク不通時に処理が長時間止まって見える／固まって見える
            # 原因になっていた。
            if model_id in self._failed_downloads:
                print(f"[StandaloneTaggerBackend] {model_id}: 前回の自動ダウンロードが"
                      f"失敗しているため今回はスキップします（再接続で再試行できます）。",
                      flush=True)
                return None
            model_path, tags_path = self._try_auto_download(model_id, model_spec)
            if model_path is None or tags_path is None:
                self._failed_downloads.add(model_id)

        if model_path is None or not model_path.is_file():
            print(f"[StandaloneTaggerBackend] {model_id}: model.onnx が見つかりません。", flush=True)
            self._print_model_search_hint(model_id)
            return None
        if tags_path is None or not tags_path.is_file():
            print(f"[StandaloneTaggerBackend] {model_id}: タグ定義ファイルが見つかりません: "
                  f"{tags_path}", flush=True)
            self._print_model_search_hint(model_id)
            return None

        print(f"[StandaloneTaggerBackend] Loading model: {model_id} ({model_path})", flush=True)

        session = self._create_session(ort, model_path, device, gpu_device_id, is_intel_gpu)
        if session is None:
            return None

        tags = self._load_tags(model_id, tags_path)
        if tags is None:
            return None

        actual_ep = session.get_providers()[0] if session.get_providers() else "unknown"
        entry = SessionEntry(
            session=session, tags=tags, model_id=model_id,
            device=device, actual_ep=actual_ep,
        )
        entry.gpu_device_id = gpu_device_id

        # --- LRU 上限制御 ---
        # 新モデルを追加する前に、上限を超えるなら最長未使用モデルを解放する。
        # D-liner は合議制を採用しないため MAX_LOADED_MODELS の既定値は 1。
        # つまり通常は「新モデルをロードする前に既存の1モデルを解放する」動作になる。
        self._evict_lru_if_needed(keep_room_for=1)

        self._sessions[model_id] = entry
        print(f"[StandaloneTaggerBackend] {model_id} Ready. {len(tags)} tags loaded. "
              f"EP={actual_ep}", flush=True)
        return entry

    def _try_auto_download(self, model_id: str, model_spec: dict) -> tuple[Path | None, Path | None]:
        """
        model.onnx / タグ定義ファイルが見つからない場合に HuggingFace から
        自動ダウンロードする。huggingface_hub が未インストールの環境では
        何もせず (None, None) 相当を返す（呼び出し元がエラーメッセージを出す）。

        ダウンロード先: {d_liner_dir}/models/{model_id}/
        model.onnx という統一ファイル名で保存する
        （_resolve_model_path() の探索パターンに合わせるため）。

        セッション10 追記: 当初 hf_hub_download(..., local_dir=...) を
        直接使っていたが、実機で自動ダウンロードが機能しない不具合が
        発覚した。huggingface_hub は local_dir 指定時にバージョンによって
        シンボリックリンクを作成しようとすることがあり、Windows では
        管理者権限または開発者モードが無いとシンボリックリンク作成が
        失敗する（静かに例外化する）。
        _MyEXT_ComfyUI_Tagger_Worker/setup_venv.py の実績のある方式
        （local_dir を指定せず、通常の HF キャッシュにダウンロードしてから
        shutil.copy() で明示コピーする）に合わせて修正した。

        セッション18 追記（検証機での重大不具合対応）:
        検証機(Ryzen3 4300U)で自動ダウンロードを試みると、以下2種類の
        問題が確認されていた。
          (a) hf_hub_download() のボディ転送自体にはタイムアウトが無く、
              一部のネットワーク環境で無期限にハングする。
          (b) コンソール表示ありのデバッグ起動ですら、ダウンロード試行
              直後にメインウィンドウごとプロセスが落ちる現象が発生。
              Pythonの例外として拾えるものではなく、ネイティブレベルの
              クラッシュ（検証機固有のSSL/DLL環境等）である可能性が高い。

        過去に(a)へ ThreadPoolExecutor + shutdown(wait=False) でハード
        タイムアウトさせる修正を試みたが、一度は動作確認できたものの
        後日クラッシュが再発し原因未特定のままロールバックした経緯がある
        （同一プロセス内のスレッドでは、ネイティブクラッシュや長時間
        ブロックしたスレッドを完全には無害化できないため）。

        今回はダウンロード処理そのものを model_downloader.py の別プロセス
        に切り出す方式に変更する。
          - (a)への対策: subprocess.run(..., timeout=N) はOSレベルで
            確実に子プロセスごと終了できる。
          - (b)への対策: 子プロセスがネイティブクラッシュしても、
            親プロセス（＝main_windowと同一プロセス）から見れば単なる
            「異常な returncode」でしかなく、GUIは道連れにならず
            継続動作できる。
        """
        dl = model_spec.get("download")
        if not dl:
            return None, None

        target_dir = Path(__file__).parent / "models" / model_id
        target_dir.mkdir(parents=True, exist_ok=True)
        model_path = target_dir / "model.onnx"
        tags_path  = target_dir / model_spec["tags_filename"]

        if model_path.is_file() and tags_path.is_file():
            return model_path, tags_path

        downloader_script = Path(__file__).parent / "model_downloader.py"
        if not downloader_script.is_file():
            print(f"[StandaloneTaggerBackend] {model_id}: model_downloader.py が"
                  f"見つからないため自動ダウンロードできません。", flush=True)
            print(f"  手動で以下に配置してください: {target_dir}", flush=True)
            return (model_path if model_path.is_file() else None,
                    tags_path if tags_path.is_file() else None)

        timeout_sec = float(os.environ.get("D_LINER_HF_DOWNLOAD_TIMEOUT", "300"))

        print(f"[StandaloneTaggerBackend] {model_id}: モデル/タグ定義ファイルの"
              f"自動ダウンロードを別プロセスで開始します "
              f"({dl['repo_id']})。サイズによっては数分かかります…", flush=True)

        cmd = [
            sys.executable,
            str(downloader_script),
            "--repo-id", dl["repo_id"],
            "--model-filename", dl["model_filename"],
            "--tags-filename", dl["tags_filename"],
            "--tags-dest-name", model_spec["tags_filename"],
            "--target-dir", str(target_dir),
        ]

        popen_kwargs: dict = {}
        if os.name == "nt":
            # pythonw.exe から起動している場合でも子プロセス起動時に
            # コンソールウィンドウが一瞬フラッシュすることがあるため抑制。
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        result_json: dict | None = None
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout_sec,
                **popen_kwargs,
            )
            proc_stdout = _decode_subprocess_bytes(proc.stdout)
            proc_stderr = _decode_subprocess_bytes(proc.stderr)
            # 標準出力の最終行に result JSON が1行出る想定
            # （huggingface_hub由来の警告等が混じっても最終行だけ見ればよい）。
            for line in reversed(proc_stdout.strip().splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    result_json = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                break

            if result_json is None:
                print(f"[StandaloneTaggerBackend] {model_id}: ダウンロード子プロセスの"
                      f"結果を解析できませんでした（returncode={proc.returncode}）。", flush=True)
                if proc_stderr:
                    print(f"  子プロセスstderr: {proc_stderr[-2000:]}", flush=True)
            else:
                if result_json.get("model_ok"):
                    print(f"[StandaloneTaggerBackend] {model_id}: model.onnx ダウンロード完了 "
                          f"({model_path}).", flush=True)
                if result_json.get("tags_ok"):
                    print(f"[StandaloneTaggerBackend] {model_id}: タグ定義ファイル "
                          f"ダウンロード完了 ({tags_path}).", flush=True)
                if result_json.get("error"):
                    print(f"[StandaloneTaggerBackend] {model_id}: 自動ダウンロードの一部/全部が"
                          f"失敗しました: {result_json['error']}", flush=True)

        except subprocess.TimeoutExpired:
            print(f"[StandaloneTaggerBackend] {model_id}: ダウンロードが {timeout_sec:.0f}秒"
                  f"応答なしのためタイムアウトしました。ネットワーク環境をご確認ください。",
                  flush=True)
            # subprocess.run(timeout=...) はタイムアウト時、内部で子プロセスへ
            # kill() を送ってから TimeoutExpired を送出するため、ここで
            # 追加の後始末は不要（ハングした子プロセスがゾンビ化する心配がない）。
        except Exception as e:
            # subprocess の起動自体に失敗した場合（実行権限が無い等）。
            print(f"[StandaloneTaggerBackend] {model_id}: ダウンロード子プロセスの起動に"
                  f"失敗しました: {e}", flush=True)

        return (model_path if model_path.is_file() else None,
                tags_path if tags_path.is_file() else None)

    @staticmethod
    def _validate_onnx_file(path: Path) -> bool:
        """
        ダウンロードした .onnx ファイルが壊れていないか軽量チェックする。

        onnx.checker.check_model() はファイル全体をロード・検証するため
        model.onnx を最終的にロードする onnxruntime.InferenceSession() より
        安全に「壊れたファイルかどうか」を Python 例外として検出できる
        （破損データを直接 InferenceSession に渡すとネイティブ側で
        異常終了する場合があるため、その前段で必ず通す）。

        onnx パッケージが無い環境では検証をスキップし True を返す
        （setup_runtime_env.py は onnx を追加インストールしているため
        通常は入っている想定だが、無い場合でも自動DL自体は止めない）。
        """
        try:
            import onnx
        except ImportError:
            return True

        try:
            if path.stat().st_size < 1024:
                # 数バイト～数KBしかない「ダウンロード失敗ページ」等を弾く
                return False
            model = onnx.load(str(path))
            onnx.checker.check_model(model)
            return True
        except Exception as e:
            print(f"[StandaloneTaggerBackend] ONNX検証失敗: {e}", flush=True)
            return False

    def _evict_lru_if_needed(self, keep_room_for: int = 1) -> None:
        """
        ロード済みモデル数が MAX_LOADED_MODELS - keep_room_for を超える場合、
        最長未使用のモデルから解放する。
        _MyEXT_ComfyUI_Tagger_Worker の _evict_lru_if_needed と同一ロジック。
        """
        limit = max(0, MAX_LOADED_MODELS - keep_room_for)
        while len(self._sessions) > limit:
            oldest_id = min(self._sessions, key=lambda k: self._sessions[k].last_used)
            print(f"[StandaloneTaggerBackend] LRU 上限超過のため解放: {oldest_id}", flush=True)
            self._unload_session(oldest_id)

    def _unload_session(self, model_id: str) -> None:
        """指定モデルのセッションを解放する。"""
        entry = self._sessions.pop(model_id, None)
        if entry is None:
            return
        del entry.session
        import gc
        gc.collect()
        print(f"[StandaloneTaggerBackend] Unloaded: {model_id} "
              f"(idle {entry.idle_seconds():.0f}s)", flush=True)

    def release_idle_sessions(self) -> int:
        """
        IDLE_TIMEOUT_SECONDS 以上未使用のセッションを解放する。
        呼び出し元（main_window.py 等）が定期タイマーから呼ぶことを想定。
        このクラス自身はバックグラウンドスレッドを持たない。

        Returns:
            解放したセッション数
        """
        idle_ids = [
            model_id for model_id, entry in self._sessions.items()
            if entry.idle_seconds() > IDLE_TIMEOUT_SECONDS
        ]
        for model_id in idle_ids:
            self._unload_session(model_id)
        return len(idle_ids)

    @property
    def loaded_models(self) -> list[str]:
        """現在ロード済みのモデル ID 一覧（デバッグ・状態表示用）。"""
        return list(self._sessions.keys())

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    @staticmethod
    def _preprocess(image_pil, model_spec: dict):
        """
        モデル仕様に従って前処理する。worker.py
        （_MyEXT_ComfyUI_Tagger_Worker）の preprocess_wd14() /
        preprocess_camie() / preprocess_joytag() と等価な処理を
        model_spec 駆動で汎用化したもの。

        model_spec のキー:
          input_size  リサイズ後の一辺ピクセル数
          layout      "NHWC"（wd14） / "NCHW"（camie, joytag）
          color       "rgb" / "bgr"（wd14 のみ BGR 変換が必要）
          normalize   "none"（[0,255]のまま。wd14） /
                      "scale01"（[0,1]。joytag） /
                      "imagenet"（[0,1] 後に ImageNet mean/std 正規化。camie）
        """
        import numpy as np
        from PIL import Image

        size  = model_spec["input_size"]
        image = image_pil.convert("RGB").resize((size, size), Image.LANCZOS)
        arr   = np.array(image, dtype=np.float32)

        normalize = model_spec.get("normalize", "none")
        if normalize == "imagenet":
            arr  = arr / 255.0
            mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
            std  = np.array([0.229, 0.224, 0.225], dtype=np.float32)
            arr  = (arr - mean) / std
        elif normalize == "scale01":
            arr = arr / 255.0
        # normalize == "none" の場合は [0,255] のまま（wd14 互換）

        if model_spec.get("color") == "bgr":
            arr = arr[:, :, ::-1]

        if model_spec.get("layout") == "NCHW":
            arr = arr.transpose(2, 0, 1)  # HWC → CHW
            # バグ修正(軽微な最適化): 上のBGR反転([::-1])とtranspose()は
            # いずれも非連続viewを生成する。連続化しないままonnxruntimeに
            # 渡すと、モデル/EPによっては内部で暗黙にコピーが発生し、
            # 反転+転置のコピーと合わせて二重コピーになりうる。ここで
            # 明示的に一度だけ連続化しておく。
            return np.ascontiguousarray(arr[None])  # (1, 3, size, size)

        return np.ascontiguousarray(arr[None])  # NHWC: (1, size, size, 3)

    def _create_session(self, ort, model_path: Path, device: str, gpu_device_id: int = 0,
                         is_intel_gpu: bool = True):
        """
        ONNXセッションを作成する。

        試行順序（device 設定による）:
          NPU  → OpenVINO NPU → [Intel GPUのみ]OpenVINO GPU → DirectML GPU → CPU(スレッド制限)
          GPU  → [Intel GPUのみ]OpenVINO GPU → DirectML GPU → CPU(スレッド制限)
          CPU  → CPU(スレッド制限) のみ

        is_intel_gpu: 選択中のGPU（gpu_device_id）がIntel製かどうか。
        OpenVINOのGPU EPはIntel GPU専用で、NVIDIA/AMDのGPUを要求すると
        エラーにならず検出できたIntel GPU（多くはiGPU）へ静かに読み替えて
        しまう場合があるため、Intel製でなければOpenVINO GPUの試行自体を
        スキップしてDirectMLへ直行する（_get_or_load_session()参照）。

        ov_cache_dir を model.onnx と同階層の ov_cache/ に設定する。
        """
        import os
        opts_cpu = ort.SessionOptions()
        opts_cpu.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        cpu_threads = int(os.environ.get("D_LINER_ORT_THREADS", "2"))
        opts_cpu.intra_op_num_threads = cpu_threads
        opts_cpu.inter_op_num_threads = 1

        opts_accel = ort.SessionOptions()
        opts_accel.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        ov_cache_dir = str(model_path.parent / "ov_cache")
        dev = device.upper()

        if dev == "NPU":
            session = self._try_openvino_session(ort, opts_accel, model_path, "NPU", ov_cache_dir)
            if session is not None:
                return session
            if is_intel_gpu:
                print("[StandaloneTaggerBackend] NPU failed, trying OpenVINO GPU...", flush=True)
                session = self._try_openvino_session(ort, opts_accel, model_path, f"GPU.{gpu_device_id}", ov_cache_dir)
                if session is not None:
                    return session
            else:
                print("[StandaloneTaggerBackend] 選択GPUはIntel製ではないため "
                      "OpenVINO GPUをスキップします。", flush=True)
            print("[StandaloneTaggerBackend] OpenVINO GPU利用不可、DirectML GPUを試行...", flush=True)
            session = self._try_directml_session(ort, model_path, gpu_device_id)
            if session is not None:
                return session
            print(f"[StandaloneTaggerBackend] GPU unavailable. CPU fallback "
                  f"(threads={cpu_threads}).", flush=True)

        elif dev == "GPU":
            if is_intel_gpu:
                session = self._try_openvino_session(ort, opts_accel, model_path, f"GPU.{gpu_device_id}", ov_cache_dir)
                if session is not None:
                    return session
            else:
                print("[StandaloneTaggerBackend] 選択GPUはIntel製ではないため "
                      "OpenVINO GPUをスキップし、DirectMLを直接試します。", flush=True)
            session = self._try_directml_session(ort, model_path, gpu_device_id)
            if session is not None:
                return session
            print(f"[StandaloneTaggerBackend] GPU unavailable. CPU fallback "
                  f"(threads={cpu_threads}).", flush=True)

        try:
            providers = ["CPUExecutionProvider"]
            session = ort.InferenceSession(str(model_path), sess_options=opts_cpu,
                                           providers=providers)
            print(f"[StandaloneTaggerBackend] Session created on CPU "
                  f"(intra_threads={cpu_threads}).", flush=True)
            return session
        except Exception as e:
            print(f"[StandaloneTaggerBackend] CPU session creation failed: {e}", flush=True)
            return None

    @staticmethod
    def _try_openvino_session(ort, opts, model_path: Path, device_type: str,
                              ov_cache_dir: str):
        """
        OpenVINO ExecutionProvider でセッション作成を試みる。
        成功すれば session を返し、失敗すれば None を返す（例外を飲む）。

        注意: onnxruntime-openvino の Windows wheel は openvino.dll 等の
        ランタイム本体を同梱しない。そのため本モジュール読み込み時
        （import onnxruntime より前）に add_openvino_libs_to_path() を
        既に1回呼び出し済みである（モジュール先頭を参照）。
        それでも失敗する場合は openvino パッケージ自体が未インストール、
        またはバージョン不整合の可能性が高い。
        呼び出し元は get_providers()[0] を見て実際に使われた EP を
        確認すること（本メソッドはその確認も行い、ログに出す）。
        """
        try:
            available = [p for p in ort.get_available_providers()
                         if "OpenVINO" in p]
            if not available:
                print(f"[StandaloneTaggerBackend] OpenVINOExecutionProvider not available "
                      f"(onnxruntime-openvino 未インストール?).", flush=True)
                return None

            provider_options = {
                "device_type": device_type,
                "cache_dir":   ov_cache_dir,
            }
            providers = [
                ("OpenVINOExecutionProvider", provider_options),
                "CPUExecutionProvider",
            ]
            session = ort.InferenceSession(str(model_path), sess_options=opts,
                                           providers=providers)
            used = session.get_providers()[0] if session.get_providers() else "unknown"

            # 要求した EP と実際の EP が一致するか確認する。
            # （DLL ロード失敗時に例外を投げず CPU へ黙ってフォールバックする
            #   onnxruntime の挙動を検証スクリプト側で確認済み。本体側でも
            #   同じ検証を入れておく）
            if used != "OpenVINOExecutionProvider":
                print(f"[StandaloneTaggerBackend] OpenVINO {device_type} を要求しましたが "
                      f"実際は {used} にフォールバックしています "
                      f"(openvino パッケージ未インストールの可能性)。", flush=True)
                return None

            print(f"[StandaloneTaggerBackend] Session created: EP={used} "
                  f"(requested {device_type}).", flush=True)
            return session
        except Exception as e:
            print(f"[StandaloneTaggerBackend] OpenVINO {device_type} failed: {e}", flush=True)
            return None

    @staticmethod
    def _try_directml_session(ort, model_path: Path, gpu_device_id: int = 0):
        """
        DirectML ExecutionProvider（Windows GPU汎用）を試みる。
        onnxruntime-directml が必要:
          pip install onnxruntime-directml
        失敗時は None を返す。
        """
        try:
            # セッション18 追記（重大バグ修正）: 以前は
            # `"DML" in p or "DirectML" in p` という大文字小文字を区別する
            # 判定だったが、onnxruntime が実際に返すプロバイダ名は
            # "DmlExecutionProvider"（大文字はDのみ）であり、"DML"（全て
            # 大文字）はこの文字列に一致しないため、この判定は常にFalseに
            # なっていた。onnxruntime-directml が正しくインストールされ、
            # 実際に使用可能な環境（検証機で実セッションprobeにより動作確認済み）
            # でも「利用不可」と誤判定され、常にCPUへフォールバックしていた。
            available = [p for p in ort.get_available_providers()
                         if "dml" in p.lower()]
            if not available:
                print("[StandaloneTaggerBackend] DirectMLExecutionProvider not available "
                      "(onnxruntime-directml 未インストール?).", flush=True)
                return None

            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            provider_options = {"device_id": str(gpu_device_id)}
            providers = [
                ("DmlExecutionProvider", provider_options),
                "CPUExecutionProvider",
            ]
            session = ort.InferenceSession(str(model_path), sess_options=opts,
                                           providers=providers)
            used = session.get_providers()[0] if session.get_providers() else "unknown"
            print(f"[StandaloneTaggerBackend] Session created: EP={used} (DirectML device_id={gpu_device_id}).", flush=True)
            return session
        except Exception as e:
            print(f"[StandaloneTaggerBackend] DirectML failed: {e}", flush=True)
            return None

    def _resolve_model_path(self, model_id: str) -> Path | None:
        """{model_id}/model.onnx を探索して返す。"""
        # 0. QSettings で手動フォルダ指定（全モデル対応、セッション18追加）
        #    main_window.py のタグ付け設定ダイアログから設定可能。
        #    ComfyUI piggyback連携（worker.py/PIDファイル探索）は凍結中で
        #    実際には使われないため、その代わりに非ComfyUIユーザー向けの
        #    救済手段として用意した。自動ダウンロードが何らかの事情
        #    （社内ポリシー等でHuggingFaceへのアクセスがブロックされている
        #    場合等）で機能しない環境でも、手動でダウンロードした
        #    model.onnx + タグ定義ファイルを任意のフォルダに置いて
        #    ここで指定すればそのまま使える。
        if self._settings is not None:
            explicit_dir = self._settings.value(f"tagger/model_dir/{model_id}", "")
            if explicit_dir:
                # 2パターンを探索する:
                #   (1) 指定フォルダ直下に model.onnx がある（例: E:/models/camie）
                #   (2) 指定フォルダが複数モデル共通の親フォルダで、
                #       {model_id} サブフォルダの下にある（例: E:/models → E:/models/camie）
                # モデルごとに毎回フォルダを指定し直さなくて済むよう、
                # 親フォルダを1回指定するだけで全モデルに使い回せるようにする。
                candidates = [
                    Path(explicit_dir) / "model.onnx",
                    Path(explicit_dir) / model_id / "model.onnx",
                ]
                found = next((c for c in candidates if c.is_file()), None)
                if found is not None:
                    print(f"[StandaloneTaggerBackend] 手動指定フォルダの model.onnx を使用: {found}",
                          flush=True)
                    return found
                print(f"[StandaloneTaggerBackend] 警告: 手動指定フォルダに model.onnx が"
                      f"見つかりません: {explicit_dir}"
                      f"（{explicit_dir}/model.onnx または "
                      f"{explicit_dir}/{model_id}/model.onnx を探索しました）", flush=True)

        # 1. QSettings で明示指定（旧キー・wd14のみのファイル直接指定。
        #    後方互換のため維持）
        if model_id == "wd14" and self._settings is not None:
            explicit = self._settings.value("tagger/model_path", "")
            if explicit:
                p = Path(explicit)
                print(f"[StandaloneTaggerBackend] QSettings model_path: {p}", flush=True)
                return p

        # 2. ComfyUI カスタムノード以下の同名モデルを間借り
        #
        # セッション12: 一度オプトイン方式に変更したが、実機確認の結果
        # 「ComfyUI側に既にあるなら、d_liner/models へ二重にダウンロード
        # するよりそちらを優先して使いたい」との判断により復活。
        # デフォルト有効（QSettings["tagger/allow_comfyui_borrow"] を
        # 明示的に false にすれば無効化できる）。
        # d_liner/models/{model_id}/ より先にチェックすることで、
        # 「ComfyUI側に既に存在するのに d_liner 側へも自動ダウンロードして
        # 二重にディスクを消費する」事態を避ける。
        allow_borrow = True
        if self._settings is not None:
            raw = self._settings.value("tagger/allow_comfyui_borrow", True)
            allow_borrow = str(raw).strip().lower() in ("1", "true", "yes")

        if allow_borrow:
            comfyui_path = self._find_comfyui_model(model_id)
            if comfyui_path is not None:
                return comfyui_path

        # 3. D-liner 同階層の models/{model_id}/
        local = Path(__file__).parent / "models" / model_id / "model.onnx"
        if local.is_file():
            print(f"[StandaloneTaggerBackend] Found local model: {local}", flush=True)
            return local

        return None

    @staticmethod
    def _find_comfyui_model(model_id: str) -> Path | None:
        """ComfyUI の custom_nodes 配下にある同名モデルを探す。"""
        _sm_roots = [
            Path("C:/StabilityMatrix/Data/Packages/ComfyUI"),
            Path.home() / "StabilityMatrix/Data/Packages/ComfyUI",
        ]
        candidate_names = {
            "wd14": ("wd14", "wd14-swinv2", "wd14_swinv2"),
        }.get(model_id, (model_id,))

        for sm in _sm_roots:
            cn = sm / "custom_nodes"
            if not cn.is_dir():
                continue
            for node_dir in cn.iterdir():
                for candidate_name in candidate_names:
                    p = node_dir / "models" / candidate_name / "model.onnx"
                    if p.is_file():
                        print(f"[StandaloneTaggerBackend] Found ComfyUI {model_id} model: {p} "
                              f"(間借り優先。無効にするには "
                              f"tagger/allow_comfyui_borrow=false)", flush=True)
                        return p
        return None

    def _resolve_tags_path(self, model_id: str, model_path: Path | None) -> Path | None:
        """
        タグ定義ファイル（wd14: selected_tags.csv / camie: *-metadata.json /
        joytag: top_tags.txt）を探索して返す。ファイル名は
        _SUPPORTED_MODELS[model_id]["tags_filename"] を使う。
        """
        spec = self._SUPPORTED_MODELS.get(model_id, {})
        filename = spec.get("tags_filename", "selected_tags.csv")

        # 0. 手動指定フォルダ（_resolve_model_path()と同じキー、全モデル対応、
        #    直下パターン/{model_id}サブフォルダパターン両対応）
        if self._settings is not None:
            explicit_dir = self._settings.value(f"tagger/model_dir/{model_id}", "")
            if explicit_dir:
                candidates = [
                    Path(explicit_dir) / filename,
                    Path(explicit_dir) / model_id / filename,
                ]
                found = next((c for c in candidates if c.is_file()), None)
                if found is not None:
                    return found

        if model_id == "wd14" and self._settings is not None:
            explicit = self._settings.value("tagger/tags_csv_path", "")
            if explicit:
                return Path(explicit)

        if model_path is not None:
            local = model_path.parent / filename
            if local.is_file():
                return local

        dliner_tags = Path(__file__).parent / "models" / model_id / filename
        if dliner_tags.is_file():
            return dliner_tags

        return None

    def _load_tags(self, model_id: str, tags_path: Path) -> list[dict] | None:
        """
        タグ定義ファイルを読み込み、全モデル共通の正規化形式
        [{"name": str, "category": str}, ...] を返す。
        category は "general"/"character"/"copyright"/"artist"/
        "meta"/"rating"/"year" のいずれかに統一する。
        モデルごとにファイル形式が異なるため model_id で分岐する。
        """
        if model_id == "camie":
            return self._load_tags_camie(tags_path)
        if model_id == "joytag":
            return self._load_tags_joytag(tags_path)
        return self._load_tags_wd14_csv(tags_path)

    @staticmethod
    def _load_tags_wd14_csv(csv_path: Path) -> list[dict] | None:
        """
        selected_tags.csv を読み込んでタグリストを返す（wd14）。
        列: tag_id, name, category, count
        category（数値）: 0=general, 1=artist, 3=copyright, 4=character, 9=meta/rating
        rating 判定は _RATING_TAGS の名前一致、または
        category==9 かつ "rating:" 始まりの名前を rating として扱う
        （従来の StandaloneTaggerBackend 仕様を踏襲）。
        """
        tags = []
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        name    = row["name"]
                        cat_int = int(row["category"])
                    except (KeyError, ValueError):
                        continue

                    if name in StandaloneTaggerBackend._RATING_TAGS or (
                        cat_int == 9 and name.startswith("rating:")
                    ):
                        category = "rating"
                    elif cat_int == 9:
                        category = "meta"
                    else:
                        category = StandaloneTaggerBackend._CATEGORY_MAP.get(cat_int, "general")

                    tags.append({"name": name, "category": category})
            print(f"[StandaloneTaggerBackend] CSV loaded: {len(tags)} tags from {csv_path.name}",
                  flush=True)
            return tags
        except Exception as e:
            print(f"[StandaloneTaggerBackend] CSV load failed: {e}", flush=True)
            return None

    @staticmethod
    def _load_tags_camie(json_path: Path) -> list[dict] | None:
        """
        camie-tagger-v2-metadata.json を読み込んでタグリストを返す。
        dataset_info.tag_mapping.idx_to_tag（index → タグ名）と
        tag_to_category（タグ名 → カテゴリ文字列。既に
        general/character/copyright/artist/meta/rating/year の
        7カテゴリで正規化済み）をそのまま使う。
        """
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            tag_mapping     = meta["dataset_info"]["tag_mapping"]
            idx_to_tag_raw  = tag_mapping["idx_to_tag"]
            tag_to_category = tag_mapping.get("tag_to_category", {})

            idx_to_tag = {int(k): v for k, v in idx_to_tag_raw.items()}
            tags = []
            for i in range(len(idx_to_tag)):
                name = idx_to_tag.get(i, "")
                cat  = tag_to_category.get(name, "general")
                tags.append({"name": name, "category": cat})

            print(f"[StandaloneTaggerBackend] camie metadata loaded: {len(tags)} tags "
                  f"from {json_path.name}", flush=True)
            return tags
        except Exception as e:
            print(f"[StandaloneTaggerBackend] camie metadata load failed: {e}", flush=True)
            return None

    @staticmethod
    def _load_tags_joytag(txt_path: Path) -> list[dict] | None:
        """
        top_tags.txt（1行1タグ）を読み込んでタグリストを返す。
        JoyTag はカテゴリ情報を持たないため全タグ "general" 扱いになる
        （worker.py の _load_tags_from_file() と同じ挙動）。
        """
        try:
            tags = []
            with open(txt_path, "r", encoding="utf-8") as f:
                for line in f:
                    name = line.strip()
                    if name:
                        tags.append({"name": name, "category": "general"})
            print(f"[StandaloneTaggerBackend] JoyTag tags loaded: {len(tags)} tags "
                  f"from {txt_path.name}", flush=True)
            return tags
        except Exception as e:
            print(f"[StandaloneTaggerBackend] JoyTag tags load failed: {e}", flush=True)
            return None

    @staticmethod
    def _print_model_search_hint(model_id: str) -> None:
        spec = StandaloneTaggerBackend._SUPPORTED_MODELS.get(model_id, {})
        tags_filename = spec.get("tags_filename", "selected_tags.csv")
        print("  モデル配置先:", flush=True)
        print(f"    {{d_liner_dir}}/models/{model_id}/model.onnx", flush=True)
        print(f"    {{d_liner_dir}}/models/{model_id}/{tags_filename}", flush=True)
        if spec.get("download"):
            print(f"  （このモデルは自動ダウンロード対応です。上記が見つからない場合、"
                  f"次回タグ付け実行時に {spec['download']['repo_id']} から自動取得を試みます。"
                  f"huggingface_hub パッケージが必要です。）", flush=True)
        if model_id == "wd14":
            print("  または QSettings[tagger/model_path] で絶対パスを指定してください。",
                  flush=True)
        print("  （ComfyUIのcustom_nodes配下に同名モデルがあれば自動的に間借りします。"
              "無効にするには QSettings[tagger/allow_comfyui_borrow]=false を設定してください。）",
              flush=True)


# 後方互換のエイリアス（既存コードが StandaloneWD14Backend を import していても壊れない）
StandaloneWD14Backend = StandaloneTaggerBackend



# ===========================================================================
# TaggerEngine — インターフェース層（最小変更）
# ===========================================================================

class TaggerEngine:
    """
    D-liner タガーのインターフェース層。
    main_window.py からの呼び出し形式（engine.tag() / engine.is_available）は変えない。

    セッション9: StandaloneTaggerBackend のみ有効（Lazy Load + LRU 対応）。
    将来: ComfyWorkerBackend を ITaggerBackend として追加し、
          connect_or_launch() の凍結コードを再有効化する。
    """

    def __init__(self, settings=None) -> None:
        self._settings = settings
        self._backend: ITaggerBackend | None = None

        # --- 凍結中: 旧 ComfyUI 連携フィールド（削除不可）---
        # self._mode: Literal["piggyback", "standalone", "unavailable"] = "unavailable"
        # self._port: int | None = None
        # self._token: str | None = None
        # self._proc: subprocess.Popen | None = None
        # self._registered: bool = False
        # self._lock = __import__("threading").Lock()
        # self._standalone_worker_path: Path | None = None
        # self._standalone_models_dir:  str  | None = None
        # self._standalone_pid_file:    str  | None = None

    # ------------------------------------------------------------------
    # 接続確立
    # ------------------------------------------------------------------

    def connect_or_launch(self) -> bool:
        """
        StandaloneTaggerBackend を初期化して返す。
        Lazy Load のため、ここではモデルファイルの実在確認のみ行い、
        実際のモデルロードは初回 tag() 呼び出し時まで遅延する。

        【凍結中】ComfyUI 探索パス（_find_comfyui_pid_file）は呼ばない。
        将来の再統合時にここを最初に確認して ComfyWorkerBackend を試みる。

        # --- FROZEN: ComfyUI piggyback 探索 ---
        # pid_info = self._find_comfyui_pid_file()
        # if pid_info:
        #     port, token = pid_info
        #     ... ComfyWorkerBackend 初期化 ...
        # --- END FROZEN ---
        """
        backend = StandaloneTaggerBackend(self._settings)
        if backend.initialize():
            self._backend = backend
            print("[TaggerEngine] StandaloneTaggerBackend ready.", flush=True)
            return True
        print("[TaggerEngine] StandaloneTaggerBackend initialization failed.", flush=True)
        return False

    # ------------------------------------------------------------------
    # タグ付け
    # ------------------------------------------------------------------

    def tag(
        self,
        image_path: str,
        model: str = "wd14",
        device: str = "CPU",
        threshold: float = 0.35,
        threshold_character: float = 0.75,
        threshold_copyright: float = 0.50,
        replace_underscores: bool = False,
        raw_scores: bool = False,
        **kwargs,
    ) -> dict | None:
        if self._backend is None:
            return None
        return self._backend.tag(
            image_path,
            model=model,
            device=device,
            threshold=threshold,
            threshold_character=threshold_character,
            threshold_copyright=threshold_copyright,
            replace_underscores=replace_underscores,
        )

    # ------------------------------------------------------------------
    # 終了処理
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        if self._backend is not None:
            self._backend.shutdown()
            self._backend = None

    # ------------------------------------------------------------------
    # モデル切り替え一貫性チェック（セッション9 追加）
    # ------------------------------------------------------------------

    def consume_model_switch_warning(self) -> tuple[str, str] | None:
        """
        モデルが切り替わったことを検出していれば (旧model, 新model) を返し、
        同時に内部フラグをクリアする。main_window.py 側はこれを
        （例えばタグ付け完了時や定期チェックで）呼び出し、None でなければ
        「モデルが切り替わりました。既存のタグ付け結果との一貫性のため
        再タグ付けを推奨します」といったダイアログを表示すること。

        実際に再タグ付けを行うかはユーザー判断に委ねる。
        StandaloneTaggerBackend.tag() のみがこのフラグを立てる
        （ITaggerBackend インターフェースとしては必須ではないため、
        backend がこのメソッドを持たない場合は None を返す）。
        """
        if self._backend is None:
            return None
        consume = getattr(self._backend, "consume_model_switch_warning", None)
        if consume is None:
            return None
        return consume()

    def release_idle_sessions(self) -> int:
        """
        長時間未使用のモデルセッションを解放する。

        セッション10: main_window.py 側がタグ付けキュー枯渇イベントに
        フックして呼ぶイベント駆動方式に変更（約60秒アイドルで解放）。
        IDLE_CHECK_INTERVAL_SECONDS / IDLE_TIMEOUT_SECONDS のポーリング値は
        イベント駆動が働かなかった場合の保険として残している。

        Returns:
            解放したセッション数（backend が対応していなければ 0）
        """
        if self._backend is None:
            return 0
        release = getattr(self._backend, "release_idle_sessions", None)
        if release is None:
            return 0
        return release()

    @property
    def loaded_models(self) -> list[str]:
        """現在ロード済みのモデル ID 一覧（デバッグウィンドウ表示用）。"""
        if self._backend is None:
            return []
        return getattr(self._backend, "loaded_models", [])

    # ------------------------------------------------------------------
    # 動的モード切り替え（凍結中）
    # ------------------------------------------------------------------

    def check_and_rebalance(self) -> str | None:
        """
        【凍結中】ComfyUI piggyback / standalone 自動切り替え。
        main_window.py の _rebalance_timer は停止済み（セッション8）。
        将来の再統合時に再有効化する。
        """
        # --- FROZEN ---
        # if self._mode == "standalone":
        #     return self._try_upgrade_to_piggyback()
        # elif self._mode == "piggyback":
        #     return self._try_fallback_to_standalone()
        return None

    def check_alive(self) -> bool:
        """バックエンドが実際に動作可能か確認する。"""
        if self._backend is None:
            return False
        return self._backend.is_available()

    # ------------------------------------------------------------------
    # プロパティ
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """バックエンドが使用可能な状態か（軽量チェック）。"""
        return self._backend is not None and self._backend.is_available()

    @property
    def mode(self) -> str:
        """
        現在のモード文字列（互換性のため残す）。

        修正（セッション11）: 以前は "standalone_wd14" を無条件に返しており、
        camie/joytag に切り替えても接続完了ログが常に WD14 と表示される
        バグがあった。Lazy Load 方式のため connect_or_launch() 時点では
        まだどのモデルもロードされていない（実際に使うモデルは初回 tag() 時に
        決まる）ので、ここでは特定モデル名を偽って返さず、実際にロード済みの
        モデルがあればそれを、無ければ汎用の "standalone" を返す。
        """
        if self._backend is None:
            return "unavailable"
        loaded = getattr(self._backend, "loaded_models", [])
        if loaded:
            return "standalone_" + "+".join(loaded)
        return "standalone"

    # ------------------------------------------------------------------
    # 凍結中メソッド群（削除不可 — 将来の ComfyWorkerBackend 統合に使用）
    # ------------------------------------------------------------------

    def _find_comfyui_pid_file(self) -> tuple[int, str] | None:
        """【凍結中】ComfyUI Worker の PIDファイルを探して (port, token) を返す。"""
        # --- FROZEN: connect_or_launch() から呼ばれなくなった ---
        return None

    def _find_worker_script(self) -> Path | None:
        """【凍結中】worker.py の場所を探索する。"""
        return None

    def _find_venv_python(self, worker_script: Path) -> str:
        """【凍結中】worker.py が動作できる Python を探す。"""
        return sys.executable

    def _find_models_dir(self, worker_script: Path) -> str | None:
        """【凍結中】worker_script と同じカスタムノードディレクトリの models/ を返す。"""
        return None

    def _launch_worker(self, worker_script: Path, models_dir=None, pid_file=None):
        """【凍結中】worker.py を独立プロセスとして起動する。"""
        return None, None

    def _try_upgrade_to_piggyback(self) -> str | None:
        """【凍結中】standalone → piggyback 昇格。"""
        return None

    def _try_fallback_to_standalone(self) -> str | None:
        """【凍結中】piggyback → standalone 降格。"""
        return None

    def _graceful_shutdown_standalone(self) -> None:
        """【凍結中】standalone worker の正常終了。"""
        pass

    @staticmethod
    def _send_raw(req: dict, port: int, token: str, timeout: float = _RECV_TIMEOUT) -> dict | None:
        """【凍結中】Worker プロトコル準拠の送受信。"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(("127.0.0.1", port))
                payload = json.dumps(req).encode("utf-8")
                s.sendall(payload)
                s.shutdown(socket.SHUT_WR)
                resp_data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    resp_data += chunk
            if not resp_data:
                return None
            return json.loads(resp_data.decode("utf-8"))
        except Exception:
            return None

    def _read_pid_file(self, pid_file: Path) -> tuple[int, int, str] | None:
        """【凍結中】PIDファイルを読んで (pid, port, token) を返す。"""
        try:
            with open(pid_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return int(data["pid"]), int(data["port"]), str(data["token"])
        except Exception:
            return None

    def _is_pid_alive(self, pid: int) -> bool:
        """【凍結中】指定 PID のプロセスが生きているか確認する。"""
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except ProcessLookupError:
            return False
        except Exception:
            return False

    def _ping(self, port: int, token: str) -> bool:
        """【凍結中】Worker に ping を送って応答を確認する。"""
        return False

    def _register(self) -> None:
        """【凍結中】Worker にクライアント登録を通知する。"""
        pass
