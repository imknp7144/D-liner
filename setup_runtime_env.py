"""
setup_runtime_env.py — D-liner 本体実行用 venv セットアップ
=========================================================================
システム Python で実行する（このスクリプト自体は onnxruntime 等を必要としない）。

検証フェーズ（setup_tagger_env.py / verify_tagger.py）で確立した知見をそのまま
本番実行用 venv に転用する:
  - onnxruntime-openvino だけでは Windows 版は openvino.dll を同梱しないため、
    openvino パッケージを明示インストールする
  - _MyEXT_ComfyUI_Tagger_Worker の requirements_worker.txt とバージョンを揃え、
    ComfyUI worker 環境との挙動差を排除する
  - インストール後に実セッションを作って OpenVINOExecutionProvider が
    本当に有効か probe する（名前がリストに出るだけでは不十分）

セッション10: NPU の有無を自動検出し、無ければ onnxruntime-directml
（GPU向け）に切り替える仕組みを追加。
  - onnxruntime-openvino / onnxruntime-directml / 素の onnxruntime は
    いずれも同じ "onnxruntime" パッケージ名を名乗る別配布物であり、
    同一 venv に共存できない（セッション9で踏んだ「onnxruntime が
    名前空間パッケージとして破損する」不具合の再発要因になる）。
    そのため venv 作成時点で一方だけを選んでインストールする必要がある。
  - 検出方法: Windows の CIM（WMI）で "Intel(R) AI Boost" などの NPU
    デバイスの有無を確認する（OpenVINO の NPU プラグインは Intel NPU
    専用のため、Intel NPU の有無で判定すれば十分）。
  - NPU あり → onnxruntime-openvino + openvino（従来どおり）
    NPU なし → onnxruntime-directml（DirectX 12 対応 GPU 全般。
               NVIDIA/AMD/Intel iGPU いずれでも動く）
    検出失敗時は安全側の onnxruntime-directml ではなく CPU 版
    （素の onnxruntime）にフォールバックする（誤検出でNPUなしと
    判定してDirectMLを入れてしまうより、確実に動くCPU版の方が安全）。
  - --runtime {auto,npu,directml,cpu} で自動検出を上書き可能。

検証用との違い:
  - venv は CPU/NPU で分けない。D-liner 本体は起動時に環境設定（tagger/device）
    で CPU/NPU/GPU を切り替えるため、1 つの venv に onnxruntime-openvino を
    入れておけば CPU 実行も内包される（CPUExecutionProvider は常に利用可）。
    ただし GPU は「OpenVINO GPU プラグイン（Intel iGPU 用）」までで、
    NVIDIA/AMD GPU の DirectML 高速化には別 venv（onnxruntime-directml）
    が必要（tagger_engine.py の _create_session() 参照）。
  - PyQt6 / Pillow / numpy など D-liner 本体が import するパッケージ一式を
    あわせてインストールする。
  - 最後に D-liner を起動するための launch_d_liner.bat を生成する。

工程:
  1. venv 作成（d_liner_runtime_env/）
  2. NPU 検出 → インストールするランタイム系統（npu/directml/cpu）を決定
  3. パッケージインストール（決定した系統に応じて内容が変わる）
  4. 実セッション probe（NPU/GPU/DirectML が本当に使えるか確認、
     失敗してもCPUで動作は継続）
  5. launch_d_liner.bat を生成（venv 内 pythonw.exe で main_window.py を起動）

使い方:
  setup_runtime_env.bat
  または
  python setup_runtime_env.py [--skip-probe] [--main main_window.py]
                               [--runtime {auto,npu,directml,cpu}]
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from pathlib import Path


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()

VENV_DIR = SCRIPT_DIR / "d_liner_runtime_env"

# _MyEXT_ComfyUI_Tagger_Worker の requirements_worker.txt に合わせて固定。
# 検証フェーズと同一バージョンにすることで「環境差由来の挙動差」を排除する。
ORT_VERSION      = "1.24.1"
OPENVINO_VERSION = "2025.4.1"

# onnxruntime-directml のバージョン（セッション10 追加）。
# onnxruntime-openvino とは別配布物のため、厳密に同一バージョン系列を
# 揃える必要はない。執筆時点の最新安定版を固定。
DIRECTML_VERSION = "1.24.4"

# D-liner 本体が実際に import している外部パッケージのうち、
# ランタイム系統（npu/directml/cpu）によらず共通のもの
# （main_window / sdi_window_viewer / thumbnail_grid / thumbnail_cache /
#   workers / folder_tree / scan_script / lifecycle_manager
#   のソース全体から洗い出し済み。preview_pane は配線撤去済み・次回配布
#   パッケージからも除外予定のため、この洗い出し対象からは除外している）。
#
# セッション10: huggingface_hub を追加。tagger_engine.py の
# StandaloneTaggerBackend._try_auto_download() が camie / joytag の
# model.onnx が未配置の場合に HuggingFace から自動ダウンロードするために使う。
COMMON_PACKAGES = [
    "PyQt6>=6.7",
    "numpy>=1.26",
    "Pillow>=10.0",
    "psutil",
    "huggingface_hub",
    # ファイル削除をゴミ箱経由にするため（thumbnail_grid.py の
    # _delete_file()）。これが無いとImportErrorで os.remove()/
    # shutil.rmtree() に落ちて即時完全削除になり、誤削除時に復元
    # できなくなる。
    "send2trash",
]


def build_runtime_packages(track: str) -> list[str]:
    """
    ランタイム系統（npu/directml/cpu）に応じたインストール対象パッケージ一覧を返す。

    onnxruntime-openvino / onnxruntime-directml / 素の onnxruntime は
    いずれも "onnxruntime" という同じトップレベルパッケージ名を提供する
    別配布物であり、同一 venv に共存できない
    （後からインストールした方が上書きし、前者の実体ファイルが
    中途半端に残って壊れる＝セッション9で踏んだ「名前空間パッケージ化」の
    再発要因になる）。そのため必ずどれか1つだけを選ぶ。
    """
    if track == "npu":
        onnx_packages = [
            f"onnxruntime-openvino=={ORT_VERSION}",
            f"openvino=={OPENVINO_VERSION}",
        ]
    elif track == "directml":
        onnx_packages = [f"onnxruntime-directml=={DIRECTML_VERSION}"]
    elif track == "cpu":
        onnx_packages = ["onnxruntime"]
    else:
        raise ValueError(f"未知のランタイム系統: {track}")

    return COMMON_PACKAGES + onnx_packages

TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE  = SCRIPT_DIR / f"runtime_setup_log_{TIMESTAMP}.txt"


# ---------------------------------------------------------------------------
# ロガー
# ---------------------------------------------------------------------------

class Logger:
    def __init__(self, path: Path) -> None:
        self._fh = open(path, "w", encoding="utf-8", buffering=1)

    def _emit(self, line: str) -> None:
        print(line, flush=True)
        self._fh.write(line + "\n")

    def section(self, title: str) -> None:
        bar = "=" * 62
        self._emit("")
        self._emit(bar)
        self._emit(f"  {title}")
        self._emit(bar)

    def info(self, msg: str = "") -> None: self._emit(f"[INFO] {msg}")
    def ok(self,   msg: str)      -> None: self._emit(f"[OK]   {msg}")
    def warn(self, msg: str)      -> None: self._emit(f"[WARN] {msg}")
    def err(self,  msg: str)      -> None: self._emit(f"[ERR]  {msg}")

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _decode_subprocess_bytes(data: bytes) -> str:
    """
    サブプロセス出力のデコード。

    バグ修正(instruction_vcredist_fix.md 2-4): 従来は
    encoding="utf-8", errors="replace" で固定デコードしていたため、
    Windows側が日本語ロケール(cp932)で出したエラー文字列が文字化けして
    ログに残ってしまい、障害調査時にエラー原文が読めなかった。
    まずUTF-8を試し、失敗したらcp932にフォールバックする。
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


def run(cmd: list, logger: Logger, check: bool = True) -> subprocess.CompletedProcess:
    logger.info(f"RUN: {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
    )
    stdout = _decode_subprocess_bytes(result.stdout)
    stderr = _decode_subprocess_bytes(result.stderr)
    for line in stdout.strip().splitlines():
        logger._emit(f"    {line}")
    for line in stderr.strip().splitlines():
        logger._emit(f"    STDERR: {line}")
    # 呼び出し側は text=True 相当の CompletedProcess.stdout/stderr(str) を
    # 期待しているため、デコード済み文字列に差し替えて返す
    result.stdout = stdout
    result.stderr = stderr
    if check and result.returncode != 0:
        raise RuntimeError(
            f"コマンド失敗 (code={result.returncode}): {' '.join(str(c) for c in cmd)}"
        )
    return result


def venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def venv_pythonw(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "pythonw.exe"
    return venv_dir / "bin" / "python"


# ---------------------------------------------------------------------------
# NPU 検出（セッション10）
# ---------------------------------------------------------------------------

def detect_npu(logger: Logger) -> bool | None:
    """
    Windows の CIM（WMI）経由で Intel NPU（"Intel(R) AI Boost" 等）の
    有無を確認する。

    OpenVINO の NPU Execution Provider は Intel NPU 専用のため、
    Intel NPU デバイスが存在するかどうかで判定すれば十分
    （Qualcomm/AMD の NPU は OpenVINO NPU プラグインの対象外であり、
    そもそも onnxruntime-openvino では使えない）。

    Returns:
        True  : NPU デバイスを検出した
        False : NPU デバイスは見つからなかった（= DirectML 系統が妥当）
        None  : 判定不能（非Windows、PowerShell実行失敗など）
                → 呼び出し元は安全側（CPU版）にフォールバックすること
    """
    logger.section("NPU 検出")

    if sys.platform != "win32":
        logger.warn(f"Windows 以外のプラットフォーム（{sys.platform}）のため NPU 検出をスキップします。")
        return None

    # Get-CimInstance で PnP デバイス名に "AI Boost" または "NPU" を含む
    # ものを検索する。Intel Core Ultra（Meteor Lake以降）のNPUは
    # デバイスマネージャー上で "Intel(R) AI Boost" と表示される。
    ps_script = (
        "$devices = Get-CimInstance -ClassName Win32_PnPEntity "
        "-ErrorAction SilentlyContinue | "
        "Where-Object { $_.Name -match 'AI Boost' -or $_.Name -match 'Neural Processing' -or $_.Name -match '\\bNPU\\b' } ; "
        "if ($devices) { $devices | ForEach-Object { Write-Output $_.Name } } else { Write-Output 'NONE' }"
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
    except Exception as e:
        logger.warn(f"PowerShell 実行に失敗しました: {e}")
        logger.warn("NPU 有無を判定できません。安全側（CPU版）にフォールバックします。")
        return None

    output = (result.stdout or "").strip()
    logger.info(f"PowerShell 検出結果:\n{output}")

    if result.returncode != 0:
        logger.warn(f"PowerShell がエラー終了しました (code={result.returncode})。")
        logger.warn("NPU 有無を判定できません。安全側（CPU版）にフォールバックします。")
        return None

    if not output or output == "NONE":
        logger.info("NPU デバイスは検出されませんでした。")
        return False

    logger.ok(f"NPU デバイスを検出しました: {output.splitlines()[0]}")
    return True


# ---------------------------------------------------------------------------
# VC++ Redistributable 検出（作業指示書: instruction_vcredist_fix.md）
# ---------------------------------------------------------------------------

def detect_vcredist_x64(logger: Logger) -> bool | None:
    """
    Microsoft Visual C++ 2015-2022 Redistributable (x64) の導入有無を
    レジストリで確認する。

    onnxruntime系パッケージ（openvino/directml/cpu いずれの系統でも）は
    VCRUNTIME140.dll / VCRUNTIME140_1.dll / MSVCP140.dll に依存しており、
    未導入環境では import 時に
    "DLL load failed while importing onnxruntime_pybind11_state" で
    失敗する。開発機では他ツール経由で導入済みのことが多く見落としやすいため、
    venv構築前に検出し警告を出す。

    Returns:
        True  : 導入確認できた
        False : 未導入と判定できた
        None  : 判定できなかった（Windows以外、レジストリキー構造の相違など）
    """
    logger.section("VC++ Redistributable 検出")

    if sys.platform != "win32":
        logger.info(f"Windows 以外のプラットフォーム（{sys.platform}）のため検出をスキップします。")
        return None
    try:
        import winreg
        # x64版Pythonから見た通常の格納先
        key_paths = [
            r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64",
            r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X64",
        ]
        for kp in key_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, kp) as key:
                    installed, _ = winreg.QueryValueEx(key, "Installed")
                    if installed == 1:
                        version, _ = winreg.QueryValueEx(key, "Version")
                        logger.ok(f"VC++ Redistributable (x64) 検出: {version}")
                        return True
            except FileNotFoundError:
                continue
        logger.warn(
            "VC++ Redistributable (x64) が見つかりません。"
            "onnxruntime系パッケージの import に失敗する可能性があります。"
        )
        return False
    except Exception as e:
        logger.warn(f"VC++ Redistributable 検出に失敗（判定不能）: {e}")
        return None


# ---------------------------------------------------------------------------
# Step 1: venv 作成
# ---------------------------------------------------------------------------

def create_venv(venv_dir: Path, logger: Logger) -> Path:
    logger.section(f"venv 作成: {venv_dir.name}")
    py = venv_python(venv_dir)

    if venv_dir.exists() and py.exists():
        logger.ok(f"既存 venv を再利用: {venv_dir}")
    else:
        logger.info(f"作成中: {venv_dir}")
        run([sys.executable, "-m", "venv", venv_dir], logger)
        if not py.exists():
            raise RuntimeError(f"venv 作成後に Python が見つかりません: {py}")
        logger.ok("venv 作成完了")

    run([py, "--version"], logger)
    return py


# ---------------------------------------------------------------------------
# Step 2: パッケージインストール
# ---------------------------------------------------------------------------

_ONNXRUNTIME_VARIANT_PACKAGES = [
    "onnxruntime",
    "onnxruntime-directml",
    "onnxruntime-openvino",
    "onnxruntime-gpu",
]


def install_packages(venv_dir: Path, track: str, logger: Logger) -> None:
    logger.section(f"パッケージインストール（D-liner 本体実行用・{track}系統）")
    py = venv_python(venv_dir)
    packages = build_runtime_packages(track)

    run([py, "-m", "pip", "install", "--upgrade", "pip", "--quiet"], logger)

    # セッション18 追記（重大な不具合対応）: create_venv() は既存 venv を
    # そのまま再利用する。onnxruntime / onnxruntime-directml /
    # onnxruntime-openvino はいずれも同じ "onnxruntime" という import
    # 名前空間を提供する別々の pip 配布物であるため、以前と異なる
    # track（例: npu → directml）で同じ venv に再インストールすると、
    # pip は配布物名で管理する都合上、前回インストールされていた別変種の
    # ファイルを自動的にアンインストールしない。結果として新旧の
    # onnxruntime ファイルが混在し、実際にはDirectMLが正しく動作する
    # 環境のはずなのに ort.get_available_providers() が期待通りの
    # EP を返さない、という不具合が実機（検証機、Ryzen 4300U）で確認された。
    # これを避けるため、対象 track 用のパッケージをインストールする前に、
    # 既知の onnxruntime 系変種パッケージを全てアンインストールしておき、
    # 常にクリーンな状態から目的の変種だけをインストールする。
    logger.info("既存の onnxruntime 系パッケージ（変種混在防止のため）をクリーンアップ中...")
    run(
        [py, "-m", "pip", "uninstall", "-y"] + _ONNXRUNTIME_VARIANT_PACKAGES,
        logger, check=False,
    )

    logger.info(f"インストール対象: {packages}")
    run([py, "-m", "pip", "install"] + packages, logger)

    r = run([py, "-c",
             "import onnxruntime as ort; "
             "print('ort:', ort.__version__); "
             "print('file:', ort.__file__); "
             "print('SessionOptions:', ort.SessionOptions); "
             "print('EP_LIST:', ort.get_available_providers())"],
            logger, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            "onnxruntime のインポートに失敗しました（runtime venv）。"
            "上記ログを確認してください。\n"
            "多くの場合、Microsoft Visual C++ 2015-2022 Redistributable (x64) "
            "が未導入です。以下からインストールしてください:\n"
            "  https://aka.ms/vs/17/release/vc_redist.x64.exe"
        )
    if "file: None" in (r.stdout or ""):
        raise RuntimeError(
            "onnxruntime が名前空間パッケージとして解決されています "
            "(__file__ が None)。インストールが壊れている可能性があります。"
        )
    logger.ok(f"パッケージインストール完了（{track}系統: {', '.join(packages)}）")


# ---------------------------------------------------------------------------
# Step 3: 実セッション probe
# ---------------------------------------------------------------------------

def probe_directml(venv_dir: Path, logger: Logger) -> str:
    """
    onnxruntime-directml が実際に DmlExecutionProvider でセッションを
    作れるかを probe する。probe_openvino() の DirectML 版。

    Returns: "DmlExecutionProvider" | "CPUExecutionProvider" | "ERROR"
    """
    logger.section("DirectML 実セッション probe（GPU 有効性確認）")
    py = venv_python(venv_dir)

    probe_script = r"""
import onnxruntime as ort

print("EP_LIST:", ort.get_available_providers())

import onnx
from onnx import helper, TensorProto

X = helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 3])
Y = helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 3])
node = helper.make_node("Identity", ["X"], ["Y"])
graph = helper.make_graph([node], "probe", [X], [Y])
model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
model.ir_version = 8
path = "_dml_probe.onnx"
onnx.save(model, path)

so = ort.SessionOptions()
try:
    sess = ort.InferenceSession(
        path, sess_options=so,
        providers=["DmlExecutionProvider", "CPUExecutionProvider"],
    )
    used = sess.get_providers()[0]
    print("RESULT:", used)
    if used == "DmlExecutionProvider":
        print("PROBE_OK: DML")
    else:
        print("PROBE_FALLBACK_CPU")
except Exception as e:
    print("RESULT_ERROR:", e)
    print("PROBE_FALLBACK_CPU")

import os
os.remove(path)
"""
    r = run([py, "-c", probe_script], logger, check=False)
    out = r.stdout or ""

    if "PROBE_OK: DML" in out:
        logger.ok("DmlExecutionProvider（DirectML GPU）が有効に動作しています。")
        return "DmlExecutionProvider"
    if "PROBE_FALLBACK_CPU" in out or r.returncode != 0:
        logger.warn("DirectML GPU は有効化できませんでした。CPU 実行にフォールバックします。")
        logger.warn("D-liner は動作しますが、タグ付け速度は CPU 相当になります。")
        return "CPUExecutionProvider"
    logger.warn("probe 結果を判定できませんでした（出力を要確認）。")
    return "UNKNOWN"


def probe_openvino(venv_dir: Path, logger: Logger) -> str:
    """
    実際にダミーモデルで InferenceSession を作り、要求した EP が
    名前だけでなく本当に有効かを確認する。
    Returns: "OpenVINOExecutionProvider" | "CPUExecutionProvider" | "ERROR"
    """
    logger.section("OpenVINO 実セッション probe（NPU/GPU 有効性確認）")
    py = venv_python(venv_dir)

    probe_script = r"""
import sys
if sys.platform == "win32":
    try:
        import onnxruntime.tools.add_openvino_win_libs as ov_libs
        ov_libs.add_openvino_libs_to_path()
    except Exception as e:
        print("ADD_LIBS_FAILED:", e)

import onnxruntime as ort
import numpy as np

print("EP_LIST:", ort.get_available_providers())

# 最小のダミーONNXモデルをその場で組み立てて実セッション作成を試す
import onnx
from onnx import helper, TensorProto

X = helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 3])
Y = helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 3])
node = helper.make_node("Identity", ["X"], ["Y"])
graph = helper.make_graph([node], "probe", [X], [Y])
model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
model.ir_version = 8
path = "_ov_probe.onnx"
onnx.save(model, path)

so = ort.SessionOptions()
for device in ("NPU", "GPU"):
    try:
        sess = ort.InferenceSession(
            path, sess_options=so,
            providers=[("OpenVINOExecutionProvider", {"device_type": device}), "CPUExecutionProvider"],
        )
        used = sess.get_providers()[0]
        print(f"RESULT_{device}:", used)
        if used == "OpenVINOExecutionProvider":
            print("PROBE_OK:", device)
            break
    except Exception as e:
        print(f"RESULT_{device}_ERROR:", e)
else:
    print("PROBE_FALLBACK_CPU")

import os
os.remove(path)
"""
    r = run([py, "-c", probe_script], logger, check=False)
    out = r.stdout or ""

    if "PROBE_OK: NPU" in out:
        logger.ok("OpenVINOExecutionProvider が NPU で有効に動作しています。")
        return "OpenVINOExecutionProvider(NPU)"
    if "PROBE_OK: GPU" in out:
        logger.ok("OpenVINOExecutionProvider が GPU（iGPU）で有効に動作しています。")
        return "OpenVINOExecutionProvider(GPU)"
    if "PROBE_FALLBACK_CPU" in out or r.returncode != 0:
        logger.warn("OpenVINO NPU/GPU は有効化できませんでした。CPU 実行にフォールバックします。")
        logger.warn("D-liner は動作しますが、タグ付け速度は CPU 相当になります。")
        return "CPUExecutionProvider"
    logger.warn("probe 結果を判定できませんでした（出力を要確認）。")
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Step 4: ランチャー生成
# ---------------------------------------------------------------------------

LAUNCHER_TEMPLATE = """@echo off
REM launch_d_liner.bat — D-liner 起動ランチャー（実行用venv経由）
REM このファイルは setup_runtime_env.py が自動生成しています。
REM 削除・再生成は setup_runtime_env.bat の再実行で行ってください。

set SCRIPT_DIR=%~dp0
set VENV_PYW={pythonw_path}
set VENV_PY={python_path}
set MAIN={main_path}

if not exist "%VENV_PYW%" (
    echo [ERROR] venv が見つかりません: %VENV_PYW%
    echo setup_runtime_env.bat を先に実行してください。
    pause
    exit /b 1
)

REM デバッグしたい場合は VENV_PY（コンソール表示あり）を使う:
REM "%VENV_PY%" "%MAIN%"
start "" "%VENV_PYW%" "%MAIN%"
"""

LAUNCHER_DEBUG_TEMPLATE = """@echo off
REM launch_d_liner_debug.bat — コンソール表示ありのデバッグ起動用
set VENV_PY={python_path}
set MAIN={main_path}
"%VENV_PY%" "%MAIN%"
pause
"""


def write_launchers(venv_dir: Path, main_path: Path, logger: Logger) -> None:
    logger.section("ランチャー生成")
    pyw = venv_pythonw(venv_dir)
    py  = venv_python(venv_dir)

    launcher = SCRIPT_DIR / "launch_d_liner.bat"
    launcher.write_text(
        LAUNCHER_TEMPLATE.format(pythonw_path=pyw, python_path=py, main_path=main_path),
        encoding="utf-8",
    )
    logger.ok(f"生成: {launcher.name}（通常起動・コンソール非表示）")

    launcher_dbg = SCRIPT_DIR / "launch_d_liner_debug.bat"
    launcher_dbg.write_text(
        LAUNCHER_DEBUG_TEMPLATE.format(python_path=py, main_path=main_path),
        encoding="utf-8",
    )
    logger.ok(f"生成: {launcher_dbg.name}（デバッグ起動・ログ確認用）")


# ---------------------------------------------------------------------------
# 引数
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="D-liner 本体実行用 venv セットアップ")
    p.add_argument("--skip-probe", action="store_true", help="実セッション probe をスキップ")
    p.add_argument("--main", default="main_window.py", help="D-liner エントリポイントのファイル名")
    p.add_argument(
        "--runtime", choices=["auto", "npu", "directml", "cpu"], default="auto",
        help="インストールするランタイム系統。auto（既定）は Intel NPU の有無を"
             "自動検出し、npu（onnxruntime-openvino）か directml"
             "（onnxruntime-directml, GPU向け）かを選ぶ。検出失敗時は cpu"
             "（素の onnxruntime）にフォールバックする。npu/directml/cpu を"
             "指定すると自動検出を上書きしてその系統を強制する。",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    args   = parse_args()
    logger = Logger(LOG_FILE)

    try:
        logger.section("D-liner 本体実行用 venv セットアップ")
        logger.info(f"日時     : {TIMESTAMP}")
        logger.info(f"作業 DIR : {SCRIPT_DIR}")
        logger.info(f"Python   : {sys.executable} ({sys.version.split()[0]})")
        logger.info(f"ログ     : {LOG_FILE}")

        if sys.version_info < (3, 10):
            logger.err(f"Python 3.10 以上が必要です（現在: {sys.version.split()[0]}）")
            sys.exit(1)

        main_path = SCRIPT_DIR / args.main
        if not main_path.exists():
            logger.warn(f"{args.main} が見つかりません: {main_path}")
            logger.warn("ランチャーは生成しますが、パスを後で手動修正してください。")

        # --- ランタイム系統の決定（セッション10） ---
        if args.runtime != "auto":
            track = args.runtime
            logger.section("ランタイム系統")
            logger.info(f"--runtime {args.runtime} が指定されたため自動検出はスキップします。")
        else:
            npu_found = detect_npu(logger)
            if npu_found is True:
                track = "npu"
                logger.ok("→ npu 系統（onnxruntime-openvino + openvino）を選択します。")
            elif npu_found is False:
                track = "directml"
                logger.info("→ NPU非搭載のため directml 系統（onnxruntime-directml）を選択します。")
                logger.info("  GPU（NVIDIA/AMD/Intel iGPU）が使えれば高速化されます。")
            else:
                track = "cpu"
                logger.warn("→ NPU有無を判定できなかったため、安全側の cpu 系統（素の onnxruntime）"
                            "を選択します。")
                logger.warn("  NPU/GPU があるはずの場合は --runtime npu または --runtime directml "
                            "で明示指定してください。")

        # --- VC++ Redistributable 事前チェック（作業指示書対応） ---
        # 誤検知(レジストリキー構造がWindowsバージョンにより異なるケース)を
        # 考慮し、検出失敗・未導入いずれの場合も中断はせず警告のみに留める。
        vcredist_ok = detect_vcredist_x64(logger)
        if vcredist_ok is False:
            logger.warn(
                "続行する場合、onnxruntime の import に失敗する可能性があります。"
                "事前に以下から Microsoft Visual C++ 2015-2022 Redistributable (x64) "
                "を導入することを推奨します:"
            )
            logger.warn("  https://aka.ms/vs/17/release/vc_redist.x64.exe")

        create_venv(VENV_DIR, logger)
        install_packages(VENV_DIR, track, logger)

        # probe には onnx パッケージが必要（probe専用、D-liner本体には不要）
        if not args.skip_probe:
            py = venv_python(VENV_DIR)
            run([py, "-m", "pip", "install", "onnx", "--quiet"], logger, check=False)
            if track == "npu":
                ep_result = probe_openvino(VENV_DIR, logger)
            elif track == "directml":
                ep_result = probe_directml(VENV_DIR, logger)
            else:
                ep_result = "CPUExecutionProvider（cpu系統のためprobe対象外）"
        else:
            logger.section("実セッション probe スキップ（--skip-probe）")
            ep_result = "SKIPPED"

        write_launchers(VENV_DIR, main_path, logger)

        # 選択したランタイム系統をマーカーファイルに残す
        # （将来 d_liner_launcher.py が venv を作り直すかどうかの判断に使う想定）
        marker = VENV_DIR / "runtime_track.txt"
        marker.write_text(f"{track}\n", encoding="utf-8")

        logger.section("セットアップ完了")
        logger.info(f"venv         : {VENV_DIR}")
        logger.info(f"pythonw.exe  : {venv_pythonw(VENV_DIR)}")
        logger.info(f"ランタイム系統: {track}")
        logger.info(f"実行 EP      : {ep_result}")
        logger.info("")
        logger.info("起動方法:")
        logger.info(f"  launch_d_liner.bat をダブルクリック（通常起動）")
        logger.info(f"  launch_d_liner_debug.bat をダブルクリック（コンソール表示・トラブル時）")
        logger.info("")
        if track == "npu":
            logger.info("D-liner の設定画面でタグ付けデバイスを NPU/GPU/CPU から選択してください。")
        elif track == "directml":
            logger.info("D-liner の設定画面でタグ付けデバイスを GPU/CPU から選択してください"
                        "（この venv に NPU 用ランタイムは入っていません）。")
        else:
            logger.info("この venv は CPU 専用です。NPU/GPU を使う場合は "
                        "--runtime npu または --runtime directml で venv を作り直してください。")

    except KeyboardInterrupt:
        logger.warn("中断されました。")
    except Exception as e:
        import traceback
        logger.err(f"予期しないエラー: {e}")
        logger.err(traceback.format_exc())
        sys.exit(1)
    finally:
        logger.close()

    print(f"\nログ: {LOG_FILE}")


if __name__ == "__main__":
    main()
