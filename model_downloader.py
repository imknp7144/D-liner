"""
model_downloader.py — タガーモデル自動ダウンロード用の独立子プロセス
======================================================================
背景（検証機で発生した障害）:
    tagger_engine.py の StandaloneTaggerBackend._try_auto_download() を
    メインプロセス内（main_window.py と同一プロセス）で直接実行して
    いたところ、検証機で HuggingFace からの自動ダウンロード開始直後に
    メインウィンドウごとプロセスが落ちる現象が発生した。

    通常起動（pythonw.exe・コンソール非表示）だけでなく、コンソール表示
    ありのデバッグ起動（launch_d_liner_debug.bat, 素の python.exe）でも
    同様に落ちたことから、Python側の例外（try/exceptで捕まえられるもの）
    ではなく、ネイティブレベルのクラッシュ（例: 検証機のSSL/DLL環境起因）
    である可能性が高いと判断した。D-liner のローカル処理（SQLite /
    PyQt6 / onnxruntime / Pillow）はこの検証機で実績があるのに対し、
    huggingface_hub 経由の HTTPS 通信（requests/urllib3/ssl）だけが
    唯一まだ通っていなかったコードパスだった。

対策:
    ダウンロード処理一式をこの独立スクリプトに切り出し、
    tagger_engine.py 側からは subprocess.run() で子プロセスとして
    起動する。子プロセスがネイティブクラッシュしても、親プロセス
    （main_window.py）は「異常な returncode / タイムアウト」という
    通常のPythonレベルの出来事として検知できるだけで、GUI自体は
    道連れにならず継続動作できる。

呼び出し例:
    python model_downloader.py \
        --repo-id SmilingWolf/wd-eva02-large-tagger-v3 \
        --model-filename model.onnx \
        --tags-filename selected_tags.csv \
        --target-dir /path/to/models/wd14

標準出力の最終行に必ず1行のJSONを出す（親プロセスはこれだけをパースする）:
    {"model_ok": bool, "tags_ok": bool, "error": str|null}

このJSON行が出せないまま終了した場合（クラッシュ・タイムアウト等）は、
親プロセス側で returncode 異常 / JSON パース失敗として扱われ、
「今回はダウンロード失敗」として処理される（再試行はキャッシュ済みの
_failed_downloads 相当の仕組みに委ねる）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _validate_onnx_file(path: Path) -> tuple[bool, str | None]:
    """
    ダウンロードした .onnx ファイルが壊れていないか軽量チェックする。
    tagger_engine.py 側の StandaloneTaggerBackend._validate_onnx_file()
    と同等のロジック（子プロセスを自己完結させるためここに複製。
    ロジックを変更する場合は両方を同期させること）。
    """
    try:
        import onnx
    except ImportError:
        return True, None

    try:
        if path.stat().st_size < 1024:
            return False, "ファイルサイズが小さすぎます（ダウンロード失敗ページの可能性）"
        model = onnx.load(str(path))
        onnx.checker.check_model(model)
        return True, None
    except Exception as e:
        return False, f"ONNX検証失敗: {e}"


def _download_model(dl_repo_id: str, model_filename: str, target_dir: Path) -> tuple[bool, str | None]:
    model_path = target_dir / "model.onnx"
    if model_path.is_file():
        return True, None

    tmp_model_path = target_dir / f".{model_path.name}.downloading"
    try:
        from huggingface_hub import hf_hub_download
        import shutil

        cached_path = hf_hub_download(repo_id=dl_repo_id, filename=model_filename)
        shutil.copy(cached_path, tmp_model_path)

        ok, err = _validate_onnx_file(tmp_model_path)
        if not ok:
            raise ValueError(err or "ダウンロードされたファイルが有効なONNXモデルではありません")

        os.replace(tmp_model_path, model_path)
        return True, None
    except Exception as e:
        for stale in (tmp_model_path, model_path):
            try:
                if stale.is_file():
                    stale.unlink()
            except OSError:
                pass
        return False, str(e)


def _download_tags(dl_repo_id: str, tags_filename: str, target_dir: Path, tags_dest_name: str) -> tuple[bool, str | None]:
    tags_path = target_dir / tags_dest_name
    if tags_path.is_file():
        return True, None

    # バグ修正: 以前は shutil.copy() で最終パス（tags_path）へ直接書き込んで
    # いたため、コピー中に子プロセスがクラッシュ/強制終了すると壊れた
    # （中途半端な）CSVファイルが tags_path にそのまま残ってしまい、以後
    # 「tags_path.is_file() が True」という理由だけで再ダウンロードされずに
    # 壊れたファイルが使われ続けるリスクがあった。_download_model() と
    # 同じ「一時ファイルへコピー → 検証 → os.replace()でatomicに配置」の
    # パターンに揃える。
    tmp_tags_path = target_dir / f".{tags_path.name}.downloading"
    try:
        from huggingface_hub import hf_hub_download
        import shutil

        cached_path = hf_hub_download(repo_id=dl_repo_id, filename=tags_filename)
        shutil.copy(cached_path, tmp_tags_path)
        os.replace(tmp_tags_path, tags_path)
        return True, None
    except Exception as e:
        for stale in (tmp_tags_path, tags_path):
            try:
                if stale.is_file():
                    stale.unlink()
            except OSError:
                pass
        return False, str(e)


def main() -> int:
    parser = argparse.ArgumentParser(description="D-liner タガーモデル自動ダウンロード（子プロセス）")
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--model-filename", required=True)
    parser.add_argument("--tags-filename", required=True)
    parser.add_argument("--tags-dest-name", required=True,
                         help="保存時のタグ定義ファイル名（_SUPPORTED_MODELS[model_id]['tags_filename']）")
    parser.add_argument("--target-dir", required=True)
    args = parser.parse_args()

    target_dir = Path(args.target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # tqdm等がコンソール無し環境で端末幅取得等に失敗しても落ちないよう、
    # 進捗バー描画そのものを無効化しておく（huggingface_hub/tqdm対策）。
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")

    result = {"model_ok": False, "tags_ok": False, "error": None}
    errors: list[str] = []

    try:
        model_ok, model_err = _download_model(args.repo_id, args.model_filename, target_dir)
        result["model_ok"] = model_ok
        if model_err:
            errors.append(f"model: {model_err}")

        tags_ok, tags_err = _download_tags(
            args.repo_id, args.tags_filename, target_dir, args.tags_dest_name
        )
        result["tags_ok"] = tags_ok
        if tags_err:
            errors.append(f"tags: {tags_err}")
    except BaseException as e:  # noqa: BLE001 — 子プロセス内では極力広く拾って必ずJSONを出す
        errors.append(f"unexpected: {e!r}")

    if errors:
        result["error"] = " / ".join(errors)

    # 親プロセスが必ずパースできるよう、最終行として1行JSONを出す。
    print(json.dumps(result), flush=True)
    return 0 if (result["model_ok"] and result["tags_ok"]) else 1


if __name__ == "__main__":
    sys.exit(main())
