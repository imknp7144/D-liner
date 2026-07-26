"""
lora_export_dialog.py — LoRA作成支援ツールへ渡す前の整形用エクスポートダイアログ
======================================================================
セッション27で新規実装。選択画像（または絞り込み結果全体）を新規フォルダへ
「画像コピー＋同名.txtキャプション」としてエクスポートする際の、出力先・
キャプション種別選択画面。

このダイアログ自体が「対象件数・出力先・キャプション種別をまとめて表示
してから実行する」確認画面を兼ねる（ユーザー判断・セッション27）。
既存の file_operation_dialog.py の「クイックアクセス一覧から親フォルダを
選ぶ」体験を踏襲しつつ、こちらは「既存フォルダを選ぶ」のではなく
「親フォルダ＋新規フォルダ名」で新規フォルダを作る点が異なるため、
file_operation_dialog.FileOperationDialog はそのまま流用せず、
クイックアクセス一覧取得関数（get_quick_access_folders）のみ再利用する。
"""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QComboBox,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QListWidget,
    QListWidgetItem,
    QFileDialog,
    QDialogButtonBox,
    QWidget,
    QMessageBox,
)

from file_operation_dialog import get_quick_access_folders

# Windows のファイル/フォルダ名として使えない文字
_INVALID_NAME_CHARS = '<>:"/\\|?*'


class LoraExportDialog(QDialog):
    """LoRA向けエクスポートの出力先・キャプション種別選択ダイアログ。"""

    def __init__(
        self,
        target_count: int,
        target_label: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("LoRA用にエクスポート")
        self.setMinimumWidth(640)
        self.setMinimumHeight(360)

        self._dest_dir: str | None = None

        root = QVBoxLayout(self)

        title = QLabel(f"対象: {target_label}（{target_count} 件）")
        title.setStyleSheet("font-weight: bold;")
        root.addWidget(title)

        info = QLabel(
            "選択した画像を新規フォルダへコピーし、同名の.txtへタグを書き出します。\n"
            "元の画像・DB上のタグは一切変更されません。"
        )
        info.setStyleSheet("color: #888888;")
        info.setWordWrap(True)
        root.addWidget(info)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, stretch=1)

        # --- 左側: 出力先（親フォルダ＋新規フォルダ名）・キャプション種別 ---
        left = QVBoxLayout()
        body.addLayout(left, stretch=3)

        left.addWidget(QLabel("親フォルダ:"))
        parent_row = QHBoxLayout()
        self.parent_combo = QComboBox(self)
        self.parent_combo.setEditable(True)
        parent_row.addWidget(self.parent_combo, stretch=1)
        browse_btn = QPushButton("...", self)
        browse_btn.setFixedWidth(32)
        browse_btn.setToolTip("親フォルダを参照")
        browse_btn.clicked.connect(self._on_browse)
        parent_row.addWidget(browse_btn)
        left.addLayout(parent_row)

        left.addWidget(QLabel("新規フォルダ名:"))
        self.folder_name_input = QLineEdit(self)
        self.folder_name_input.setPlaceholderText("例: lora_export_01")
        left.addWidget(self.folder_name_input)

        left.addSpacing(8)
        left.addWidget(QLabel("キャプション種別:"))
        self.mode_all_radio = QRadioButton("AIタグ + マニュアルタグ", self)
        self.mode_all_radio.setChecked(True)
        self.mode_manual_radio = QRadioButton("マニュアルタグのみ", self)
        left.addWidget(self.mode_all_radio)
        left.addWidget(self.mode_manual_radio)

        left.addStretch(1)

        # --- 右側: クイックアクセス（親フォルダ選択の補助） ---
        right = QVBoxLayout()
        body.addLayout(right, stretch=2)
        right.addWidget(QLabel("⚡ クイックアクセス（親フォルダ選択）"))

        self.quick_list = QListWidget(self)
        self.quick_list.setAlternatingRowColors(True)
        quick_paths = get_quick_access_folders()
        for path in quick_paths:
            name = os.path.basename(path.rstrip("/\\")) or path
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.quick_list.addItem(item)

        if not quick_paths:
            placeholder = QListWidgetItem("(未登録)")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.quick_list.addItem(placeholder)

        self.quick_list.itemClicked.connect(self._on_quick_clicked)
        right.addWidget(self.quick_list, stretch=1)

        # --- OK/Cancel ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.parent_combo.setFocus()

    def _on_browse(self) -> None:
        start = self.parent_combo.currentText().strip() or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "親フォルダを選択", start)
        if d:
            self.parent_combo.setCurrentText(d)

    def _on_quick_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.parent_combo.setCurrentText(path)

    def _on_accept(self) -> None:
        parent_dir = self.parent_combo.currentText().strip()
        folder_name = self.folder_name_input.text().strip()

        if not parent_dir or not os.path.isdir(parent_dir):
            QMessageBox.warning(self, "エラー", "有効な親フォルダを指定してください。")
            return

        if not folder_name:
            QMessageBox.warning(self, "エラー", "新規フォルダ名を入力してください。")
            return

        if folder_name in (".", ".."):
            QMessageBox.warning(
                self, "エラー", "この名前は新規フォルダ名として使用できません。"
            )
            return

        if any(ch in folder_name for ch in _INVALID_NAME_CHARS):
            QMessageBox.warning(
                self, "エラー",
                f"フォルダ名に使用できない文字が含まれています（{_INVALID_NAME_CHARS}）。",
            )
            return

        dest_dir = os.path.normpath(os.path.join(parent_dir, folder_name))

        if os.path.isfile(dest_dir):
            QMessageBox.warning(self, "エラー", "同名のファイルが既に存在します。")
            return

        self._dest_dir = dest_dir
        self.accept()

    def destination_dir(self) -> str | None:
        return self._dest_dir

    def caption_mode(self) -> str:
        return "all" if self.mode_all_radio.isChecked() else "manual_only"
