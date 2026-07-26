from __future__ import annotations

import os
import shutil
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QThread
from PyQt6.QtGui import QAction, QFileSystemModel, QFont, QColor
from PyQt6.QtCore import QDir
from PyQt6.QtWidgets import (
    QTreeView,
    QWidget,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QTabWidget,
    QSplitter,
    QPushButton,
    QInputDialog,
    QProgressDialog,
    QApplication,
    QStyledItemDelegate,
)

import lifecycle_manager


class _DragHoverHighlightDelegate(QStyledItemDelegate):
    """
    D&D操作中、ドロップ先候補の行を視覚的にハイライトするための委譲描画。

    バグ修正: 従来 _DropTargetTreeView / _DropTargetListWidget は
    dragMoveEvent() でドロップ可否の判定のみ行い、カーソル下の行を
    視覚的に強調していなかった。そのためD&D中にどのフォルダへ
    ドロップしようとしているのか一見して分かりづらいという指摘があった
    （linarは対象行がハイライトされる。参考SS: nijiuraフォルダへの
    D&D時に選択色でハイライトされる）。
    選択状態（QItemSelectionModel）を書き換えると folder_selected の
    selectionChanged 経由でフォルダ切り替えが誤発火するため、選択とは
    独立した「ドラッグ中ホバー行」を親ビュー側の _drag_hover_index に
    保持し、ここで背景色として描画するだけに留めている。
    """
    def paint(self, painter, option, index) -> None:  # noqa: D102
        hover_index = getattr(self.parent(), "_drag_hover_index", None)
        if hover_index is not None and index == hover_index:
            painter.save()
            palette = self.parent().palette()
            highlight = QColor(palette.color(palette.ColorRole.Highlight))
            highlight.setAlpha(110)
            painter.fillRect(option.rect, highlight)
            painter.setPen(palette.color(palette.ColorRole.Highlight))
            painter.drawRect(option.rect.adjusted(0, 0, -1, -1))
            painter.restore()
        super().paint(painter, option, index)


class _DropTargetTreeView(QTreeView):
    """
    サムネイルビュー等からのファイルドラッグ&ドロップを受け付ける
    QTreeView。ドロップ位置のフォルダを解決し、コピー/移動/キャンセルを
    尋ねるコンテキストメニューを出す（IrfanView/linar 準拠のUX）。
    """
    files_dropped = pyqtSignal(list, str, QPoint, bool)  # (paths, dest_dir, global_pos, is_right_drag)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        # D&Dホバー中の行ハイライト用（選択状態とは独立に保持）
        self._drag_hover_index = None
        self.setItemDelegate(_DragHoverHighlightDelegate(self))

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        if event.mimeData().hasUrls() and index.isValid():
            event.acceptProposedAction()
            if self._drag_hover_index != index:
                self._drag_hover_index = index
                self.viewport().update()
        else:
            event.ignore()
            if self._drag_hover_index is not None:
                self._drag_hover_index = None
                self.viewport().update()

    def dragLeaveEvent(self, event) -> None:
        # バグ修正: ウィジェット外へドラッグが抜けた際にハイライトが
        # 残り続けないよう解除する。
        self._drag_hover_index = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._drag_hover_index = None
        self.viewport().update()
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        if not index.isValid():
            event.ignore()
            return
        model = self.model()
        dest_dir = model.filePath(index)
        if not dest_dir or not os.path.isdir(dest_dir):
            event.ignore()
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            event.ignore()
            return
        # ドラッグ開始側（thumbnail_grid.py の ThumbnailLabel）が
        # 埋め込んだ「どちらのボタンでドラッグを開始したか」を読み取る。
        # ドロップ完了時点ではボタンは既に離されているため、
        # QDropEvent側のボタン状態は当てにできない。
        marker = bytes(event.mimeData().data("application/x-dliner-drag-button"))
        is_right_drag = (marker == b"right")
        event.acceptProposedAction()
        self.files_dropped.emit(paths, dest_dir, self.mapToGlobal(pos), is_right_drag)


class _NoExpandFileSystemModel(QFileSystemModel):
    """
    サブフォルダを持たないフォルダに展開インジケータ（▶三角マーク）を
    表示しないようにした QFileSystemModel サブクラス。

    デフォルトの QFileSystemModel はディレクトリの中身を非同期で取得する前に
    インジケータを描画するため、空フォルダや葉フォルダにも▶が出てしまう。
    hasChildren() をオーバーライドして os.scandir で実際の子ディレクトリ存在を確認する。
    """
    def hasChildren(self, parent=None) -> bool:
        from PyQt6.QtCore import QModelIndex
        if parent is None:
            parent = QModelIndex()
        if not parent.isValid():
            return super().hasChildren(parent)
        path = self.filePath(parent)
        if not path:
            return super().hasChildren(parent)
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        return True
            return False
        except PermissionError:
            return super().hasChildren(parent)


class ScanWorker(QThread):
    """
    フォルダスキャン＆登録をバックグラウンドで実行するWorker。
    メインスレッドをブロックしてフリーズするのを防ぐ。
    """
    progress = pyqtSignal(str, int, int)   # (message, current, total)
    finished = pyqtSignal(dict)            # {"added", "recovered", "skipped", "folders"}
    error    = pyqtSignal(str)

    def __init__(self, path: str, recursive: bool, parent=None):
        super().__init__(parent)
        self.path      = path
        self.recursive = recursive

    def run(self) -> None:
        from pathlib import Path
        try:
            conn = lifecycle_manager.get_connection()
            lifecycle_manager.ensure_schema(conn)
            total_count = {"added": 0, "recovered": 0, "skipped": 0}

            if self.recursive:
                all_dirs = [Path(self.path)] + sorted(
                    p for p in Path(self.path).rglob("*") if p.is_dir()
                )
            else:
                all_dirs = [Path(self.path)]

            n = len(all_dirs)
            for i, d in enumerate(all_dirs):
                if self.isInterruptionRequested():
                    break
                self.progress.emit(
                    f"スキャン中: {d.name}  ({i+1}/{n})", i + 1, n
                )
                d_str = d.as_posix()  # Windowsバックスラッシュ→スラッシュ統一（日本語パス対応）
                res = lifecycle_manager.scan_folder(conn, d_str, recursive=False)
                lifecycle_manager.add_watched_folder(
                    conn, d_str, recursive=False, watch_mode="startup_check"
                )
                total_count["added"]     += res.get("added", 0)
                total_count["recovered"] += res.get("recovered", 0)
                total_count["skipped"]   += res.get("skipped", 0)

            conn.close()
            total_count["folders"] = n
            self.finished.emit(total_count)
        except Exception as e:
            self.error.emit(str(e))


class TagListPane(QWidget):
    """
    現在の検索結果に含まれる画像のタグを集計して一覧表示するペイン。

    カテゴリ別にグループ化（manual > character > copyright > general > artist > rating）。
    クリックで検索バーにタグを追記。
    manual（手動追加タグ）は指示書02タスクBで追加。meta/year は元々除外設計
    （検索結果集計ではノイズになりやすいため）であり、変更していない。

    Grabber / Danbooru 準拠の配色（ダークテーマ向けに調整）:
      manual     金  #ffd54f  (tag_panel.py TagPanel と統一)
      character  緑  #82d982
      copyright  紫  #c797ff
      general    青  #8eb4e3
      artist     赤  #f28383
      rating   黄緑  #a8d8a8
    """

    tag_clicked = pyqtSignal(str)   # アンダースコア形式のタグ名

    # manual は指示書02タスクBに基づき先頭固定。meta/year は元々このペインでは
    # 除外設計（検索結果集計という性質上、ノイズになりやすいため）であり、
    # 今回もその方針は維持する。
    CATEGORY_ORDER = ["manual", "character", "copyright", "general", "artist", "rating"]
    CATEGORY_LABELS = {
        "manual":    "手動",
        "character": "キャラクター",
        "copyright": "版権・作品",
        "general":   "一般",
        "artist":    "アーティスト",
        "rating":    "レーティング",
    }
    CATEGORY_COLORS = {
        # tag_panel.py の TagPanel.CATEGORY_COLORS と同色に統一
        "manual":    "#ffd54f",
        "character": "#82d982",
        "copyright": "#c797ff",
        "general":   "#8eb4e3",
        "artist":    "#f28383",
        "rating":    "#a8d8a8",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._worker = None
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # タグ絞り込みフィルタ入力
        filter_bar = QWidget(self)
        filter_bar.setStyleSheet("background-color: palette(mid);")
        fl = QHBoxLayout(filter_bar)
        fl.setContentsMargins(4, 3, 4, 3)
        fl.setSpacing(4)
        self._filter_input = QLineEdit(filter_bar)
        self._filter_input.setPlaceholderText("タグを絞り込み...")
        self._filter_input.setClearButtonEnabled(True)
        self._filter_input.textChanged.connect(self._apply_filter)
        fl.addWidget(self._filter_input)
        layout.addWidget(filter_bar)

        # タグリスト本体
        self.list_widget = QListWidget(self)
        self.list_widget.setStyleSheet("""
            QListWidget {
                border: none;
                font-size: 12px;
            }
            QListWidget::item {
                padding: 2px 6px;
            }
            QListWidget::item:hover {
                background-color: rgba(255,255,255,20);
            }
            QListWidget::item:selected {
                background-color: palette(highlight);
                color: palette(highlighted-text);
            }
        """)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list_widget)

        # 件数ラベル
        self._count_label = QLabel("", self)
        self._count_label.setStyleSheet(
            "color: palette(mid); font-size: 10px; padding: 2px 6px;"
        )
        layout.addWidget(self._count_label)

        # 内部キャッシュ（フィルタ用）
        self._all_items: list[tuple] = []   # (tag, category, count)

    # ------------------------------------------------------------------
    # 外部インターフェース
    # ------------------------------------------------------------------

    def update_for_results(self, search_results: list) -> None:
        """
        検索結果 [(id, path, w, h, size), ...] を受け取りタグを集計する。
        DB未登録画像（id < 0）はスキップ。
        """
        image_ids = [img_id for img_id, *_ in search_results if img_id >= 0]

        # 実行中のWorkerを中断
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.quit()
            self._worker = None

        if not image_ids:
            self._all_items = []
            self.list_widget.clear()
            self._count_label.setText("")
            return

        from workers import TagListWorker
        self._worker = TagListWorker(image_ids, parent=self)
        self._worker.finished.connect(self._on_tags_fetched)
        self._worker.error.connect(lambda e: self._count_label.setText(f"エラー: {e}"))
        self._worker.start()

    # ------------------------------------------------------------------
    # 内部スロット
    # ------------------------------------------------------------------

    def _on_tags_fetched(self, tags: list) -> None:
        """TagListWorker からの結果を受け取って表示する。"""
        self._all_items = tags   # [(tag, category, count)]
        self._filter_input.clear()
        self._render(tags)

    def _apply_filter(self, text: str) -> None:
        """フィルタ入力の変化に応じてリストを絞り込む。"""
        if not text:
            self._render(self._all_items)
            return
        text_lower = text.lower()
        filtered = [
            (tag, cat, cnt) for tag, cat, cnt in self._all_items
            if text_lower in tag.lower().replace("_", " ")
        ]
        self._render(filtered)

    def _render(self, items: list) -> None:
        """カテゴリ別グループ + ヘッダー行でリストを再構築する。"""
        self.list_widget.clear()

        # カテゴリ別に分類（順序維持）
        by_cat: dict[str, list] = {c: [] for c in self.CATEGORY_ORDER}
        for tag, cat, count in items:
            if cat in by_cat:
                by_cat[cat].append((tag, count))
            # 未知カテゴリは無視（meta/year は除外済み）

        total_tags = 0
        for cat in self.CATEGORY_ORDER:
            cat_items = by_cat[cat]
            if not cat_items:
                continue

            # カテゴリヘッダー行（クリック不可）
            label = self.CATEGORY_LABELS.get(cat, cat)
            header = QListWidgetItem(f"  {label}")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setForeground(QColor("#888888"))
            header_font = header.font()
            header_font.setBold(True)
            header_font.setPointSize(header_font.pointSize() - 1)
            header.setFont(header_font)
            self.list_widget.addItem(header)

            # タグ行
            color = QColor(self.CATEGORY_COLORS.get(cat, "#cccccc"))
            for tag, count in cat_items:
                display = tag.replace("_", " ")
                item = QListWidgetItem(f"    {display}  ({count})")
                item.setData(Qt.ItemDataRole.UserRole, tag)   # アンダースコア形式を保持
                item.setForeground(color)
                item.setToolTip(f"{tag}\n出現: {count} 件")
                self.list_widget.addItem(item)
                total_tags += 1

        self._count_label.setText(f"{total_tags} タグ")

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        tag = item.data(Qt.ItemDataRole.UserRole)
        if tag:
            self.tag_clicked.emit(tag)


class _DropTargetListWidget(QListWidget):
    """
    サムネイルビュー等からのファイルドラッグ&ドロップを受け付ける
    QListWidget。BookmarkPane の「お気に入り」「登録フォルダ」一覧に
    使う。ロジックは _DropTargetTreeView と同一（項目のパスは
    Qt.ItemDataRole.UserRole から取得する点のみ異なる）。
    """
    files_dropped = pyqtSignal(list, str, QPoint, bool)  # (paths, dest_dir, global_pos, is_right_drag)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        # D&Dホバー中の行ハイライト用（選択状態とは独立に保持）
        self._drag_hover_index = None
        self.setItemDelegate(_DragHoverHighlightDelegate(self))

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        valid = item is not None and bool(item.data(Qt.ItemDataRole.UserRole))
        if event.mimeData().hasUrls() and valid:
            event.acceptProposedAction()
            index = self.indexFromItem(item)
            if self._drag_hover_index != index:
                self._drag_hover_index = index
                self.viewport().update()
        else:
            event.ignore()
            if self._drag_hover_index is not None:
                self._drag_hover_index = None
                self.viewport().update()

    def dragLeaveEvent(self, event) -> None:
        # バグ修正: ウィジェット外へドラッグが抜けた際にハイライトが
        # 残り続けないよう解除する。
        self._drag_hover_index = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._drag_hover_index = None
        self.viewport().update()
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        if item is None:
            event.ignore()
            return
        dest_dir = item.data(Qt.ItemDataRole.UserRole)
        if not dest_dir or not os.path.isdir(dest_dir):
            event.ignore()
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            event.ignore()
            return
        marker = bytes(event.mimeData().data("application/x-dliner-drag-button"))
        is_right_drag = (marker == b"right")
        event.acceptProposedAction()
        self.files_dropped.emit(paths, dest_dir, self.mapToGlobal(pos), is_right_drag)


class BookmarkPane(QWidget):
    """
    左ペイン上部のフォルダショートカットペイン。2セクション構成:

    ★ 登録フォルダ  … watched_folders で watch_mode != 'none' のもの（DBスキャン済み）
    ⚡ クイックアクセス … watched_folders で quick_access = 1 のもの（ショートカットのみ）

    両者は独立した QListWidget で表示し、混在しない。
    """
    folder_selected  = pyqtSignal(str)   # クリックされたフォルダパス
    remove_requested = pyqtSignal(str)   # 右クリック→登録解除
    files_dropped = pyqtSignal(list, str, QPoint, bool)  # サムネイルビュー等からのD&D

    _ITEM_REGISTERED  = "registered"
    _ITEM_QUICKACCESS = "quickaccess"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._init_ui()

    def _make_section_header(self, title: str) -> QWidget:
        header = QWidget(self)
        header.setStyleSheet("background-color: palette(mid);")
        h = QHBoxLayout(header)
        h.setContentsMargins(6, 2, 6, 2)
        lbl = QLabel(title, header)
        font = lbl.font()
        font.setBold(True)
        lbl.setFont(font)
        h.addWidget(lbl)
        h.addStretch()
        return header

    def _make_list(self) -> QListWidget:
        lw = _DropTargetListWidget(self)
        lw.setStyleSheet(
            "QListWidget { border: none; }"
            "QListWidget::item { padding: 3px 6px; }"
            "QListWidget::item:hover { background-color: palette(highlight);"
            "  color: palette(highlighted-text); }"
            "QListWidget::item:selected { background-color: palette(highlight);"
            "  color: palette(highlighted-text); }"
        )
        lw.itemClicked.connect(self._on_item_clicked)
        lw.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lw.files_dropped.connect(self.files_dropped)
        return lw

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # タブ: [お気に入り（クイックアクセス）] [登録フォルダ]
        # XnView準拠: クイックアクセスがデフォルトタブ
        self.inner_tabs = QTabWidget(self)
        self.inner_tabs.setDocumentMode(True)
        self.inner_tabs.setStyleSheet("""
            QTabBar::tab { padding: 3px 8px; font-size: 11px; }
            QTabBar::tab:selected { font-weight: bold; }
        """)

        # ---- タブ0: ⚡ お気に入り（クイックアクセス）----
        qa_container = QWidget()
        qa_layout = QVBoxLayout(qa_container)
        qa_layout.setContentsMargins(0, 0, 0, 0)
        qa_layout.setSpacing(0)
        self.list_quickaccess = self._make_list()
        self.list_quickaccess.customContextMenuRequested.connect(
            lambda pos: self._on_context_menu(pos, self.list_quickaccess, self._ITEM_QUICKACCESS)
        )
        qa_layout.addWidget(self.list_quickaccess)
        self.inner_tabs.addTab(qa_container, "お気に入り")

        # ---- タブ1: ★ 登録フォルダ ----
        reg_container = QWidget()
        reg_layout = QVBoxLayout(reg_container)
        reg_layout.setContentsMargins(0, 0, 0, 0)
        reg_layout.setSpacing(0)
        # リフレッシュボタン付きヘッダー
        hdr = QWidget()
        hdr.setStyleSheet("background-color: palette(mid);")
        hdr_l = QHBoxLayout(hdr)
        hdr_l.setContentsMargins(4, 2, 4, 2)
        hdr_l.addStretch()
        refresh_btn = QPushButton("↺")
        refresh_btn.setFixedSize(20, 20)
        refresh_btn.setFlat(True)
        refresh_btn.setToolTip("リスト更新")
        refresh_btn.clicked.connect(self.refresh)
        hdr_l.addWidget(refresh_btn)
        reg_layout.addWidget(hdr)
        self.list_registered = self._make_list()
        self.list_registered.customContextMenuRequested.connect(
            lambda pos: self._on_context_menu(pos, self.list_registered, self._ITEM_REGISTERED)
        )
        reg_layout.addWidget(self.list_registered)
        self.inner_tabs.addTab(reg_container, "登録フォルダ")

        # ---- タブ2: タグ一覧 ----
        self.tag_list_pane = TagListPane(self)
        self.inner_tabs.addTab(self.tag_list_pane, "タグ一覧")

        layout.addWidget(self.inner_tabs)

    def refresh(self) -> None:
        """DBから再読み込みして2つのリストを更新"""
        self.list_registered.clear()
        self.list_quickaccess.clear()
        try:
            conn = lifecycle_manager.get_connection()
            # quick_access 列がなければ追加
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(watched_folders)").fetchall()]
            if "quick_access" not in cols:
                conn.execute(
                    "ALTER TABLE watched_folders ADD COLUMN quick_access INTEGER DEFAULT 0")
                conn.commit()
            rows = conn.execute(
                "SELECT path, watch_mode, quick_access FROM watched_folders ORDER BY path"
            ).fetchall()
            conn.close()
        except Exception:
            return

        for path, watch_mode, quick_access in rows:
            name = os.path.basename(path) or path
            # ★ 登録フォルダ: スキャン済み（watch_mode が none 以外）
            if watch_mode and watch_mode != "none":
                item = QListWidgetItem(f"★  {name}")
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setToolTip(path)
                self.list_registered.addItem(item)
            # ⚡ クイックアクセス: quick_access フラグが立っているもの
            if quick_access:
                item = QListWidgetItem(f"⚡  {name}")
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setToolTip(path)
                self.list_quickaccess.addItem(item)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.folder_selected.emit(path)

    def _on_context_menu(self, pos: QPoint,
                         list_widget: "QListWidget",
                         section: str) -> None:
        item = list_widget.itemAt(pos)
        if not item:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        name = os.path.basename(path) or path
        menu = QMenu(self)

        open_action = QAction(f"「{name}」をエクスプローラーで開く", self)
        open_action.triggered.connect(lambda: os.startfile(path))
        menu.addAction(open_action)
        menu.addSeparator()

        if section == self._ITEM_REGISTERED:
            unreg_action = QAction("DB登録解除", self)
            unreg_action.triggered.connect(lambda: self.remove_requested.emit(path))
            menu.addAction(unreg_action)
        else:
            # クイックアクセスからの削除（DBスキャン登録は維持）
            qa_remove = QAction("クイックアクセスから削除", self)
            qa_remove.triggered.connect(lambda: self._remove_quick_access(path))
            menu.addAction(qa_remove)

        menu.exec(list_widget.mapToGlobal(pos))

    def _remove_quick_access(self, path: str) -> None:
        try:
            conn = lifecycle_manager.get_connection()
            conn.execute(
                "UPDATE watched_folders SET quick_access = 0 WHERE path = ?", (path,)
            )
            # watch_mode が none かつ quick_access=0 になったエントリは不要なので削除
            conn.execute(
                "DELETE FROM watched_folders WHERE path = ? AND watch_mode = 'none'"
                " AND (quick_access = 0 OR quick_access IS NULL)",
                (path,)
            )
            conn.commit()
            conn.close()
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"クイックアクセスの削除に失敗しました:\n{e}")

    # 旧APIとの互換性維持（QListWidgetを1つに見せる参照）
    @property
    def list_widget(self) -> "QListWidget":
        return self.list_registered


class FolderTreeWidget(QWidget):
    """
    左ペイン全体: 上部に登録フォルダペイン（BookmarkPane）、
    下部にOSフォルダツリー（QFileSystemModel）を配置。
    """
    folder_selected = pyqtSignal(str)   # 選択されたフォルダパスを通知
    scan_requested  = pyqtSignal(str)   # スキャン/登録解除後の再検索要求
    files_operation_done = pyqtSignal()  # D&Dによるコピー/移動完了通知
    folder_unwatched = pyqtSignal(str)   # 登録解除されたフォルダパス（バグ修正: タスクA）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # XnView準拠レイアウト:
        #   上ペイン: BookmarkPane（お気に入り/登録フォルダ タブ切り替え）
        #   下ペイン: OSフォルダツリー
        #   → QSplitter で高さを自由に調整可能
        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.setChildrenCollapsible(False)

        # --- 上ペイン: BookmarkPane（タブ内部で お気に入り/登録フォルダ切り替え）---
        self.bookmark_pane = BookmarkPane(self)
        self.bookmark_pane.folder_selected.connect(self._on_bookmark_selected)
        self.bookmark_pane.remove_requested.connect(self._unregister_folder)
        # やり残し対応: お気に入り/登録フォルダ一覧へのD&Dもツリービューと
        # 同じロジック（左ドラッグ=即コピー、右ドラッグ=コピー/移動選択
        # メニュー）で処理する。
        self.bookmark_pane.files_dropped.connect(self._on_files_dropped_on_tree)
        splitter.addWidget(self.bookmark_pane)

        # --- 下ペイン: ヘッダー「フォルダー」＋OSフォルダツリー ---
        tree_container = QWidget()
        tree_layout = QVBoxLayout(tree_container)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(0)

        # フォルダーヘッダー
        tree_header = QWidget()
        tree_header.setStyleSheet("background-color: palette(mid);")
        th_layout = QHBoxLayout(tree_header)
        th_layout.setContentsMargins(6, 3, 6, 3)
        tree_title = QLabel("フォルダー")
        font = tree_title.font()
        font.setBold(True)
        tree_title.setFont(font)
        th_layout.addWidget(tree_title)
        tree_layout.addWidget(tree_header)

        self.tree_view = _DropTargetTreeView(tree_container)
        self.tree_view.files_dropped.connect(self._on_files_dropped_on_tree)
        self.model_fs = _NoExpandFileSystemModel(self)
        self.model_fs.setRootPath("")
        self.model_fs.setFilter(
            QDir.Filter.AllDirs |
            QDir.Filter.NoDotAndDotDot |
            QDir.Filter.Drives
        )
        self.tree_view.setModel(self.model_fs)
        for col in range(1, self.model_fs.columnCount()):
            self.tree_view.setColumnHidden(col, True)
        self.tree_view.setHeaderHidden(True)
        self.tree_view.setAnimated(True)
        self.tree_view.selectionModel().selectionChanged.connect(self._on_tree_selection_changed)
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self._on_tree_context_menu)
        tree_layout.addWidget(self.tree_view)

        splitter.addWidget(tree_container)

        # 上(お気に入り): 40% / 下(フォルダー): 60% をデフォルトに
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        layout.addWidget(splitter)
        self.bookmark_pane.refresh()

    # --- ブックマークペイン ---
    def _on_bookmark_selected(self, path: str) -> None:
        self.folder_selected.emit(path)
        # ツリー側も追随
        self.select_path(path)

    def _unregister_folder(self, path: str) -> None:
        name = os.path.basename(path) or path
        reply = QMessageBox.question(
            self, "登録解除",
            f"「{name}」をDB管理から外しますか?\n（画像ファイルは削除されません）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                conn = lifecycle_manager.get_connection()
                lifecycle_manager.remove_watched_folder(conn, path)
                conn.close()
                self.bookmark_pane.refresh()
                self.scan_requested.emit(path)
                # バグ修正(タスクA): 登録解除をmain_window側に通知し、
                # 進行中のバックグラウンドタグ付けを中断できるようにする
                self.folder_unwatched.emit(path)
            except Exception as e:
                QMessageBox.critical(self, "エラー", f"登録解除に失敗しました:\n{e}")

    # --- ツリービュー ---
    def _on_files_dropped_on_tree(
        self, paths: list[str], dest_dir: str, global_pos: QPoint, is_right_drag: bool
    ) -> None:
        """
        サムネイルビュー等からドラッグされたファイルをフォルダツリーへ
        ドロップした際のハンドラ。

        バグ修正:
        - 右ドラッグでのD&Dがそもそも開始されていなかった（drag元の
          ThumbnailLabel が左ボタンでしかドラッグを開始していなかった）
          ため、右ドラッグでの「コピー/移動」選択メニューが機能して
          いなかった。ドラッグ側の修正と合わせて、ここでは
          is_right_drag に応じて分岐する。
        - 右ドラッグ時にメニューをこの場（dropEvent由来のコールバック）で
          同期的に開くと、Windows上ではOLEドラッグ&ドロップのネイティブ
          セッションがまだ完全に終わっていないタイミングでモーダルな
          QMenu を入れ子で回すことになり、メニューが正しく表示されずに
          既定動作（コピー）へ流れてしまうことがある。QTimer.singleShot
          でイベントループに一度戻してから開くことで回避する。
        """
        if is_right_drag:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(
                0, lambda: self._show_drop_menu(paths, dest_dir, global_pos)
            )
        else:
            # 左ドラッグはExplorer同様の既定動作としてコピーを実行
            # （移動したい場合は右ドラッグでメニューから選ぶ）
            self._execute_file_drop(paths, dest_dir, is_move=False)

    def _show_drop_menu(self, paths: list[str], dest_dir: str, global_pos: QPoint) -> None:
        menu = QMenu(self)
        copy_act = menu.addAction(f"ここにコピー(&C)  [{len(paths)}件]")
        move_act = menu.addAction(f"ここに移動(&M)  [{len(paths)}件]")
        menu.addSeparator()
        menu.addAction("キャンセル(&A)")
        chosen = menu.exec(global_pos)
        if chosen is None or chosen not in (copy_act, move_act):
            return
        self._execute_file_drop(paths, dest_dir, is_move=(chosen is move_act))

    def _execute_file_drop(self, paths: list[str], dest_dir: str, is_move: bool) -> None:
        """ドロップされたファイル群をdest_dirへコピー/移動し、DBとメインウィンドウへ反映する。"""
        errors: list[str] = []
        ok_count = 0
        norm_dest_dir = dest_dir.replace("\\", "/").rstrip("/")

        for src in paths:
            fname = os.path.basename(src)
            dest = os.path.join(dest_dir, fname)
            try:
                if os.path.abspath(os.path.dirname(src)) == os.path.abspath(dest_dir):
                    continue  # 同一フォルダへのドロップは無視
                if os.path.exists(dest):
                    reply = QMessageBox.question(
                        self, "確認",
                        f"「{fname}」は{'移動' if is_move else 'コピー'}先に既に存在します。上書きしますか?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        continue
                if is_move:
                    shutil.move(src, dest)
                    self._sync_db_move(src, dest)
                else:
                    shutil.copy2(src, dest)
                ok_count += 1
            except Exception as e:
                errors.append(f"{fname}: {e}")

        if ok_count:
            # コピー/移動先フォルダが登録済み監視フォルダの傘下であれば
            # 差分スキャンして新規ファイルをDBに反映しておく
            try:
                conn = lifecycle_manager.get_connection()
                if lifecycle_manager.is_watched_path(conn, norm_dest_dir):
                    lifecycle_manager.scan_folder(conn, norm_dest_dir, recursive=False)
                conn.close()
            except Exception:
                pass
            self.files_operation_done.emit()

        if errors:
            op = "移動" if is_move else "コピー"
            QMessageBox.critical(self, "エラー", f"一部のファイルで{op}に失敗しました:\n" + "\n".join(errors))

    def _sync_db_move(self, old_path: str, new_path: str) -> None:
        """ファイル移動に伴うDBのpath更新（該当レコードが存在すれば）。"""
        norm_old = old_path.replace("\\", "/")
        norm_new = new_path.replace("\\", "/")
        try:
            conn = lifecycle_manager.get_connection()
            conn.execute(
                "UPDATE images SET path = ?, status = 'ACTIVE' WHERE path = ?",
                (norm_new, norm_old),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _on_tree_selection_changed(self) -> None:
        index = self.tree_view.currentIndex()
        if index.isValid():
            path = self.model_fs.filePath(index)
            self.folder_selected.emit(path)

    def select_path(self, path: str) -> None:
        """外部から指定されたフォルダを選択状態にし展開する"""
        if not path or not os.path.exists(path):
            return
        normalized = os.path.abspath(path)
        index = self.model_fs.index(normalized)
        if index.isValid():
            self.tree_view.setCurrentIndex(index)
            self.tree_view.scrollTo(index)
            self.tree_view.expand(index)

    def _on_tree_context_menu(self, pos: QPoint) -> None:
        index = self.tree_view.indexAt(pos)
        if not index.isValid():
            return

        dir_path = self.model_fs.filePath(index)
        dir_name = os.path.basename(dir_path) or dir_path

        try:
            conn = lifecycle_manager.get_connection()
            norm = dir_path.replace("\\", "/").rstrip("/")
            # quick_access のみ（watch_mode='none'）は「DB登録済み」扱いしない
            row = conn.execute(
                "SELECT watch_mode FROM watched_folders WHERE path = ?", (norm,)
            ).fetchone()
            is_reg = bool(row and row[0] and row[0] != "none")
            conn.close()
        except Exception:
            is_reg = False

        menu = QMenu(self)

        # --- エクスプローラーで開く ---
        open_action = QAction(f"{dir_path} を開く", self)
        open_action.triggered.connect(lambda: os.startfile(dir_path))
        menu.addAction(open_action)

        menu.addSeparator()

        # --- フォルダ作成 (K) ---
        mkdir_action = QAction("フォルダの作成(K)", self)
        mkdir_action.setShortcut("K")
        mkdir_action.triggered.connect(lambda: self._create_folder(dir_path))
        menu.addAction(mkdir_action)

        # --- フォルダ名前変更 (R) ---
        rename_action = QAction("フォルダの名前変更(R)", self)
        rename_action.triggered.connect(lambda: self._rename_folder(dir_path))
        menu.addAction(rename_action)

        menu.addSeparator()

        # --- DB登録/解除 ---
        if is_reg:
            unreg_action = QAction("★ DB登録済み　─　登録解除", self)
            unreg_action.triggered.connect(lambda: self._unregister_folder(dir_path))
            menu.addAction(unreg_action)
        else:
            reg_action = QAction("このフォルダをスキャンして登録...", self)
            reg_action.triggered.connect(lambda: self._scan_and_register(dir_path))
            menu.addAction(reg_action)

        menu.addSeparator()

        # --- クイックアクセス（ブックマーク）に追加 ---
        qa_action = QAction("クイックアクセスリストに追加(A)...", self)
        qa_action.triggered.connect(lambda: self._add_to_quick_access(dir_path))
        menu.addAction(qa_action)

        menu.exec(self.tree_view.mapToGlobal(pos))

    def _create_folder(self, parent_path: str) -> None:
        """指定フォルダ直下に新しいフォルダを作成する"""
        name, ok = QInputDialog.getText(
            self, "フォルダの作成",
            f"「{os.path.basename(parent_path)}」に作成するフォルダ名を入力してください:"
        )
        if not ok or not name.strip():
            return
        new_path = os.path.join(parent_path, name.strip())
        try:
            os.makedirs(new_path, exist_ok=False)
            # ツリーを更新して新フォルダを選択
            self.model_fs.setRootPath(self.model_fs.rootPath())  # リフレッシュ
            index = self.model_fs.index(new_path)
            if index.isValid():
                # バグ修正: setCurrentIndex() は selectionModel().selectionChanged
                # を発火させ、_on_tree_selection_changed() 経由で folder_selected
                # が飛び、サムネイルグリッドの表示先が新規フォルダへ切り替わって
                # しまっていた（一般的なファイラーの「新規フォルダ作成後は
                # ハイライトのみ、表示は現在のフォルダのまま」という挙動と異なり、
                # 戻る操作が余計に必要になっていた）。selectionModel の
                # シグナルを一時的にブロックしてから setCurrentIndex() する
                # ことで、ツリー上のハイライト（フォーカス）だけを更新し、
                # フォルダ切り替え（folder_selected）は発火させない。
                self.tree_view.selectionModel().blockSignals(True)
                self.tree_view.setCurrentIndex(index)
                self.tree_view.selectionModel().blockSignals(False)
                self.tree_view.scrollTo(index)
            # サムネイルグリッド側のフォルダ行（img_id == -2）は別経路
            # （検索/再スキャン時にworkers.py側でサブフォルダ一覧として
            # 都度生成）のため、ツリーを更新しただけでは反映されない。
            # scan_requested を親フォルダパスで発火し、既存の
            # _on_folder_scan_requested()（現在表示中フォルダと一致する
            # 場合のみ trigger_search() する判定を既に持つ）に委ねる。
            # これにより、F8（現フォルダ直下に作成）の場合だけグリッドが
            # 自動更新され、コンテキストメニューから別フォルダの配下に
            # 作成した場合は今の表示に影響しない。
            self.scan_requested.emit(parent_path)
        except FileExistsError:
            QMessageBox.warning(self, "エラー", f"「{name}」は既に存在します。")
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"フォルダ作成に失敗しました:\n{e}")

    def _rename_folder(self, path: str) -> None:
        """フォルダ名前変更"""
        old_name = os.path.basename(path)
        parent_dir = os.path.dirname(path)
        new_name, ok = QInputDialog.getText(
            self, "フォルダの名前変更",
            "新しいフォルダ名を入力してください:",
            text=old_name
        )
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        new_path = os.path.join(parent_dir, new_name.strip())
        try:
            os.rename(path, new_path)
            # ツリーを新パスに移動
            self.model_fs.setRootPath(self.model_fs.rootPath())
            index = self.model_fs.index(new_path)
            if index.isValid():
                self.tree_view.setCurrentIndex(index)
                self.tree_view.scrollTo(index)
            # DBにパスが登録されていれば更新
            try:
                conn = lifecycle_manager.get_connection()
                conn.execute(
                    "UPDATE watched_folders SET path = ? WHERE path = ?",
                    (new_path.replace("\\", "/"), path.replace("\\", "/"))
                )
                conn.execute(
                    "UPDATE images SET path = REPLACE(path, ?, ?) WHERE path LIKE ?",
                    (path.replace("\\", "/"), new_path.replace("\\", "/"),
                     path.replace("\\", "/") + "%")
                )
                conn.commit()
                conn.close()
                self.bookmark_pane.refresh()
                self.scan_requested.emit(new_path)
            except Exception:
                pass  # DB更新失敗はUI操作に影響させない
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"名前変更に失敗しました:\n{e}")

    def _add_to_quick_access(self, path: str) -> None:
        """
        クイックアクセスに指定フォルダ1つだけを登録する。
        ・サブフォルダは一切登録しない
        ・DBスキャン（watched_folders の scan/watch）は行わない
        ・既に登録済み（watched_folders に存在）なら quick_access フラグを立てるだけ
        """
        try:
            conn = lifecycle_manager.get_connection()
            lifecycle_manager.ensure_schema(conn)
            norm = path.replace("\\", "/").rstrip("/")
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(watched_folders)").fetchall()]
            if "quick_access" not in cols:
                conn.execute(
                    "ALTER TABLE watched_folders ADD COLUMN quick_access INTEGER DEFAULT 0")
            row = conn.execute(
                "SELECT id, quick_access FROM watched_folders WHERE path = ?", (norm,)
            ).fetchone()
            if row:
                if row[1]:  # 既にクイックアクセス登録済み
                    QMessageBox.information(
                        self, "クイックアクセス",
                        f"「{os.path.basename(path)}」は既にクイックアクセスに登録されています。"
                    )
                    conn.close()
                    return
                conn.execute(
                    "UPDATE watched_folders SET quick_access = 1 WHERE path = ?", (norm,))
            else:
                # このフォルダのみ、watch_mode='none'（スキャンなし）でエントリ追加
                conn.execute(
                    "INSERT INTO watched_folders (path, recursive, watch_mode, quick_access) "
                    "VALUES (?, 0, 'none', 1)",
                    (norm,)
                )
            conn.commit()
            conn.close()
            self.bookmark_pane.refresh()
            QMessageBox.information(
                self, "クイックアクセス",
                f"「{os.path.basename(path)}」をクイックアクセスに追加しました。"
            )
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"クイックアクセスへの追加に失敗しました:\n{e}")

    def _scan_and_register(self, path: str) -> None:
        from pathlib import Path as _Path
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox, QRadioButton

        # サブフォルダが存在するか確認 → 登録範囲ダイアログ
        subdirs = [p for p in _Path(path).iterdir() if p.is_dir()]
        recursive = False
        if subdirs:
            dlg = QDialog(self)
            dlg.setWindowTitle("登録範囲の選択")
            layout = QVBoxLayout(dlg)
            layout.addWidget(QLabel(
                f"「{os.path.basename(path)}」にはサブフォルダが {len(subdirs)} 個あります。\n"
                f"どの範囲を登録しますか?"
            ))
            rb_this = QRadioButton("このフォルダのみ（直下の画像ファイルだけ）")
            rb_this.setChecked(True)
            rb_recursive = QRadioButton("このフォルダ＋全サブフォルダを再帰的に登録")
            layout.addWidget(rb_this)
            layout.addWidget(rb_recursive)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(dlg.accept)
            buttons.rejected.connect(dlg.reject)
            layout.addWidget(buttons)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            recursive = rb_recursive.isChecked()

        # 進捗ダイアログ（キャンセルボタン付き）
        prog = QProgressDialog(
            f"「{os.path.basename(path)}」をスキャン中...",
            "キャンセル", 0, 0, self          # maximum=0 でインジケータ表示
        )
        prog.setWindowTitle("フォルダ登録")
        prog.setMinimumWidth(420)
        prog.setMinimumDuration(0)    # 即表示
        prog.setAutoClose(False)
        prog.setValue(0)
        prog.show()
        QApplication.processEvents()

        # パスをスラッシュ統一してからWorkerに渡す（日本語パス対応）
        from pathlib import Path as _PPath
        path = _PPath(path).as_posix()

        # ScanWorker でバックグラウンド実行
        self._scan_worker = ScanWorker(path, recursive, parent=self)

        _last_update = [0.0]

        def on_progress(msg: str, cur: int, total: int) -> None:
            import time as _time
            now = _time.monotonic()
            # 100ms に1回だけ UI を更新（大量シグナルでの GUI 圧迫を防ぐ）
            if now - _last_update[0] < 0.1 and cur < total:
                return
            _last_update[0] = now
            prog.setMaximum(total)
            prog.setValue(cur)
            prog.setLabelText(msg)
            # バグ修正: on_progress自体がメインスレッドのイベントループから
            # 呼ばれるスロットのため、ここでさらにprocessEvents()を呼ぶと
            # イベントループが再入する。スキャン中にボタン連打等が起きると
            # スタックオーバーフローや予期せぬクラッシュに繋がりうる危険な
            # アンチパターンのため削除。QProgressDialogはsetValue()呼び出し
            # だけで自身のタイミングで安全に再描画される。
            if prog.wasCanceled():
                self._scan_worker.requestInterruption()

        def on_finished(result: dict) -> None:
            prog.close()
            n_folders = result.get("folders", 1)
            folder_msg = (f"＋サブフォルダ {n_folders-1} 個" if n_folders > 1 else "")
            self.bookmark_pane.refresh()
            QMessageBox.information(
                self, "登録完了",
                f"「{os.path.basename(path)}」{folder_msg}を登録しました。\n"
                f"新規: {result['added']} 件 / 復帰: {result['recovered']} 件"
                f" / スキップ: {result['skipped']} 件"
            )
            self.scan_requested.emit(path)

        def on_error(msg: str) -> None:
            prog.close()
            QMessageBox.critical(self, "エラー", f"登録に失敗しました:\n{msg}")

        self._scan_worker.progress.connect(on_progress)
        self._scan_worker.finished.connect(on_finished)
        self._scan_worker.error.connect(on_error)
        self._scan_worker.start()