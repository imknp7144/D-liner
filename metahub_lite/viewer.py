"""
viewer.py
Standalone metadata-aware multi-format image viewer.

Usage:
    python -m metahub_lite.viewer [--meta-only] [--sync-channel <name>] <path>

Left pane: image (static via QPixmap, animated GIF/WebP via QMovie).
Right pane: labeled prompt sections (Base / Regional A / Note / ... ) resolved
by analyze.py, laid out as stacked cards -- deliberately close to the "Note"
panel style shown in the reference ComfyUI mockup, not a single flat text box.

--meta-only: skips creating the left image pane entirely (no pixmap/movie
decode, no splitter) and shows only the section panel. Added for D-liner's
SDI window integration, where the image is already visible in the caller's
own window and a second copy of it is redundant. Folder-navigation still
works in this mode, just via Left/Right arrow keys instead of the mouse
wheel (the wheel-to-navigate binding lives on the image pane, which doesn't
exist here, and repurposing the section panel's own scroll wheel for
navigation would fight with scrolling long prompt text).

--sync-channel <name>: opens a QLocalServer under this name. Each line
(newline-terminated UTF-8 path string) received from a connected client is
treated exactly like a fresh open_path() call. This lets a caller (D-liner's
SDI window) keep an already-open --meta-only viewer following its own image
navigation, without this file knowing anything about D-liner beyond "a path
string arrived". See meta_viewer_launcher.MetaViewerLink on the D-liner side
for the client half of this protocol.

This file has no dependency on D-liner (no DB, no tag store). It is meant to
be launched as a separate process with a file path as the last argv entry.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtGui import QMovie, QPixmap, QFont, QShortcut, QKeySequence
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QScrollArea, QSplitter,
    QVBoxLayout, QHBoxLayout, QFrame, QTextEdit, QSizePolicy, QFileDialog,
    QToolBar, QCheckBox,
)

from .analyze import analyze
from .comfy_resolve import PromptSection

ANIMATED_SUFFIXES = {".gif", ".webp", ".apng"}
STATIC_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".avif"}
SUPPORTED_SUFFIXES = ANIMATED_SUFFIXES | STATIC_SUFFIXES

# Roughly one color per recurring label so Base/Regional/Negative are
# distinguishable at a glance without reading the header text every time.
SECTION_COLORS = {
    "Base": "#3a3f52",
    "Positive": "#3a3f52",
    "Regional A": "#4a3a52",
    "Regional B": "#3a4a4a",
    "Regional C": "#4a4a3a",
    "Negative": "#522e2e",
    "Note": "#4a4030",
    "A1111": "#39434a",
}
DEFAULT_SECTION_COLOR = "#3a3a3a"


class AutoHeightTextEdit(QTextEdit):
    """A read-only QTextEdit that always shows its full wrapped content --
    no internal scrollbar, height tracks content exactly. The outer
    SectionPanel (a QScrollArea) is what scrolls, not the individual cards.

    QTextEdit's own line-wrap keeps the *rendered* text in sync with the
    widget's width automatically, but document().size() -- which is what we
    need to compute the required height -- only reflects that if
    document().setTextWidth() is kept in sync with the viewport width
    ourselves. So height is recalculated on every resize (width change from
    window/splitter resizing) and once right after the text is set."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAcceptDrops(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def setPlainText(self, text: str) -> None:
        super().setPlainText(text)
        self._update_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_height()

    def _update_height(self):
        doc = self.document()
        width = self.viewport().width()
        if width <= 0:
            return  # not laid out yet; resizeEvent will fire again once it is
        doc.setTextWidth(width)
        margins = self.contentsMargins()
        # Sanity cap: a single pathological prompt (a runaway dynamic
        # caption dump, say) shouldn't be able to make one card dominate
        # the whole scroll area. This is a safety bound, not the normal
        # sizing mechanism -- ordinary prompts, even very long ones, size
        # to their exact wrapped content.
        height = int(doc.size().height() + margins.top() + margins.bottom() + 12)
        height = max(32, min(height, 2400))
        if height != self.height():
            self.setFixedHeight(height)


class SectionCard(QFrame):
    """One labeled prompt block, styled like the reference Note panel."""

    def __init__(self, section: PromptSection, parent=None):
        super().__init__(parent)
        color = SECTION_COLORS.get(section.label, DEFAULT_SECTION_COLOR)
        self.setStyleSheet(f"""
            SectionCard {{
                background-color: #22252b;
                border: 1px solid {color};
                border-radius: 6px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QLabel(f"  {section.label}" + (f"  ·  {section.title}" if section.title != section.label else ""))
        header.setStyleSheet(
            f"background-color: {color}; color: #e8e8e8; padding: 6px 8px; "
            f"font-weight: bold; border-top-left-radius: 5px; border-top-right-radius: 5px;"
        )
        layout.addWidget(header)

        body = AutoHeightTextEdit()
        body.setStyleSheet(
            "QTextEdit { background-color: #22252b; color: #d8d8d8; border: none; padding: 8px; }"
        )
        body.setFont(QFont("Consolas", 10))
        body.setPlainText(section.text or "(空)")
        layout.addWidget(body)


class SectionPanel(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea { background-color: #1c1e23; border: none; }")

        # Note sections never carry the resolved Positive/Negative text --
        # they're plain annotations (see comfy_resolve.py's TEXT_CONTAINER_
        # TYPES / fallback sweep) -- so they default to hidden and clutter
        # the panel far more than they inform it, especially on workflows
        # with a lot of process documentation notes. Kept one toggle away
        # rather than dropped entirely, since they're still occasionally
        # useful context.
        self._all_sections: list[PromptSection] = []
        self._show_notes = False

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setSpacing(10)

        self._notes_toggle = QCheckBox()
        self._notes_toggle.setStyleSheet("QCheckBox { color: #999; padding: 2px 4px; }")
        self._notes_toggle.stateChanged.connect(self._on_toggle_notes)
        self._notes_toggle.hide()  # only shown once we know there are Notes
        self._layout.addWidget(self._notes_toggle)

        self._layout.addStretch(1)
        self.setWidget(self._container)

    def _on_toggle_notes(self, state):
        self._show_notes = bool(state)
        self._render()

    def set_sections(self, sections: list[PromptSection]):
        self._all_sections = sections
        non_note = [s for s in sections if s.label != "Note"]
        # Notes default to hidden so they don't clutter a real Positive/
        # Negative result -- but if there's nothing BUT Notes, that default
        # would leave the panel showing only a "click here to see them"
        # placeholder for content that's the entire result. Show them
        # automatically in that case instead.
        self._show_notes = bool(sections) and not non_note
        self._notes_toggle.blockSignals(True)
        self._notes_toggle.setChecked(self._show_notes)
        self._notes_toggle.blockSignals(False)
        self._render()

    def _render(self):
        # Clear existing cards/placeholder -- everything between the
        # toggle checkbox (index 0) and the trailing stretch (last index).
        while self._layout.count() > 2:
            item = self._layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()

        sections = self._all_sections
        if not sections:
            placeholder = QLabel("メタデータが見つかりませんでした。")
            placeholder.setStyleSheet("color: #888; padding: 20px;")
            self._layout.insertWidget(1, placeholder)
            self._notes_toggle.hide()
            return

        note_count = sum(1 for s in sections if s.label == "Note")
        if note_count:
            self._notes_toggle.setText(f"  📝 Note を表示 ({note_count}件、初期状態では非表示)")
            self._notes_toggle.show()
        else:
            self._notes_toggle.hide()

        visible = sections if self._show_notes else [s for s in sections if s.label != "Note"]

        if not visible:
            placeholder = QLabel(
                "Positive/Negativeに該当するセクションが見つかりませんでした"
                "(Noteのみ検出。上のチェックでNoteを表示できます)。"
            )
            placeholder.setStyleSheet("color: #888; padding: 20px;")
            placeholder.setWordWrap(True)
            self._layout.insertWidget(1, placeholder)
            return

        for i, section in enumerate(visible):
            self._layout.insertWidget(i + 1, SectionCard(section))


class ImagePanel(QScrollArea):
    # Emitted on mouse-wheel scroll over the image: +1 = next image in the
    # folder, -1 = previous. The panel itself doesn't know about the
    # folder listing (that's ViewerWindow's job) -- it just reports intent.
    wheelNavigate = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("QScrollArea { background-color: #101114; border: none; }")

        # QLabel alone as the scroll area's resizable widget centers its
        # pixmap correctly *horizontally* but was observed drifting toward
        # the bottom vertically once the pixmap size changes after a resize
        # (the label's own sizeHint lags the viewport for one layout pass,
        # and QScrollArea's automatic scroll-position handling then anchors
        # to the old bottom edge). Wrapping the label in a plain container
        # with an explicit stretch-label-stretch VBox layout makes centering
        # a property of the always-correctly-sized container instead of the
        # label's sizeHint, which is the standard fix for this pattern.
        self._container = QWidget()
        self._container.setStyleSheet("background-color: #101114;")
        container_layout = QVBoxLayout(self._container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.addStretch(1)
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        container_layout.addWidget(self._label, 0, Qt.AlignmentFlag.AlignCenter)
        container_layout.addStretch(1)
        self.setWidget(self._container)

        self._movie: QMovie | None = None
        self._original_pixmap: QPixmap | None = None
        self._movie_native_size = None  # QSize, for scaling animated content

    def load(self, path: Path):
        if self._movie:
            self._movie.stop()
            self._movie = None
        self._original_pixmap = None
        self._movie_native_size = None

        if path.suffix.lower() in ANIMATED_SUFFIXES:
            movie = QMovie(str(path))
            if movie.isValid() and movie.frameCount() > 1:
                movie.jumpToFrame(0)
                native = movie.currentImage().size()
                if not native.isEmpty():
                    self._movie_native_size = native
                self._movie = movie
                self._label.setMovie(movie)
                self._apply_fit()
                movie.start()
                return

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._label.setText(f"表示できない形式です: {path.suffix}")
            self._label.setStyleSheet("color: #888;")
        else:
            self._original_pixmap = pixmap
            self._apply_fit()

    def _apply_fit(self):
        """Shrink to fit the current viewport, preserving aspect ratio.
        Never upscale past the image's native size -- only shrink when the
        image is larger than the available space."""
        viewport_size = self.viewport().size()
        if viewport_size.width() <= 1 or viewport_size.height() <= 1:
            return  # not laid out yet

        if self._original_pixmap is not None:
            src_size = self._original_pixmap.size()
            if src_size.width() > viewport_size.width() or src_size.height() > viewport_size.height():
                scaled = self._original_pixmap.scaled(
                    viewport_size, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._label.setPixmap(scaled)
            else:
                self._label.setPixmap(self._original_pixmap)

        elif self._movie is not None and self._movie_native_size is not None:
            src_size = self._movie_native_size
            if src_size.width() > viewport_size.width() or src_size.height() > viewport_size.height():
                target = src_size.scaled(
                    viewport_size, Qt.AspectRatioMode.KeepAspectRatio,
                )
                self._movie.setScaledSize(target)
            else:
                self._movie.setScaledSize(src_size)

    def refit(self):
        """外部（ViewerWindow.showEvent）から明示的に再フィットさせるための
        公開エントリポイント。初回表示時、QSplitterのレイアウトがまだ
        確定していない段階で load() 経由の _apply_fit() が走ってしまい、
        本来より狭いviewport幅でフィットした結果が残る（左端に黒枠が出る）
        ケースへの対策。"""
        self._apply_fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_fit()

    def wheelEvent(self, event):
        # Images are always fit-to-viewport (never upscaled, shrunk to fit),
        # so there's normally nothing to scroll here anyway -- repurpose the
        # wheel for folder navigation instead of the inherited QScrollArea
        # scroll behavior. angleDelta().y() > 0 means "scrolled up".
        delta = event.angleDelta().y()
        if delta > 0:
            self.wheelNavigate.emit(-1)
        elif delta < 0:
            self.wheelNavigate.emit(1)
        event.accept()


class SyncListener(QObject):
    """
    --sync-channel で指定された名前で QLocalServer を立て、接続してきた
    クライアントから改行区切りのUTF-8パス文字列を受け取る度に
    on_path_received(path) を呼ぶだけの受信専用ヘルパー。D-liner側の
    meta_viewer_launcher.MetaViewerLink / open_in_meta_viewer_singleton()
    と対になるプロトコル実装（送信側はそちらを参照。プロトコルの実体は
    「改行区切りのパス文字列を1行流すだけ」で、それ以外の意味は持たない）。

    同時に複数クライアントを相手にする想定はなく、新しい接続が来たら
    古い接続を切って唯一の同期元として扱う。新しい接続を受けた瞬間だけ
    on_new_connection() を呼ぶ（ViewerWindow側はここでウィンドウを前面に
    出す。同一接続上での継続的な受信では呼ばれない）。
    """

    def __init__(self, parent: QObject, channel: str, on_path_received, on_new_connection=None):
        super().__init__(parent)
        self._client: QLocalSocket | None = None
        self._buffer = b""
        self._on_path_received = on_path_received
        self._on_new_connection = on_new_connection

        QLocalServer.removeServer(channel)  # 前回異常終了時の残骸掃除（Unix向け）
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        self._server.listen(channel)

    def _on_connection(self) -> None:
        sock = self._server.nextPendingConnection()
        if sock is None:
            return
        if self._client is not None:
            self._client.disconnectFromServer()
        self._client = sock
        self._buffer = b""
        sock.readyRead.connect(self._on_data)
        sock.disconnected.connect(self._on_disconnected)
        if self._on_new_connection is not None:
            self._on_new_connection()

    def _on_data(self) -> None:
        if self._client is None:
            return
        self._buffer += bytes(self._client.readAll())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            path_str = line.decode("utf-8", errors="replace").strip()
            if path_str:
                self._on_path_received(path_str)

    def _on_disconnected(self) -> None:
        self._client = None


class ViewerWindow(QMainWindow):
    def __init__(self, path: str | None = None, meta_only: bool = False, sync_channel: str | None = None):
        super().__init__()
        self.setWindowTitle("Meta Viewer")
        self.setStyleSheet("QMainWindow { background-color: #1c1e23; }")
        self.setAcceptDrops(True)

        self._meta_only = meta_only
        self._folder_images: list[Path] = []
        self._current_index: int = -1

        toolbar = QToolBar()
        toolbar.setMovable(False)
        nav_hint = "← → で前後の画像へ" if meta_only else "ホイールまたは← →で前後の画像へ"
        open_btn = QLabel(f"  📂 開く (Ctrl+O) ・ 画像をドラッグ&ドロップ ・ {nav_hint}")
        open_btn.setStyleSheet("color: #ccc; padding: 4px;")
        toolbar.addWidget(open_btn)
        self.addToolBar(toolbar)

        self.section_panel = SectionPanel()

        if meta_only:
            # 画像パネル自体を生成しない。D-liner SDIウィンドウから呼ばれる
            # 場合、画像は呼び出し元(SDI)側に既に表示されているため、ここで
            # 二重に描画・デコードするのは無駄（ユーザー指摘の重複表示問題）。
            self.image_panel = None
            self.resize(700, 900)
            self.setCentralWidget(self.section_panel)
        else:
            self.resize(1400, 900)
            splitter = QSplitter(Qt.Orientation.Horizontal)
            self.image_panel = ImagePanel()
            splitter.addWidget(self.image_panel)
            splitter.addWidget(self.section_panel)
            splitter.setStretchFactor(0, 3)
            splitter.setStretchFactor(1, 2)
            self.setCentralWidget(splitter)
            self.image_panel.wheelNavigate.connect(self._navigate)

        QShortcut(QKeySequence("Ctrl+O"), self, activated=self._open_dialog)

        # 前後の画像へ（フル表示ではホイールと併用、meta-onlyではホイールの
        # 代替として唯一のナビゲーション手段）。Escでウィンドウを閉じる。
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, activated=lambda: self._navigate(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, activated=lambda: self._navigate(1))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.close)

        # --sync-channel: D-liner SDIウィンドウ等からの画像切替追従用。
        # 受信処理の実体は SyncListener（送信側は meta_viewer_launcher.py
        # の MetaViewerLink / open_in_meta_viewer_singleton）。
        self._sync_listener: SyncListener | None = None
        if sync_channel:
            self._sync_listener = SyncListener(
                self, sync_channel,
                on_path_received=self.open_path,
                on_new_connection=self._on_sync_reconnect,
            )

        if path:
            self.open_path(path)

    def _on_sync_reconnect(self) -> None:
        # 新しい接続 = 誰かが改めて「メタデータビューアで開く」を実行した
        # 瞬間（シングルトン再利用時）。この瞬間だけ前面に出す。SDI連動での
        # 画像切替（同一接続上での継続的なpush）ではここは通らないので、
        # ナビゲーションの度にフォーカスを奪うことはない。
        #
        # raise_()/activateWindow() だけではWindowsのフォアグラウンドロック
        # （非アクティブプロセスによるフォーカス奪取の抑制）に阻まれ、
        # タスクバーが点滅するだけで前面に来ないことがセッション33の実機
        # 確認で判明した。WindowStaysOnTopHintを一瞬だけ付与するとSetWindowPos
        # によるZ-order変更として扱われフォーカス奪取の制限を受けないため、
        # 確実に最前面へ持って来られる（Qt/Windowsで広く使われる回避策）。
        if self.isMinimized():
            self.showNormal()
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.show()
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.image_panel is not None:
            # QSplitterのレイアウトはこの時点でようやく確定する。__init__内
            # のopen_path()経由で走った初回の_apply_fit()は、まだ確定して
            # いない（本来より狭い）viewport幅を基準にしてしまっている
            # ことがあるため、確定後の幅で明示的に取り直す。
            self.image_panel.refit()

    def open_path(self, path_str: str, refresh_folder_list: bool = True):
        path = Path(path_str)
        if not path.exists():
            self.section_panel.set_sections([
                PromptSection("Error", "", f"ファイルが見つかりません: {path}")
            ])
            return

        if refresh_folder_list:
            self._refresh_folder_list(path)

        self.setWindowTitle(f"Meta Viewer - {path.name}")
        if self.image_panel is not None:
            self.image_panel.load(path)
        sections = analyze(path)
        self.section_panel.set_sections(sections)

    def _refresh_folder_list(self, path: Path):
        """Lists every supported image in the same folder as `path`, sorted
        by filename, and records where `path` sits in that list -- this is
        the navigation order used by wheel-scroll. Only rescanned when a
        file is opened explicitly (dialog / drag-and-drop), not on every
        wheel step, so the list stays stable while flipping through it even
        if the folder is being written to concurrently (e.g. still
        generating)."""
        folder = path.parent
        try:
            files = sorted(
                (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES),
                key=lambda p: p.name,
            )
        except OSError:
            files = [path]

        if not files:
            files = [path]

        self._folder_images = files
        try:
            self._current_index = files.index(path)
        except ValueError:
            # Opened file isn't among the scanned ones (unsupported
            # extension slipped through, or a symlink/path mismatch) --
            # still make it navigable on its own.
            self._folder_images = [path]
            self._current_index = 0

    def _navigate(self, delta: int):
        if not self._folder_images:
            return
        self._current_index = (self._current_index + delta) % len(self._folder_images)
        next_path = self._folder_images[self._current_index]
        self.open_path(str(next_path), refresh_folder_list=False)

    def _open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "画像を開く", "",
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.avif)"
        )
        if path:
            self.open_path(path)

    def dragEnterEvent(self, event):
        if self._supported_drop_path(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._supported_drop_path(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        path = self._supported_drop_path(event)
        if path is not None:
            self.open_path(str(path))
            event.acceptProposedAction()
        else:
            event.ignore()

    @staticmethod
    def _supported_drop_path(event) -> Path | None:
        """Returns the first locally-supported image path in the drop's
        URL list, or None if nothing droppable is present."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            p = Path(url.toLocalFile())
            if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES:
                return p
        return None


def main():
    app = QApplication(sys.argv)
    args = sys.argv[1:]
    meta_only = "--meta-only" in args
    if meta_only:
        args = [a for a in args if a != "--meta-only"]
    sync_channel = None
    if "--sync-channel" in args:
        idx = args.index("--sync-channel")
        if idx + 1 < len(args):
            sync_channel = args[idx + 1]
            del args[idx:idx + 2]
        else:
            del args[idx]  # 値なしで渡された場合は無視して同期なしで続行
    path = args[0] if args else None
    win = ViewerWindow(path, meta_only=meta_only, sync_channel=sync_channel)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
