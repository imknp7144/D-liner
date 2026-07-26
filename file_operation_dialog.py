"""
file_operation_dialog.py — linar風のファイルコピー/移動先選択ダイアログ
=====================================================================
従来は QFileDialog.getExistingDirectory() の素のOSフォルダ選択ダイアログを
使っていたが、linar本家のように「クイックアクセス登録フォルダへワン
クリックで移動/コピーできる」体験に合わせるため、専用ダイアログを用意する。

構成:
    ・現在の位置（送り元）表示
    ・移動/コピー先パスの入力欄（直近の履歴をコンボボックスで選択可能）
    ・「...」ボタンで任意フォルダをOSダイアログから参照
    ・右側にクイックアクセス登録フォルダ一覧（folder_tree.py の
      BookmarkPane と同じ watched_folders.quick_access=1 を参照）。
      シングルクリックで移動先欄にセット、ダブルクリックでそのまま確定。
"""

from __future__ import annotations

import os
import json

from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QComboBox,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QFileDialog,
    QDialogButtonBox,
    QWidget,
    QMessageBox,
)

_HISTORY_KEY_MOVE = "fileop/history_move"
_HISTORY_KEY_COPY = "fileop/history_copy"
_MAX_HISTORY = 10


def _load_history(key: str) -> list[str]:
    """
    移動/コピー先の履歴を読み込む。

    バグ修正: 以前は QSettings.value(key, [], type=list) で直接
    リストとして読み書きしていたが、要素数が1件のときネイティブ
    バックエンド（特にWindowsレジストリ）ではリストと文字列の区別が
    曖昧になり、1件しか履歴が無い状態だと文字列として返ってくることが
    ある（既知のQt/PyQtの落とし穴）。これが起きると呼び出し側の
    `for h in history` がパス文字列を1文字ずつ走査してしまい、
    コンボボックスに文字単位のゴミ項目が並ぶ形で履歴が壊れる。
    JSON文字列として保存/読込することで型のあいまいさを排除する。
    """
    settings = QSettings("D-liner", "D-liner")
    raw = settings.value(key, "", type=str)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(p) for p in data]
    except Exception:
        pass
    return []


def _save_history(key: str, history: list[str]) -> None:
    settings = QSettings("D-liner", "D-liner")
    settings.setValue(key, json.dumps(history[:_MAX_HISTORY]))


def _push_history(key: str, dest: str) -> None:
    """destを履歴の先頭に追加する（重複除去、最大_MAX_HISTORY件に切り詰め）。"""
    history = _load_history(key)
    history = [h for h in history if h != dest]
    history.insert(0, dest)
    _save_history(key, history)


def get_quick_access_folders() -> list[str]:
    """
    watched_folders テーブルから quick_access=1 のフォルダパス一覧を返す。
    folder_tree.py の BookmarkPane と同じ定義に揃えている。
    取得に失敗した場合は空リストを返す（呼び出し側は「未登録」として扱う）。
    """
    try:
        import lifecycle_manager as _lm
        conn = _lm.get_connection()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(watched_folders)").fetchall()]
        if "quick_access" not in cols:
            conn.close()
            return []
        rows = conn.execute(
            "SELECT path FROM watched_folders WHERE quick_access = 1 ORDER BY path"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


class FileOperationDialog(QDialog):
    """linar風「ファイルの移動/コピー」ダイアログ。"""

    def __init__(
        self,
        is_move: bool,
        current_dir: str,
        file_count: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._is_move = is_move
        self._selected_dir: str | None = None
        self._history_key = _HISTORY_KEY_MOVE if is_move else _HISTORY_KEY_COPY

        op_label = "移動" if is_move else "コピー"
        self.setWindowTitle(f"ファイルの{op_label}")
        self.setMinimumWidth(660)
        self.setMinimumHeight(360)

        root = QVBoxLayout(self)

        title = QLabel(
            f"{file_count} 件のファイルを{op_label}します" if file_count != 1
            else f"ファイルを{op_label}します"
        )
        title.setStyleSheet("font-weight: bold;")
        root.addWidget(title)

        cur_label = QLabel(f"現在の位置: {current_dir}")
        cur_label.setStyleSheet("color: #888888;")
        cur_label.setWordWrap(True)
        root.addWidget(cur_label)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, stretch=1)

        # --- 左側: 移動/コピー先パス欄 ---
        left = QVBoxLayout()
        body.addLayout(left, stretch=3)

        left.addWidget(QLabel(f"{op_label}先フォルダ:"))

        path_row = QHBoxLayout()
        self.path_combo = QComboBox(self)
        self.path_combo.setEditable(True)
        history = _load_history(self._history_key)
        for h in history:
            self.path_combo.addItem(h)
        self.path_combo.setCurrentText("")
        path_row.addWidget(self.path_combo, stretch=1)

        browse_btn = QPushButton("...", self)
        browse_btn.setFixedWidth(32)
        browse_btn.setToolTip("フォルダを参照")
        browse_btn.clicked.connect(self._on_browse)
        path_row.addWidget(browse_btn)
        left.addLayout(path_row)

        left.addStretch(1)

        # --- 右側: クイックアクセス一覧 ---
        right = QVBoxLayout()
        body.addLayout(right, stretch=2)
        right.addWidget(QLabel("⚡ クイックアクセス"))

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
            placeholder = QListWidgetItem(
                "(未登録)\nフォルダツリーの右クリックメニューから\n"
                "「クイックアクセスリストに追加(A)...」で追加できます"
            )
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.quick_list.addItem(placeholder)

        self.quick_list.itemClicked.connect(self._on_quick_clicked)
        self.quick_list.itemDoubleClicked.connect(self._on_quick_double_clicked)
        right.addWidget(self.quick_list, stretch=1)

        # --- OK/Cancel ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.path_combo.setFocus()

    def _on_browse(self) -> None:
        start = self.path_combo.currentText().strip() or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "フォルダを選択", start)
        if d:
            self.path_combo.setCurrentText(d)

    def _on_quick_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.path_combo.setCurrentText(path)

    def _on_quick_double_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.path_combo.setCurrentText(path)
            self._on_accept()

    def _on_accept(self) -> None:
        dest = self.path_combo.currentText().strip()
        if not dest or not os.path.isdir(dest):
            QMessageBox.warning(self, "エラー", "有効なフォルダを指定してください。")
            return
        self._selected_dir = dest

        # 履歴更新（重複除去して先頭に追加、最大件数で切り詰め）
        _push_history(self._history_key, dest)

        self.accept()

    def selected_directory(self) -> str | None:
        return self._selected_dir

    @staticmethod
    def get_destination(
        is_move: bool,
        current_dir: str,
        file_count: int = 1,
        parent: QWidget | None = None,
    ) -> str | None:
        """
        QFileDialog.getExistingDirectory() の代替として使う簡易呼び出し。
        キャンセル時は None を返す。
        """
        dlg = FileOperationDialog(is_move, current_dir, file_count, parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.selected_directory()
        return None
