"""
version_info.py — 「D-liner について」ダイアログ用のバージョン情報収集・表示。

目的（セッション35、ユーザー要望）: 実際に動作している venv 内のライブラリ
バージョンと画像形式の対応状況を、毎回 pip コマンドを打たずにアプリ上で確認
できるようにする。システム Python と venv でバージョンが食い違う事故を避ける
ため、必ずアプリ自身の実行環境（sys.executable）から取得する。

設計上の注意:
- onnxruntime 等は import しない（import 時の DLL ロードや OpenVINO のパス設定
  依存で副作用・遅延が出うるため）。importlib.metadata でバージョン文字列のみ
  を読む。
- 取得処理は全項目で個別に try/except し、1項目の失敗でダイアログ全体が
  開かなくなることを避ける。
"""
from __future__ import annotations

import platform
import sys
from importlib import metadata

from PyQt6.QtCore import QT_VERSION_STR, PYQT_VERSION_STR
from PyQt6.QtGui import QFont, QGuiApplication, QImageReader
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QVBoxLayout,
)

# (表示名, pip ディストリビューション名の候補)。先に見つかったものを表示する。
_PACKAGES: list[tuple[str, tuple[str, ...]]] = [
    ("Pillow", ("Pillow", "pillow")),
    ("numpy", ("numpy",)),
    ("onnxruntime", ("onnxruntime", "onnxruntime-openvino",
                     "onnxruntime-directml", "onnxruntime-gpu")),
    ("piexif", ("piexif",)),
    ("send2trash", ("Send2Trash", "send2trash")),
]


def _dist_version(label: str, names: tuple[str, ...]) -> str:
    """候補名のうち最初にインストールされているものを '名前 バージョン' で返す。"""
    found: list[str] = []
    for n in names:
        try:
            ver = metadata.version(n)
            # 表示名と同じ配布名なら重複を避けてバージョンのみ表示
            found.append(ver if n.lower() == label.lower() else f"{n} {ver}")
            # 同系統の別ビルドが共存している場合（openvino と directml 等）に
            # 気付けるよう、見つかったものは全て列挙する（重複名は除外）。
        except metadata.PackageNotFoundError:
            continue
        except Exception as e:  # 壊れたメタデータ等
            found.append(f"{n} (取得失敗: {e})")
    # 大文字小文字違いの同一パッケージ重複を除去
    uniq: list[str] = []
    seen: set[str] = set()
    for f in found:
        key = f.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return " / ".join(uniq) if uniq else "未導入"


def _pillow_feature(name: str) -> str:
    try:
        from PIL import features
        return "対応" if features.check(name) else "非対応"
    except Exception:
        return "判定不可"


def _qt_formats() -> str:
    try:
        fmts = sorted(
            bytes(f).decode("ascii", "ignore").lower()
            for f in QImageReader.supportedImageFormats()
        )
        return ", ".join(fmts) if fmts else "(なし)"
    except Exception as e:
        return f"取得失敗: {e}"


def collect_version_text(app_version: str) -> str:
    """ダイアログ表示・クリップボードコピー用のプレーンテキストを組み立てる。"""
    lines: list[str] = [f"D-liner {app_version}", ""]

    lines.append("[実行環境]")
    lines.append(f"Python      : {platform.python_version()}")
    lines.append(f"実行パス    : {sys.executable}")
    try:
        lines.append(f"OS          : {platform.platform()}")
    except Exception:
        pass
    lines.append(f"PyQt6 / Qt  : {PYQT_VERSION_STR} / {QT_VERSION_STR}")
    lines.append("")

    lines.append("[ライブラリ]")
    for label, names in _PACKAGES:
        lines.append(f"{label:<11} : {_dist_version(label, names)}")
    lines.append("")

    lines.append("[画像形式の対応状況]")
    lines.append(f"Pillow AVIF : {_pillow_feature('avif')}")
    lines.append(f"Pillow WebP : {_pillow_feature('webp')}")
    lines.append(f"Qt 対応形式 : {_qt_formats()}")
    return "\n".join(lines)


def show_about_dialog(parent, app_version: str) -> None:
    """「D-liner について」ダイアログ（バージョン情報つき）を表示する。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle("D-liner について")
    dlg.resize(640, 460)

    layout = QVBoxLayout(dlg)
    head = QLabel(
        f"<b>D-liner {app_version}</b><br>"
        "Danbooru タグ × ファイル名ハイブリッド検索対応<br>"
        "AI生成画像ビューア"
    )
    layout.addWidget(head)

    text = collect_version_text(app_version)
    box = QTextEdit(dlg)
    box.setReadOnly(True)
    box.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
    mono = QFont("Consolas")
    mono.setStyleHint(QFont.StyleHint.Monospace)
    box.setFont(mono)
    box.setPlainText(text)
    layout.addWidget(box, 1)

    row = QHBoxLayout()
    btn_copy = QPushButton("コピー")

    def _copy() -> None:
        QGuiApplication.clipboard().setText(text)
        btn_copy.setText("コピーしました")

    btn_copy.clicked.connect(_copy)
    row.addWidget(btn_copy)
    row.addStretch(1)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, dlg)
    buttons.accepted.connect(dlg.accept)
    row.addWidget(buttons)
    layout.addLayout(row)

    dlg.exec()
