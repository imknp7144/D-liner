from __future__ import annotations

import os
import shutil
import struct
from collections import OrderedDict
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QSize, QMimeData, QUrl, QByteArray, QTimer, QThreadPool, QThread
from PyQt6.QtGui import QImage, QPixmap, QPainter, QMouseEvent, QKeyEvent, QWheelEvent, QAction, QGuiApplication, QDrag
from PyQt6.QtWidgets import (
    QWidget,
    QScrollArea,
    QGridLayout,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QStyle,
    QMessageBox,
    QInputDialog,
    QFileDialog,
    QDialog,
    QPushButton,
    QDialogButtonBox,
)

from workers import ThumbnailLoadTask


def show_delete_confirm_dialog(parent: QWidget | None, message_text: str) -> bool:
    """
    削除確認ダイアログ（thumbnail_grid.py・sdi_window_viewer.py 共通）。

    バグ修正: 以前は単一ファイル削除のときだけこの専用スタイルの
    ダイアログを使い、複数ファイル削除のときは素の
    QMessageBox.question()（タイトルも配色も異なる既定スタイル）を
    使っていたため、右クリック削除とキーボード(Delete)削除、また
    単一/複数選択で見た目が統一されていなかった。件数によらず
    常にこの1つのダイアログを使うようにする。さらに、SDIウィンドウ側
    (sdi_window_viewer.py)の削除確認も独自の QMessageBox.question() を
    使っており、デザイン・既定ボタンがサムネイルビュー側と不一致
    だったため、モジュールレベル関数として切り出し両方から共有する。

    バグ修正2: btn_yes に setAutoDefault(False) を設定していたため、
    QPushButtonの仕様上「フォーカスがあってもEnter/Returnに反応しない」
    状態になっていた。矢印キー等でYesボタンへフォーカス移動しEnterを
    押しても、Enterはダイアログの default ボタン（No）へ伝播してしまい、
    削除が実行されない（キーボード操作が無視されたように見える）
    不具合があった。安全のため初期フォーカス・既定ボタンは引き続き
    「No」のままとしつつ、Yesボタンにも setAutoDefault(True) を設定し、
    「現在フォーカスがあるボタンがEnterに反応する」という通常の
    Qtダイアログの挙動に合わせる。
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle("削除の確認")
    dlg.setMinimumWidth(420)
    layout = QVBoxLayout(dlg)
    layout.setSpacing(12)
    layout.setContentsMargins(20, 16, 20, 16)

    msg = QLabel(message_text)
    msg.setWordWrap(True)
    layout.addWidget(msg)

    btn_row = QHBoxLayout()
    btn_row.addStretch()

    btn_yes = QPushButton("Yes")
    btn_yes.setFixedWidth(90)
    btn_yes.setStyleSheet("""
        QPushButton {
            background-color: #c0392b;
            color: #ffffff;
            border: 2px solid #e74c3c;
            border-radius: 4px;
            padding: 6px 0;
            font-weight: bold;
            font-size: 13px;
        }
        QPushButton:hover  { background-color: #e74c3c; border-color: #ff6b6b; }
        QPushButton:focus  { border: 3px solid #ff9999; }
        QPushButton:pressed{ background-color: #96281b; }
    """)
    btn_yes.setDefault(False)
    # バグ修正2: フォーカス移動後にEnterで反応させるため True にする
    # （dialogの「既定ボタン」自体はNoのまま = 何もキー操作していない
    # 初期状態でEnterを押した場合は安全側のNoが選ばれる）。
    btn_yes.setAutoDefault(True)

    btn_no = QPushButton("No")
    btn_no.setFixedWidth(90)
    btn_no.setDefault(True)
    btn_no.setAutoDefault(True)
    btn_no.setStyleSheet("""
        QPushButton {
            background-color: #2c3e50;
            color: #e8e8e8;
            border: 2px solid #7f8c8d;
            border-radius: 4px;
            padding: 6px 0;
            font-size: 13px;
        }
        QPushButton:hover  { background-color: #3d5166; border-color: #bdc3c7; }
        QPushButton:focus  { border: 3px solid #4db3ff; }
        QPushButton:pressed{ background-color: #1a252f; }
    """)

    btn_yes.clicked.connect(dlg.accept)
    btn_no.clicked.connect(dlg.reject)

    btn_row.addWidget(btn_yes)
    btn_row.addSpacing(8)
    btn_row.addWidget(btn_no)
    layout.addLayout(btn_row)

    btn_no.setFocus()
    return dlg.exec() == QDialog.DialogCode.Accepted


class ThumbnailLabel(QWidget):
    """
    サムネイルとファイル名ラベルを表示する単一セル
    """
    clicked = pyqtSignal(int, str, bool, bool) # id, path, is_ctrl, is_shift
    dbl_clicked = pyqtSignal(int, str)       # id, path

    def __init__(self, img_id: int, path: str, parent=None) -> None:
        super().__init__(parent)
        self.img_id = img_id
        self.path = path
        self.filename = os.path.basename(path)
        self.pixmap: QPixmap | None = None
        self.selected = False
        
        self.init_ui()

    def init_ui(self) -> None:
        self.setFixedSize(170, 190)
        
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(4, 4, 4, 4)
        self.layout.setSpacing(2)

        self.image_label = QLabel(self)
        self.image_label.setFixedSize(160, 150)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background-color: palette(mid); border: 1px solid palette(shadow); border-radius: 4px;")
        self.layout.addWidget(self.image_label)

        self.text_label = QLabel(self)
        self.text_label.setFixedWidth(160)
        self.text_label.setFixedHeight(16)          # 1行固定でセル高さを統一
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.text_label.setStyleSheet("font-size: 11px; padding-left: 2px;")
        self.text_label.setToolTip(self.filename)   # ホバーで完全なファイル名を表示
        self._update_elided_text()
        self.layout.addWidget(self.text_label)

    def set_image(self, qimage: QImage) -> None:
        self.pixmap = QPixmap.fromImage(qimage)
        self.image_label.setPixmap(self.pixmap)
        self.image_label.setStyleSheet("background-color: palette(base); border: 1px solid palette(mid); border-radius: 4px;")

    def set_error(self) -> None:
        self.image_label.setText("× Error")
        self.image_label.setStyleSheet("background-color: #FFE6E6; color: #CC0000; font-weight: bold; border-radius: 4px;")

    def set_folder_icon(self) -> None:
        """サブフォルダ用セル: OSのフォルダアイコンを表示する"""
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        pm = icon.pixmap(64, 64)
        self.pixmap = pm
        self.image_label.setPixmap(pm)
        self.image_label.setStyleSheet(
            "background-color: #2a2a35;"
            "border: 1px solid palette(mid);"
            "border-radius: 4px;"
        )

    def _update_elided_text(self) -> None:
        """フォント幅に合わせてファイル名を中間省略する。
        ウィジェット表示前は width() が 0 のため固定幅(156px)にフォールバック。
        ホバーツールチップで完全なファイル名を確認できる。"""
        fm = self.text_label.fontMetrics()
        w = self.text_label.width()
        available = (w - 4) if w > 8 else 156   # 未確定時は 160px - 4px パディング
        elided = fm.elidedText(
            self.filename,
            Qt.TextElideMode.ElideRight,   # 末尾省略: Linar準拠（先頭から読める）
            available
        )
        self.text_label.setText(elided)
        self.text_label.setToolTip(self.filename)  # 常に完全名をツールチップに

    def showEvent(self, event) -> None:
        """表示時にレイアウトが確定しているので省略テキストを再計算する"""
        super().showEvent(event)
        self._update_elided_text()

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        if selected:
            # サムネイル自体が見えるよう背景は極薄にして、image_label を太枠で囲む
            # Linar準拠: 明るい青枠 + ファイル名テキストを太字白にして選択を強調
            self.image_label.setStyleSheet(
                "background-color: palette(base);"
                "border: 3px solid #4db3ff;"
                "border-radius: 4px;"
            )
            self.text_label.setStyleSheet(
                "font-size: 11px; padding-left: 2px; color: #4db3ff; font-weight: bold;"
            )
        else:
            # 非選択: image_labelスタイルを画像ロード済み/未ロードで分岐しないためリセット
            # (set_imageが呼ばれると上書きされるので初期化のみ)
            self.image_label.setStyleSheet(
                "background-color: palette(mid);"
                "border: 1px solid palette(shadow);"
                "border-radius: 4px;"
            )
            self.text_label.setStyleSheet("font-size: 11px; padding-left: 2px;")
        # ピクセルマップが既にセットされていれば再描画（border変更後にピクセルマップを維持）
        if self.pixmap and not self.pixmap.isNull():
            self.image_label.setPixmap(self.pixmap)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
            self._drag_button = Qt.MouseButton.LeftButton
            self._drag_started = False
            is_ctrl  = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            is_shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            # バグ修正: 複数選択中のアイテムをまとめてドラッグしようとすると
            # 常に1件しかコピー/移動されない不具合があった。原因は、
            # mousePressEvent（マウスを押した瞬間）で無条件に clicked を
            # 発火させていたため、Ctrl/Shift無しの押下は
            # _on_item_clicked() の単一選択分岐（_clear_multi_selection()）
            # に入り、ドラッグ判定（mouseMoveEventの8px閾値）より前に
            # 複数選択がその場で1件へ潰れてしまっていたこと。
            # 既に複数選択済みのアイテムを、Ctrl/Shift無しで押下した場合は
            # 「単一選択への確定」を即座には行わず、mouseReleaseEvent まで
            # 保留する。実際にドラッグが始まった場合は元の複数選択を
            # そのまま使い、ドラッグにならず単純にクリックだけで終わった
            # 場合にのみ、その場で単一選択へ確定する
            # （Explorer等の標準的なファイラーの挙動と同様）。
            grid = getattr(self, "_grid_ref", None)
            already_multi_selected = (
                grid is not None
                and len(grid.selected_paths) > 1
                and self.path in grid.selected_paths
            )
            if already_multi_selected and not is_ctrl and not is_shift:
                self._pending_click = True
            else:
                self._pending_click = False
                self.clicked.emit(self.img_id, self.path, is_ctrl, is_shift)
        elif event.button() == Qt.MouseButton.RightButton:
            # バグ修正: 右クリックドラッグでフォルダツリーへD&Dした際、
            # コピー/移動の選択メニューを出せるようにするため、右ボタン
            # でもドラッグ開始位置を記録する。selected_pathsの変更は
            # 行わない（右クリック単体時の通常のコンテキストメニュー
            # 表示動作に影響を与えないため）。
            self._drag_start_pos = event.pos()
            self._drag_button = Qt.MouseButton.RightButton
            self._drag_started = False
            self._pending_click = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # バグ修正（上記mousePressEventのコメント参照）: 保留していた
        # 単一選択への確定を、実際にドラッグが始まらなかった場合のみ
        # ここで行う。
        if event.button() == Qt.MouseButton.LeftButton and getattr(self, "_pending_click", False):
            if not getattr(self, "_drag_started", False):
                self.clicked.emit(self.img_id, self.path, False, False)
        self._pending_click = False
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not (event.buttons() & (Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)):
            return
        if not hasattr(self, "_drag_start_pos"):
            return
        dist = (event.pos() - self._drag_start_pos).manhattanLength()
        if dist < 8:  # ドラッグ開始閾値 (px)
            return
        self._drag_started = True
        # ドラッグ開始: ファイルURL を MimeData に乗せる
        # 複数選択中にその一員をドラッグした場合は、選択されている
        # 全ファイルをまとめて渡す（フォルダツリーへのD&Dで一括
        # コピー/移動できるようにするため）。単独/範囲外なら自分の
        # パスのみ。
        grid = getattr(self, "_grid_ref", None)
        if grid is not None and len(grid.selected_paths) > 1 and self.path in grid.selected_paths:
            drag_paths = list(grid.selected_paths)
        else:
            drag_paths = [self.path]

        drag_button = getattr(self, "_drag_button", Qt.MouseButton.LeftButton)

        drag = QDrag(self)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in drag_paths])
        # バグ修正: ドロップ先（フォルダツリー）が「右ドラッグなら
        # コピー/移動を確認するメニューを出す」ため、どちらのボタンで
        # ドラッグを開始したかをMimeDataに埋め込んで伝える。QDropEvent
        # 側のボタン状態はドロップ完了時には離されていて当てにできない
        # ため、開始時点の情報を明示的に運ぶ。
        marker = b"right" if drag_button == Qt.MouseButton.RightButton else b"left"
        mime.setData("application/x-dliner-drag-button", marker)
        drag.setMimeData(mime)
        # サムネイルをドラッグ画像に使用
        if self.pixmap and not self.pixmap.isNull():
            scaled = self.pixmap.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
            drag.setPixmap(scaled)
            drag.setHotSpot(QPoint(scaled.width() // 2, scaled.height() // 2))
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.dbl_clicked.emit(self.img_id, self.path)
        super().mouseDoubleClickEvent(event)


class ThumbnailGridWidget(QScrollArea):
    """
    サムネイルをタイル表示、スクロール連動非同期ロード、LRUキャッシュ。
    """
    selection_changed = pyqtSignal(int, str) # 選択された画像のIDとPathを通知
    open_in_sdi = pyqtSignal(str)             # SDIで開く（パス）
    file_operation_done = pyqtSignal()        # ファイル操作（削除/移動/リネーム）後の再検索要求
    drop_requested = pyqtSignal(list, bool)   # D&Dドロップ: (paths, is_move)
    folder_navigate = pyqtSignal(str)         # フォルダサムネイルをダブルクリック→フォルダ移動
    bulk_tag_requested = pyqtSignal()         # 選択画像への一括タグ追加/削除を要求（main_window側で処理）
    retag_with_settings_requested = pyqtSignal()  # 選択画像を別設定(閾値/モデル)でタグ付けし直す要求（同上）
    lora_export_requested = pyqtSignal()          # LoRA用エクスポート要求（同上、セッション27）
    # 指示書03 タスクD: 「似たタグの画像を探す」で抽出したタグ（スペース区切り
    # 文字列）。ThumbnailGridWidget は main_window への参照を持たない設計
    # （既存の bulk_tag_requested 等と同じ疎結合パターン）のため、検索欄への
    # 反映・検索実行は main_window 側で行う。
    similar_tag_search_requested = pyqtSignal(str)

    # サムネイルサイズ (Linar準拠: +/- キーで変更)
    THUMB_SIZES = [80, 100, 130, 160, 200, 250, 320]  # 段階的なサイズ一覧
    _thumb_size_idx = 3  # デフォルト: 160px (index=3)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        self.grid_container = QWidget(self)
        self.setWidget(self.grid_container)

        self.image_data: list[tuple] = []
        self.image_items: list[ThumbnailLabel | None] = []
        self.selected_item_path: str = ""
        self.selected_item: ThumbnailLabel | None = None
        # 複数選択: Ctrl+クリックで追加、Shift+クリックで範囲選択
        self.selected_paths: set[str] = set()
        self._last_clicked_idx: int = -1
        self.cache = OrderedDict() # path -> QImage (LRU)
        self.cache_limit = 100
        self.workers: dict[str, list[bool]] = {}
        # バグ修正: 従来は _trigger_load() のたびに ThumbnailWorker(QThread) を
        # 無制限に生成しており、リサイズ/高速スクロールで大量セルが同時に
        # 可視化されるとその数だけOSネイティブスレッドが同時起動しうる
        # 問題があった。QThreadPool で同時実行数を確定的に絞る
        # （CPUコア数に応じて2〜4に制限）。self.workers の値は
        # ThumbnailLoadTask と共有するキャンセルフラグ（[bool]）のみを
        # 保持し、実際のスレッド管理はプール側に任せる。
        self._thumb_pool = QThreadPool(self)
        self._thumb_pool.setMaxThreadCount(max(2, min(4, QThread.idealThreadCount())))
        # 永続キャッシュ（main_window から set_cache() で注入）
        self._thumb_cache = None

        # キーボードナビゲーション用
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)

        # コンテキストメニュー設定 (Task 3)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        # バグ修正: 以前は valueChanged を直接 load_visible_thumbnails に
        # 接続しており、さらに wheelEvent() 側でも同じフレーム内で明示的に
        # load_visible_thumbnails() を呼んでいたため、ホイール1目盛りで
        # 2回実行されていた。加えて高速ホイール/トラックパッドで
        # valueChanged が連続発火すると毎回サムネイル読み込みワーカーが
        # 起動し、CPU負荷が急上昇していた。高速スクロール中の中間フレーム
        # は人の目にも映らないため、短い間引きタイマーでコアレシングする。
        self._scroll_load_pending = False
        self._scroll_load_timer = QTimer(self)
        self._scroll_load_timer.setSingleShot(True)
        self._scroll_load_timer.timeout.connect(self._flush_scroll_load)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)

        # バグ修正: resizeEvent() にはデバウンスが無く、ウィンドウを
        # ドラッグしてリサイズしている間、フレームごとに
        # load_visible_thumbnails() が呼ばれてサムネイルデコードタスクが
        # 大量に発行されていた（ホイールスクロールは上記で既にデバウンス
        # 済みだったが、ウィンドウリサイズは未対応のままだった）。
        # 同様のリーディング+トレーリング デバウンスを別タイマーで行う。
        self._resize_load_pending = False
        self._resize_load_timer = QTimer(self)
        self._resize_load_timer.setSingleShot(True)
        self._resize_load_timer.timeout.connect(self._flush_resize_load)

    def _on_scroll_value_changed(self, _value: int) -> None:
        """
        スクロール位置変化時の間引き付きハンドラ。
        静止状態からの最初の1回は即座に反映してレスポンスを保ち、
        以降60ms以内の連続変化は間引いて、停止後に最後にもう一度
        確定反映する（リーディング+トレーリング デバウンス）。
        """
        if self._scroll_load_timer.isActive():
            self._scroll_load_pending = True
            return
        self.load_visible_thumbnails()
        self._scroll_load_timer.start(60)

    def _flush_scroll_load(self) -> None:
        if self._scroll_load_pending:
            self._scroll_load_pending = False
            self.load_visible_thumbnails()

    def _flush_resize_load(self) -> None:
        if self._resize_load_pending:
            self._resize_load_pending = False
            self.load_visible_thumbnails()

    def set_cache(self, cache) -> None:
        """
        永続キャッシュを注入する。main_window の init_ui 後に呼ぶ。

        Args:
            cache: ThumbnailCache インスタンス（または None で無効化）
        """
        self._thumb_cache = cache

        # スクロールバーを視認しやすい色に（ダークテーマでは背景と近似するため）
        self.setStyleSheet("""
            QScrollBar:vertical {
                background: #2a2a2a;
                width: 10px;
                margin: 0px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #5a8ab5;
                min-height: 24px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical:hover {
                background: #4db3ff;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: none;
            }
        """)

    def set_images(self, image_tuples: list[tuple]) -> None:
        """
        検索結果の全画像をグリッドにセット。
        仮想化（絶対座標遅延生成）により、何万件あっても一瞬でロード完了する。
        """
        # 前の遅延バッチタイマーがあればキャンセル
        if hasattr(self, "_batch_timer") and self._batch_timer:
            self._batch_timer.stop()
            self._batch_timer = None
        self._pending_tuples = []

        # バグ修正: 削除/リネーム/移動などのファイル操作後は
        # file_operation_done → trigger_search() 経由で非同期に
        # SearchWorker が再検索を行い、完了後にこの set_images() が
        # 呼ばれる。以前は無条件で選択とスクロール位置をリセットして
        # いたため、削除直後にローカルで正しく次の項目へ選択を移して
        # いても（_remove_items_from_grid_batch 参照）、少し遅れて届く
        # 再検索結果によって毎回フォルダ先頭へ戻されてしまっていた。
        # ここでは「直前に選択していたパス」を退避し、新しい結果にも
        # まだ存在するなら選択とスクロール位置を復元する。存在しない
        # 場合（フォルダそのものを切り替えた等）は従来通り先頭へ戻す。
        prev_selected_path = self.selected_item_path

        # クリーンアップ（実行中/待機中タスクへキャンセルを通知するのみ。
        # QThreadPool管理下のタスクなので個別にquit()する必要はない）
        for cancel_flag in self.workers.values():
            cancel_flag[0] = True
        self.workers.clear()

        # 既存ウィジェットの削除
        for item in self.image_items:
            if item is not None:
                item.deleteLater()
        self.image_items.clear()
        self.selected_item = None

        self.image_data = list(image_tuples)
        self.image_items = [None] * len(image_tuples)
        self.selected_paths.clear()
        self.selected_item_path = ""
        self._last_clicked_idx = -1

        # グリッドコンテナのサイズ更新
        self._update_grid_size()

        # 選択状態・スクロール位置の復元を試みる（同一フォルダ内の
        # ファイル操作による再検索であればここでヒットする）
        if prev_selected_path and self._path_to_index(prev_selected_path) >= 0:
            self.select_by_path(prev_selected_path)
        else:
            self.verticalScrollBar().setValue(0)

        # サムネイル表示
        self.load_visible_thumbnails()

    def _update_grid_size(self) -> None:
        """列数とコンテナサイズを計算し、適用する"""
        total_items = len(self.image_data)
        size = self.THUMB_SIZES[self._thumb_size_idx]
        item_w = size + 10
        item_h = size + 30
        spacing = 10
        col_pitch = item_w + spacing
        row_pitch = item_h + spacing

        vw = self.viewport().width()
        if vw <= 0:
            vw = self.width() - self.verticalScrollBar().width() - 4
        if vw <= 0:
            vw = 900
        
        cols = max(1, vw // col_pitch)
        self._current_cols = cols

        if total_items == 0:
            self.grid_container.setFixedSize(vw, 0)
            return

        rows = (total_items + cols - 1) // cols
        total_h = rows * row_pitch + spacing
        self.grid_container.setFixedSize(vw, max(1, total_h))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # レイアウト寸法の再計算自体は軽量なので毎フレーム即時に行うが、
        # サムネイルデコードタスク発行を伴う load_visible_thumbnails() は
        # デバウンスして、リサイズ中の連続発火を間引く。
        self._update_grid_size()
        if self._resize_load_timer.isActive():
            self._resize_load_pending = True
            return
        self.load_visible_thumbnails()
        self._resize_load_timer.start(120)

    def _change_thumb_size(self, delta: int) -> None:
        """サムネイルサイズを段階的に変更する（Linar準拠: +/- キー）"""
        idx = ThumbnailGridWidget._thumb_size_idx + delta
        idx = max(0, min(idx, len(self.THUMB_SIZES) - 1))
        if idx == ThumbnailGridWidget._thumb_size_idx:
            return
        ThumbnailGridWidget._thumb_size_idx = idx

        # サイズが変わったら異なるサイズのキャッシュが混在しないようクリア
        self.cache.clear()

        # 既存ウィジェットを一旦破棄して再初期化
        for item in self.image_items:
            if item is not None:
                item.deleteLater()
        self.image_items = [None] * len(self.image_data)
        self.selected_item = None

        self._update_grid_size()
        self.load_visible_thumbnails()

    def load_visible_thumbnails(self) -> None:
        """
        現在スクロール内に見えているサムネイルのみを遅延非同期ロード
        (仮想スクロール: 画面内のウィジェットのみを動的生成し、画面外は破棄)
        """
        if not self.image_data:
            return

        size = self.THUMB_SIZES[self._thumb_size_idx]
        item_w = size + 10
        item_h = size + 30
        spacing = 10
        col_pitch = item_w + spacing
        row_pitch = item_h + spacing

        scroll_y = self.verticalScrollBar().value()
        viewport_h = self.viewport().height()
        if viewport_h <= 0:
            viewport_h = self.height()

        cols = getattr(self, "_current_cols", 1)
        total_items = len(self.image_data)
        rows = (total_items + cols - 1) // cols

        # 画面内に見える行範囲を算出 (マージンとして前後1行追加)
        start_row = max(0, (scroll_y // row_pitch) - 1)
        end_row = min(rows - 1, (scroll_y + viewport_h) // row_pitch + 1)
        
        start_idx = start_row * cols
        end_idx = min(total_items - 1, (end_row + 1) * cols - 1)

        visible_indices = set(range(start_idx, end_idx + 1))
        
        # 1. 画面内アイテムの生成とロード
        for idx in visible_indices:
            item = self.image_items[idx]
            img_id, path, _, _, _ = self.image_data[idx]

            if item is None:
                item = ThumbnailLabel(img_id, path, self.grid_container)
                item.setFixedSize(item_w, item_h)
                item.image_label.setFixedSize(size, int(size * 0.9))
                item.text_label.setFixedWidth(size + 4)

                # ドラッグ開始時に複数選択かどうかを判定できるよう、
                # 所属グリッドへの参照を持たせておく（フォルダツリーへの
                # D&Dで複数ファイルをまとめて渡せるようにするため）。
                item._grid_ref = self

                item.clicked.connect(self._on_item_clicked)
                item.dbl_clicked.connect(self._on_item_dbl_clicked)
                
                # 選択状態復元
                if self.selected_item_path == path:
                    item.set_selected(True)
                    self.selected_item = item

                item.show()
                self.image_items[idx] = item

            # バグ修正: 従来はここ(item is None の分岐内)でのみ座標を設定して
            # いたため、削除等でリストの要素がずれた際に「再利用される既存
            # ウィジェット」の座標が更新されず、古い位置のまま表示され続けて
            # いた（1件削除だと大半のウィジェットが再利用対象になるため
            # 特に顕著だった）。座標計算はidxに対して常に再計算し、
            # 新規/再利用を問わず毎回 move() する。
            row = idx // cols
            col = idx % cols
            x = spacing + col * col_pitch
            y = spacing + row * row_pitch
            item.move(x, y)

            # 画像ロード
            if item.img_id == -2:
                if item.pixmap is None:
                    item.set_folder_icon()
            else:
                if item.path in self.cache:
                    item.set_image(self.cache[item.path])
                else:
                    self._trigger_load(item)

        # 2. 画面外アイテムの破棄
        for idx in range(total_items):
            if idx not in visible_indices:
                item = self.image_items[idx]
                if item is not None:
                    if item == self.selected_item:
                        self.selected_item = None
                    if item.path in self.workers:
                        # バグ修正: 従来の quit() は run()を完全に
                        # オーバーライドしたQThreadには効かず（内部イベント
                        # ループが無いため）、事実上キャンセルされていなかった。
                        # 共有キャンセルフラグを立てて確実に無効化する。
                        self.workers[item.path][0] = True
                        del self.workers[item.path]
                    item.hide()
                    item.deleteLater()
                    self.image_items[idx] = None

    def _trigger_load(self, item: ThumbnailLabel) -> None:
        if item.path in self.workers:
            return

        size = self.THUMB_SIZES[self._thumb_size_idx]

        # 1. 永続キャッシュを確認（ヒットなら Worker を起動せず即表示）
        if self._thumb_cache is not None:
            # バグ修正: 従来 get() 内で threading.Thread(...) を呼ぶ箇所が
            # threading モジュール未importでNameErrorになっており、キャッシュ
            # 無効化(mtime/fsize不一致)時にここが例外を投げてこの呼び出し元
            # (load_visible_thumbnails()のループ)ごと中断していた可能性がある。
            # thumbnail_cache.py 側は修正済みだが、呼び出し側でも念のため
            # 例外を握りつぶさず記録した上でキャッシュ無効時と同じ扱い
            # （Workerでのデコードにフォールバック）にしておく。
            try:
                cached = self._thumb_cache.get(item.path, size)
            except Exception as e:
                print(f"[ThumbnailGrid] cache.get() raised for {item.path}: {e}", flush=True)
                cached = None
            if cached is not None:
                # インメモリ LRU にも乗せておく
                self.cache[item.path] = cached
                if len(self.cache) > self.cache_limit:
                    self.cache.popitem(last=False)
                item.set_image(cached)
                return

        # 2. キャッシュミス → QThreadPool経由のタスクで非同期デコード
        #    （同時実行数は self._thumb_pool.setMaxThreadCount() で確定的に制限）
        cancel_flag: list[bool] = [False]
        task = ThumbnailLoadTask(item.path, target_size=size, cancel_flag=cancel_flag)
        task.signals.finished.connect(lambda p, qimg: self._on_worker_finished(p, qimg))
        task.signals.error.connect(lambda p, err: self._on_worker_error(p, err))
        self.workers[item.path] = cancel_flag
        self._thumb_pool.start(task)

    def _on_worker_finished(self, path: str, qimage: QImage) -> None:
        if path in self.workers:
            del self.workers[path]

        # インメモリ LRU キャッシュ更新
        self.cache[path] = qimage
        if len(self.cache) > self.cache_limit:
            self.cache.popitem(last=False)

        # 永続キャッシュに保存（バックグラウンドで書き込むため UI をブロックしない）
        if self._thumb_cache is not None:
            size = self.THUMB_SIZES[self._thumb_size_idx]
            self._thumb_cache.put(path, size, qimage)

        # UI のサムネイルに画像適用
        for item in self.image_items:
            if item is not None and item.path == path:
                item.set_image(qimage)

    def _on_worker_error(self, path: str, error_msg: str) -> None:
        if path in self.workers:
            del self.workers[path]
        for item in self.image_items:
            if item is not None and item.path == path:
                item.set_error()

    def _on_item_clicked(self, img_id: int, path: str, is_ctrl: bool, is_shift: bool) -> None:
        """
        クリック時の選択処理。
        通常: 単一選択  Ctrl: トグル追加  Shift: 範囲選択
        フォルダアイテム(img_id==-2)は複数選択対象外。
        """
        if img_id == -2:
            self.select_by_path(path)
            return

        idx = self._path_to_index(path)
        if idx == -1:
            return

        if is_shift and self._last_clicked_idx >= 0:
            self._clear_multi_selection()
            lo = min(self._last_clicked_idx, idx)
            hi = max(self._last_clicked_idx, idx)
            for i in range(lo, hi + 1):
                _, p, _, _, _ = self.image_data[i]
                self.selected_paths.add(p)
                it = self.image_items[i]
                if it:
                    it.set_selected(True)
            self._set_primary(idx, path, img_id)
        elif is_ctrl:
            if path in self.selected_paths:
                self.selected_paths.discard(path)
                it = self.image_items[idx]
                if it:
                    it.set_selected(False)
                if path == self.selected_item_path:
                    self.selected_item = None
                    self.selected_item_path = ""
                    if self.selected_paths:
                        other = next(iter(self.selected_paths))
                        oi = self._path_to_index(other)
                        if oi >= 0:
                            self._set_primary(oi, other, self.image_data[oi][0])
            else:
                self.selected_paths.add(path)
                it = self.image_items[idx]
                if it:
                    it.set_selected(True)
                self._set_primary(idx, path, img_id)
            self._last_clicked_idx = idx
        else:
            self._clear_multi_selection()
            self.selected_paths.add(path)
            self._set_primary(idx, path, img_id)
            self._last_clicked_idx = idx

    def _set_primary(self, idx: int, path: str, img_id: int) -> None:
        """メイン選択（selection_changedを発火）を設定する"""
        self.selected_item_path = path
        it = self.image_items[idx]
        if it:
            it.set_selected(True)
            self.selected_item = it
        else:
            self.selected_item = None
        self.selection_changed.emit(img_id, path)
        self._scroll_to_index(idx)

    def _clear_multi_selection(self) -> None:
        """全複数選択を解除する"""
        for p in list(self.selected_paths):
            i = self._path_to_index(p)
            if i >= 0:
                it = self.image_items[i]
                if it:
                    it.set_selected(False)
        self.selected_paths.clear()
        self.selected_item = None
        self.selected_item_path = ""

    def _select_all(self) -> None:
        """
        Ctrl+A: 現在のフォルダ（表示中の絞り込み結果）の画像を全選択する。
        フォルダアイテム（img_id == -2）は複数選択対象外のため除外する。
        """
        self._clear_multi_selection()
        last_idx = -1
        last_path = ""
        last_img_id = -1
        for i, (img_id, p, _, _, _) in enumerate(self.image_data):
            if img_id == -2:
                continue  # フォルダアイテムは対象外
            self.selected_paths.add(p)
            it = self.image_items[i]
            if it:
                it.set_selected(True)
            last_idx, last_path, last_img_id = i, p, img_id

        if last_idx >= 0:
            self._set_primary(last_idx, last_path, last_img_id)

    def _scroll_to_index(self, idx: int) -> None:
        """指定インデックスがビューポートに入るようスクロールする"""
        size = self.THUMB_SIZES[self._thumb_size_idx]
        row_pitch = size + 30 + 10
        cols = getattr(self, "_current_cols", 1)
        row = idx // max(cols, 1)
        y_min = row * row_pitch
        y_max = y_min + row_pitch
        bar = self.verticalScrollBar()
        sv = bar.value()
        vh = self.viewport().height()
        if y_min < sv:
            bar.setValue(y_min)
        elif y_max > sv + vh:
            bar.setValue(y_max - vh)

    def _on_item_dbl_clicked(self, img_id: int, path: str) -> None:
        """ダブルクリック: フォルダなら移動、画像ならSDIで開く"""
        if img_id == -2:
            self.folder_navigate.emit(path)
            return
        self.select_by_index(self._path_to_index(path))
        self.open_in_sdi.emit(path)

    def _path_to_index(self, path: str) -> int:
        for i, (img_id, p, _, _, _) in enumerate(self.image_data):
            if p == path:
                return i
        return -1

    def select_by_path(self, path: str) -> None:
        """外部から指定パスを単一選択する（複数選択は解除）"""
        self._clear_multi_selection()
        idx = self._path_to_index(path)
        if idx == -1:
            self.selected_item_path = path
            return
        img_id = self.image_data[idx][0]
        self.selected_paths.add(path)
        self._set_primary(idx, path, img_id)
        self._last_clicked_idx = idx

    # --- Task 3: 右クリックコンテキストメニュー ---
    def _on_context_menu(self, pos: QPoint) -> None:
        widget = self.childAt(pos)
        target_item: ThumbnailLabel | None = None
        while widget:
            if isinstance(widget, ThumbnailLabel):
                target_item = widget
                break
            widget = widget.parentWidget()

        if not target_item:
            return

        # バグ修正: 以前は右クリックした対象を無条件で select_by_path() に
        # 渡していた。select_by_path() は内部で複数選択を解除して単一選択
        # にするため、Ctrlキーで複数選択した状態のまま右クリックすると、
        # メニューが構築されるより前に選択がその1件だけに縮小されて
        # しまい、「削除」「移動」「コピー」がいずれも右クリックした
        # 1件にしか効かなくなっていた（複数選択が外れる不具合）。
        # 右クリックした対象が既に選択済み（複数選択の一員）なら選択状態
        # を維持し、選択されていない項目を右クリックした場合のみ
        # 単一選択に切り替える。
        if target_item.path not in self.selected_paths:
            self.select_by_path(target_item.path)

        menu = QMenu(self)

        open_action = QAction("関連付けで開く", self)
        open_action.triggered.connect(lambda: self._open_with_association(target_item.path))
        menu.addAction(open_action)

        open_sdi_action = QAction("画像ビューアで開く", self)
        open_sdi_action.triggered.connect(lambda: self.open_in_sdi.emit(target_item.path))
        menu.addAction(open_sdi_action)

        fullscreen_action = QAction("全画面表示", self)
        fullscreen_action.triggered.connect(lambda: self.open_in_sdi.emit(target_item.path))
        menu.addAction(fullscreen_action)

        menu.addSeparator()

        clip_image_action = QAction("画像をクリップボードへコピー", self)
        clip_image_action.triggered.connect(lambda: self._copy_image_to_clipboard(target_item.path))
        menu.addAction(clip_image_action)

        clip_path_action = QAction("パスをクリップボードへコピー", self)
        clip_path_action.triggered.connect(lambda: self._copy_paths_to_clipboard([target_item.path]))
        menu.addAction(clip_path_action)

        menu.addSeparator()

        copy_action = QAction("コピー先を選択してコピー...", self)
        move_action = QAction("移動先を選択して移動...", self)
        # バグ修正: 削除と同様、複数選択中に右クリックした対象がその
        # 一員であれば選択中の全ファイルを対象にする（以前はcopy/move
        # 常にtarget_item単体のみが対象で、複数選択への対応が
        # そもそも無かった）。
        if len(self.selected_paths) > 1 and target_item.path in self.selected_paths:
            copy_action.setText(f"選択中 {len(self.selected_paths)} 件をコピー...")
            move_action.setText(f"選択中 {len(self.selected_paths)} 件を移動...")
            copy_action.triggered.connect(lambda: self._copy_file())
            move_action.triggered.connect(lambda: self._move_file())
        else:
            copy_action.triggered.connect(lambda: self._copy_file(target_item))
            move_action.triggered.connect(lambda: self._move_file(target_item))
        menu.addAction(copy_action)
        menu.addAction(move_action)

        rename_action = QAction("名前の変更...", self)
        # バグ修正: F2ショートカット（rename_selected()）は複数選択時に
        # 何もしない設計だが、右クリックメニュー側はこのチェックが
        # 漏れており、複数選択中でも右クリックした1件だけを対象に
        # リネームが実行されてしまっていた（他の選択ファイルは無視され、
        # ユーザーには何も起きていないように見える危険な挙動）。
        # F2と同じ「複数選択時は無効化」に統一する。
        if len(self.selected_paths) > 1 and target_item.path in self.selected_paths:
            rename_action.setEnabled(False)
            rename_action.setToolTip("複数選択時は名前を変更できません")
        else:
            rename_action.triggered.connect(lambda: self._rename_file(target_item))
        menu.addAction(rename_action)

        menu.addSeparator()

        bulk_tag_action = QAction("選択画像へタグを一括追加/削除(&T)...", self)
        if len(self.selected_paths) > 1 and target_item.path in self.selected_paths:
            bulk_tag_action.setText(f"選択中 {len(self.selected_paths)} 件へタグを一括追加/削除(&T)...")
        bulk_tag_action.triggered.connect(self.bulk_tag_requested.emit)
        menu.addAction(bulk_tag_action)

        # 実機確認（A項目）フィードバック対応: 自動タグの結果に満足できない
        # 場合（例: 人間が写っていないのに 1girl/1boy が付く等）に、選択画像
        # だけ別の閾値/モデルで一回限りタグ付けをやり直せるようにする。
        # 個別設定の保存は行わない（ユーザー判断: 保持不要、自動タグ付け
        # 済みか否かだけを見ればよいため）。bulk_tag_action と同じ疎結合
        # パターン（シグナル経由でmain_window側が処理）に揃える。
        retag_settings_action = QAction("選択画像を別設定でタグ付けし直す(&R)...", self)
        if len(self.selected_paths) > 1 and target_item.path in self.selected_paths:
            retag_settings_action.setText(
                f"選択中 {len(self.selected_paths)} 件を別設定でタグ付けし直す(&R)..."
            )
        retag_settings_action.triggered.connect(self.retag_with_settings_requested.emit)
        menu.addAction(retag_settings_action)

        # LoRA作成支援機構（セッション27）: 選択画像（未選択なら絞り込み結果
        # 全体）を新規フォルダへコピー＋同名.txtキャプションとしてエクス
        # ポートする。retag_settings_action と同じ疎結合パターン。
        lora_export_action = QAction("LoRA用にエクスポート(&E)...", self)
        if len(self.selected_paths) > 1 and target_item.path in self.selected_paths:
            lora_export_action.setText(
                f"選択中 {len(self.selected_paths)} 件をLoRA用にエクスポート(&E)..."
            )
        lora_export_action.triggered.connect(self.lora_export_requested.emit)
        menu.addAction(lora_export_action)

        # 指示書03 タスクD: 「似たタグの画像を探す」
        # - 対象外行（フォルダ行 img_id==-2 / 未登録フォルダの画像 img_id==-1）は
        #   タグ自体が存在しないため非表示にする。
        # - この機能は単一の参照画像が前提のため、複数選択中に右クリックした
        #   場合も非表示にする（「名前の変更」の複数選択時の扱いと同じ考え方。
        #   ただしrename側は無効化=グレーアウト、こちらは項目自体を出さない
        #   方式。指示書の「非表示にする」という明示的な指定に合わせる）。
        is_multi_select = len(self.selected_paths) > 1 and target_item.path in self.selected_paths
        if target_item.img_id >= 0 and not is_multi_select:
            similar_search_action = QAction("似たタグの画像を探す", self)
            similar_search_action.triggered.connect(
                lambda: self._search_similar_tags(target_item.img_id)
            )
            menu.addAction(similar_search_action)

        menu.addSeparator()

        delete_action = QAction("削除...", self)
        delete_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        if len(self.selected_paths) > 1 and target_item.path in self.selected_paths:
            delete_action.setText(f"選択中 {len(self.selected_paths)} 件を削除...")
            delete_action.triggered.connect(lambda: self._delete_file())
        else:
            delete_action.triggered.connect(lambda: self._delete_file(target_item))
        menu.addAction(delete_action)

        menu.addSeparator()

        reload_action = QAction("サムネイル再読み込み", self)
        reload_action.triggered.connect(lambda: self._reload_single_item(target_item))
        menu.addAction(reload_action)

        menu.addSeparator()

        wallpaper_action = QAction("壁紙設定(&W)", self)
        wallpaper_action.triggered.connect(lambda: self._set_wallpaper(target_item.path))
        menu.addAction(wallpaper_action)

        menu.exec(self.mapToGlobal(pos))

    def _extract_similarity_tags(self, img_id: int) -> list[str]:
        """
        指示書03 タスクD: 類似検索用にタグを抽出する。
        character最大2件・general最大3件（rating/meta/copyright/artistは
        除外）。5件程度をAND条件にすると、レアなキャラ名・固有ポーズ等が
        重なって検索結果0件になりやすいための抑制。
        汎用タグ（1girl/solo等）は除外する。手動タグの表記揺れ
        （大文字・スペース区切り等）にも対応するため、比較対象・除外リスト
        双方を .lower().replace(" ", "_") で正規化してから比較する。
        単一image_idに対するインデックス検索1回・明示的なユーザー操作起点
        のため、既存の _add_manual_tag() 等と同様に同期SQL呼び出しでよい
        （指示書03改訂・優先度低の判断に準拠）。
        """
        import lifecycle_manager as _lm
        from workers import GENERIC_TAGS_FOR_SIMILARITY_SEARCH

        conn = _lm.get_connection()
        try:
            rows = conn.execute(
                "SELECT tag, category FROM tags WHERE image_id = ? "
                "AND category IN ('character', 'general')",
                (img_id,),
            ).fetchall()
        finally:
            conn.close()

        def _normalize(s: str) -> str:
            return s.lower().replace(" ", "_")

        excluded = {_normalize(t) for t in GENERIC_TAGS_FOR_SIMILARITY_SEARCH}

        characters = [t for t, cat in rows if cat == "character" and _normalize(t) not in excluded]
        generals = [t for t, cat in rows if cat == "general" and _normalize(t) not in excluded]

        return characters[:2] + generals[:3]

    def _search_similar_tags(self, img_id: int) -> None:
        """
        「似たタグの画像を探す」実行。抽出したタグをスペース区切りで
        main_window側へ渡す（similar_tag_search_requested シグナル経由。
        ThumbnailGridWidget は main_window への参照を持たない疎結合設計の
        ため、bulk_tag_requested と同じパターンに揃える）。
        """
        tags = self._extract_similarity_tags(img_id)
        if not tags:
            QMessageBox.information(
                self, "似たタグの画像を探す",
                "この画像には検索に使えるタグがありませんでした。"
            )
            return
        self.similar_tag_search_requested.emit(" ".join(tags))

    def _open_with_association(self, path: str) -> None:
        """関連付けで開く。関連付けなし・エラー時はダイアログ表示（クラッシュ防止）"""
        try:
            os.startfile(path)
        except OSError:
            QMessageBox.warning(
                self, "関連付けなし",
                f"このファイルを開くアプリケーションが見つかりません。\n\n"
                f"{os.path.basename(path)}\n\nファイルの種類に対応するアプリを関連付けてください。"
            )
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"ファイルを開けませんでした:\n{e}")

    def _copy_image_to_clipboard(self, path: str) -> None:
        from PyQt6.QtGui import QImageReader
        reader = QImageReader(path)
        qimg = reader.read()
        if not qimg.isNull():
            QGuiApplication.clipboard().setImage(qimg)

    def _resolve_targets(self, item: ThumbnailLabel | None) -> list[tuple[int, str, str]]:
        """
        コピー/移動/削除で共通して使う対象抽出ロジック。
        item指定時、複数選択の一員ならその全選択を対象にする。
        item未指定時はselected_pathsを対象にする。
        """
        if item is not None and len(self.selected_paths) <= 1:
            return [(item.img_id, item.path, item.filename)]
        elif self.selected_paths:
            targets = []
            for p in list(self.selected_paths):
                i = self._path_to_index(p)
                if i >= 0:
                    targets.append((self.image_data[i][0], p, os.path.basename(p)))
            return targets
        elif item is not None:
            return [(item.img_id, item.path, item.filename)]
        return []

    def _copy_file(self, item: ThumbnailLabel | None = None) -> None:
        from file_operation_dialog import FileOperationDialog
        targets = self._resolve_targets(item)
        if not targets:
            return

        current_dir = os.path.dirname(targets[0][1])
        dest_dir = FileOperationDialog.get_destination(
            is_move=False, current_dir=current_dir, file_count=len(targets), parent=self
        )
        if not dest_dir:
            return

        errors = []
        ok_count = 0
        for _img_id, src, fname in targets:
            dest = os.path.join(dest_dir, fname)
            try:
                if os.path.exists(dest):
                    reply = QMessageBox.question(self, "確認",
                        f"「{fname}」はコピー先に既に存在します。上書きしますか?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    if reply != QMessageBox.StandardButton.Yes:
                        continue
                shutil.copy2(src, dest)
                ok_count += 1
            except Exception as e:
                errors.append(f"{fname}: {e}")

        if ok_count:
            QMessageBox.information(self, "完了", f"{ok_count} 件のファイルをコピーしました:\n{dest_dir}")
        if errors:
            QMessageBox.critical(self, "エラー", "コピーに失敗したファイルがあります:\n" + "\n".join(errors))

    def _move_file(self, item: ThumbnailLabel | None = None) -> None:
        import lifecycle_manager as _lm
        from file_operation_dialog import FileOperationDialog
        targets = self._resolve_targets(item)
        if not targets:
            return

        current_dir = os.path.dirname(targets[0][1])
        dest_dir = FileOperationDialog.get_destination(
            is_move=True, current_dir=current_dir, file_count=len(targets), parent=self
        )
        if not dest_dir:
            return

        errors = []
        moved_paths = []
        for img_id, src, fname in targets:
            dest = os.path.join(dest_dir, fname).replace("\\", "/")
            try:
                if os.path.exists(dest):
                    reply = QMessageBox.question(self, "確認",
                        f"「{fname}」は移動先に既に存在します。上書きしますか?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    if reply != QMessageBox.StandardButton.Yes:
                        continue
                shutil.move(src, dest)
                if img_id and img_id != -1:
                    try:
                        _c = _lm.get_connection()
                        _c.execute(
                            "UPDATE images SET path = ?, status = 'ACTIVE' WHERE id = ?",
                            (dest, img_id)
                        )
                        _c.commit()
                        _c.close()
                    except Exception:
                        pass
                moved_paths.append(src)
            except Exception as e:
                errors.append(f"{fname}: {e}")

        if moved_paths:
            # バグ修正: 以前は単一ファイル専用に _remove_item_from_grid(item)
            # を呼んでいたため、複数選択時にまとめて移動しても実際には
            # グリッドから1件しか除去されず、他は移動済みなのにサムネイル
            # ビューに残り続けているように見えていた（「移動したはずの
            # ファイルが残っている」不具合）。削除と同じバッチ除去を使う。
            self._remove_items_from_grid_batch(moved_paths)
            self.selected_paths -= set(moved_paths)
            self.file_operation_done.emit()
        if errors:
            QMessageBox.critical(self, "エラー", "移動に失敗したファイルがあります:\n" + "\n".join(errors))

    # --- Ctrl+C / Ctrl+X / Ctrl+V: OSクリップボード経由のファイル操作 ---
    #
    # 素の "C"/"V" キー（Linar準拠、宛先ダイアログを開く即時コピー/
    # ビューア起動）とは別物。Ctrl+C/Ctrl+X/Ctrl+V は QShortcut として
    # main_window.py 側でウィンドウ全体に登録され、Qtの修飾キー完全一致
    # 判定によって素のキーとは衝突しない（このクラスの keyPressEvent 内の
    # 素キー判定は修飾キーを見ていないため、もし Ctrl+C 等をここで直接
    # 処理すると素の "C" と同時発火してしまう。そのため実装場所を分離
    # している）。
    #
    # "Preferred DropEffect" は Windows Explorer が切り取り/コピーを
    # 区別するために使う非標準クリップボード形式（DWORD、1=COPY, 2=MOVE）。
    # これを載せておくことで、D-liner⇔Explorer間の相互コピペで
    # 切り取り/コピーの意図が正しく伝わる。

    _DROP_EFFECT_FORMAT = "Preferred DropEffect"
    _DROPEFFECT_COPY = 1
    _DROPEFFECT_MOVE = 2

    def _stage_clipboard_urls(self, paths: list[str], is_cut: bool) -> None:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        effect = self._DROPEFFECT_MOVE if is_cut else self._DROPEFFECT_COPY
        mime.setData(self._DROP_EFFECT_FORMAT, QByteArray(struct.pack("<I", effect)))
        QGuiApplication.clipboard().setMimeData(mime)

    def copy_selected_to_clipboard(self) -> None:
        """Ctrl+C: 選択ファイルをOSクリップボードにコピー登録する。"""
        targets = self._resolve_targets(self.selected_item)
        if not targets:
            return
        self._stage_clipboard_urls([t[1] for t in targets], is_cut=False)

    def cut_selected_to_clipboard(self) -> None:
        """Ctrl+X: 選択ファイルをOSクリップボードに切り取り登録する。"""
        targets = self._resolve_targets(self.selected_item)
        if not targets:
            return
        self._stage_clipboard_urls([t[1] for t in targets], is_cut=True)

    def paste_from_clipboard(self, dest_dir: str) -> None:
        """
        Ctrl+V: OSクリップボード上のファイルを dest_dir に貼り付ける。

        貼り付け元はD-liner自身のCtrl+C/Ctrl+Xに限らず、エクスプローラー
        等の外部アプリでコピー/切り取りされたファイルも受け付ける
        （クリップボードの text/uri-list を見るだけで出所を問わない）。
        "Preferred DropEffect" が無い場合はコピー扱いとする（デフォルトの
        Explorer挙動に合わせる）。
        """
        if not dest_dir or not os.path.isdir(dest_dir):
            return

        mime = QGuiApplication.clipboard().mimeData()
        if not mime.hasUrls():
            return

        src_paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        if not src_paths:
            return

        is_cut = False
        raw = mime.data(self._DROP_EFFECT_FORMAT)
        if raw and len(raw) >= 4:
            try:
                effect = struct.unpack("<I", bytes(raw)[:4])[0]
                is_cut = (effect == self._DROPEFFECT_MOVE)
            except struct.error:
                is_cut = False

        import lifecycle_manager as _lm

        errors = []
        ok_count = 0
        moved_src_paths = []
        for src in src_paths:
            if not os.path.isfile(src):
                errors.append(f"{src}: ファイルが見つかりません")
                continue
            fname = os.path.basename(src)
            dest = os.path.join(dest_dir, fname)
            if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dest)):
                # 同一フォルダへの貼り付けは無視（コピー元＝貼り付け先）
                continue
            try:
                if os.path.exists(dest):
                    reply = QMessageBox.question(
                        self, "確認",
                        f"「{fname}」は貼り付け先に既に存在します。上書きしますか?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        continue

                if is_cut:
                    dest = dest.replace("\\", "/")
                    shutil.move(src, dest)
                    moved_src_paths.append(src)
                    # 貼り付け元がDB登録済み画像（D-liner内Ctrl+X、または
                    # 既存の監視フォルダ配下の外部ファイル）であればパスを
                    # 追従させる。未登録（DB外からの切り取り）なら何もしない。
                    try:
                        norm_src = src.replace("\\", "/")
                        conn = _lm.get_connection()
                        conn.execute(
                            "UPDATE images SET path = ?, status = 'ACTIVE' WHERE path = ?",
                            (dest, norm_src),
                        )
                        conn.commit()
                        conn.close()
                    except Exception:
                        pass
                else:
                    shutil.copy2(src, dest)
                ok_count += 1
            except Exception as e:
                errors.append(f"{fname}: {e}")

        if is_cut and moved_src_paths:
            # 切り取り貼り付けの場合、貼り付け後は同じ内容を再度貼り付け
            # できてしまう（Explorer同様の挙動だが、移動済みファイルへの
            # 誤操作を防ぐためクリップボードは消費しておく）。
            QGuiApplication.clipboard().clear()
            self._remove_items_from_grid_batch(moved_src_paths)
            self.selected_paths -= set(moved_src_paths)

        if ok_count:
            self.file_operation_done.emit()
        if errors:
            QMessageBox.critical(self, "エラー", "貼り付けに失敗したファイルがあります:\n" + "\n".join(errors))

    def rename_selected(self) -> None:
        """
        F2: 選択中1件をリネームする。複数選択時は何もしない
        （Explorer同様、複数選択でのF2一括リネームは非対応。
        一括リネームは既存の「ファイル」メニューの専用ダイアログを使う）。
        """
        if self.selected_item is None or len(self.selected_paths) > 1:
            return
        self._rename_file(self.selected_item)

    def _rename_file(self, item: ThumbnailLabel) -> None:
        old_path = item.path
        old_name = item.filename
        new_name, ok = QInputDialog.getText(self, "名前の変更", "新しいファイル名:", text=old_name)
        if not ok or not new_name.strip() or new_name == old_name:
            return
        new_name = new_name.strip()
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        try:
            if os.path.exists(new_path):
                QMessageBox.warning(self, "エラー", f"「{new_name}」は既に存在します。")
                return
            os.rename(old_path, new_path)
            self.file_operation_done.emit()
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"リネームに失敗しました:\n{e}")

    def _confirm_delete(self, targets: list[tuple[int, str, str]]) -> bool:
        """削除確認ダイアログ（1件・複数件共通）。実体は show_delete_confirm_dialog()。"""
        if len(targets) == 1:
            _, path, fname = targets[0]
            msg_text = f"「{fname}」を削除しますか?\n\n{path}"
        else:
            preview = "\n".join(f"  {t[2]}" for t in targets[:8])
            if len(targets) > 8:
                preview += f"\n  ... 他 {len(targets) - 8} 件"
            msg_text = f"{len(targets)} 件のファイルを削除しますか?\n\n{preview}"
        return show_delete_confirm_dialog(self, msg_text)

    def _delete_file(self, item: ThumbnailLabel | None = None) -> None:
        """
        削除処理。
        複数選択中(selected_paths > 1)はまとめて削除。
        単一の場合は item または selected_paths を対象にする。
        """
        import lifecycle_manager as _lm

        # バグ修正: 以前は単一選択時に self.selected_item（画面上の
        # ウィジェット参照）が None かどうかで判定していた。仮想スクロール
        # は画面外に出たアイテムのウィジェットを破棄して None に戻す
        # ため、選択自体（selected_item_path/selected_paths、こちらは
        # パス文字列でありウィジェットの生存に依存しない）は有効なのに
        # ウィジェットだけ None というケースが起こりうる。その場合
        # 従来はどの分岐にも当てはまらず else: return で削除が完全に
        # 無反応になっていた。_resolve_targets() はウィジェット参照では
        # なく selected_paths（データ）を正として対象を決定する
        # （copy/moveと共通のロジック）。
        targets = self._resolve_targets(item)
        if not targets:
            return

        # send2trash が使えない環境（setup_runtime_env.py 再実行前の
        # 既存インストール等）では、確認ダイアログを出す前に一度だけ
        # 判定し、その場合は「ゴミ箱を使わない完全削除になる」ことを
        # 明示して同意を取る。誤削除時に復元できなくなるため、これを
        # 黙って permanently delete にフォールバックさせない。
        try:
            import send2trash  # noqa: F401
            use_trash = True
        except ImportError:
            use_trash = False
            reply = QMessageBox.warning(
                self, "ゴミ箱機能が利用できません",
                "send2trash パッケージが見つからないため、削除は元に戻せない"
                "完全削除になります。\n"
                "（setup_runtime_env.bat を再実行するとゴミ箱経由の削除が"
                "有効になります）\n\n"
                "このまま完全削除で続行しますか?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # 確認ダイアログ（件数によらず共通の1つのダイアログを使う）
        if not self._confirm_delete(targets):
            return

        errors = []
        deleted = []
        for img_id, path, fname in targets:
            try:
                # バグ修正: img_id == -2 はフォルダ行（workers.py側で
                # (-2, path, 0, 0, 0) として生成される、通常の画像ファイル
                # ではない）。これを他のファイルと同じ扱いで os.remove()
                # に渡すと、Windowsではディレクトリに対して常に
                # [WinError 5] アクセスが拒否されました、になる
                # （os.remove/DeleteFile はファイル専用APIでディレクトリ
                # には使えないため）。send2trash はディレクトリ・ファイル
                # 双方に対応しているため、通常はこちらが優先される
                # （setup_runtime_env.py の COMMON_PACKAGES に追加済み）。
                # 万一 send2trash が無い環境では、上記の事前確認を経た
                # 上でフォルダ/ファイルに応じて rmtree/os.remove を使う。
                is_folder = (img_id == -2)
                if use_trash:
                    # バグ修正: send2trashのWindows実装は内部で拡張長パス
                    # プレフィックス(\\?\)を付与してWin32 APIを呼ぶが、
                    # \\?\ 付きパスはOS側の "/" → "\" 自動変換が働かない。
                    # D-linerはパスを "/" 区切りで保持しているため、
                    # os.remove()（\\?\ プレフィックス無し、自動変換あり）
                    # では問題なかったが、send2trash に渡す直前では
                    # os.path.normpath() でOSネイティブ区切りに変換して
                    # おく必要がある。これをしないと [Errno 2] 指定された
                    # ファイルが見つかりません、になる（フォルダに限らず
                    # 通常ファイルの削除でも同様に発生する）。
                    send2trash.send2trash(os.path.normpath(path))
                elif is_folder:
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                # DB更新（フォルダ行はDBの images テーブルに対応する
                # レコードを持たないため img_id == -2 では実行しない）
                if img_id and img_id >= 0:
                    try:
                        _c = _lm.get_connection()
                        _c.execute(
                            "UPDATE images SET status = 'DELETED' WHERE id = ?",
                            (img_id,)
                        )
                        _c.commit()
                        _c.close()
                    except Exception:
                        pass
                deleted.append(path)
            except Exception as e:
                errors.append(f"{fname}: {e}")

        # グリッドから除去（一括）
        # 削除件数が多いと、1件ごとに _remove_item_from_grid() を呼ぶと
        # image_data.pop() のO(N)探索 + 毎回の _update_grid_size() /
        # load_visible_thumbnails() 再描画がN回発生し、1000件規模の
        # 一括削除でUIがフリーズするため、まとめて1回だけ更新する。
        self._remove_items_from_grid_batch(deleted)
        self.selected_paths -= set(deleted)
        self.file_operation_done.emit()

        if errors:
            QMessageBox.critical(
                self, "削除エラー",
                "削除に失敗したファイルがあります:\n" + "\n".join(errors)
            )

    def _set_wallpaper(self, path: str) -> None:
        """壁紙設定"""
        try:
            import ctypes
            SPI_SETDESKWALLPAPER = 20
            win_path = path.replace("/", "\\")
            result = ctypes.windll.user32.SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, win_path, 3)
            if result:
                QMessageBox.information(self, "壁紙設定", f"「{os.path.basename(path)}」を壁紙に設定しました。")
            else:
                QMessageBox.warning(self, "壁紙設定", "壁紙の設定に失敗しました。")
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"壁紙設定に失敗しました:\n{e}")

    def _remove_item_from_grid(self, item: ThumbnailLabel) -> None:
        """アイテムをリストから即時除去する"""
        idx = self._path_to_index(item.path)
        if idx == -1:
            return

        is_selected = (self.selected_item_path == item.path)
        
        if item.path in self.cache:
            del self.cache[item.path]

        self.image_data.pop(idx)
        self.image_items.pop(idx)
        
        item.hide()
        item.deleteLater()

        if is_selected:
            self.selected_item_path = ""
            self.selected_item = None
            if self.image_data:
                next_idx = min(idx, len(self.image_data) - 1)
                self.select_by_index(next_idx)

        self._update_grid_size()
        self.load_visible_thumbnails()

    def _remove_items_from_grid_batch(self, paths: list[str]) -> None:
        """
        複数アイテムをまとめてリストから除去する（_remove_item_from_gridの
        一括版）。1件ずつ呼ぶと image_data.pop() のO(N)探索 + 毎回の
        _update_grid_size()/load_visible_thumbnails() 再描画がN回走り、
        大量選択削除時にUIがフリーズするため、フィルタリングと再描画を
        それぞれ1回にまとめる。
        """
        remove_set = set(paths)
        if not remove_set:
            return

        # バグ修正: 削除前の選択位置(idx)を先に記録しておき、削除後は
        # 「同じ位置に来る次の要素」を選択する。以前は無条件に
        # select_by_index(0) していたため、右クリック削除のたびに
        # フォルダの先頭へ選択位置が飛んでしまっていた
        # （_remove_item_from_grid の単体削除では元々 next_idx で
        # 位置を保持していたのに、一括版へ統合した際に欠落していた）。
        was_selected = self.selected_item_path in remove_set
        prev_idx = self._path_to_index(self.selected_item_path) if was_selected else -1

        new_image_data = []
        new_image_items = []
        for data, it in zip(self.image_data, self.image_items):
            path = data[1]
            if path in remove_set:
                if path in self.cache:
                    del self.cache[path]
                if it is not None:
                    it.hide()
                    it.deleteLater()
                continue
            new_image_data.append(data)
            new_image_items.append(it)

        self.image_data = new_image_data
        self.image_items = new_image_items

        if was_selected:
            self.selected_item_path = ""
            self.selected_item = None
            if self.image_data:
                next_idx = min(prev_idx, len(self.image_data) - 1)
                self.select_by_index(next_idx)

        self._update_grid_size()
        self.load_visible_thumbnails()

    def _reload_single_item(self, item: ThumbnailLabel) -> None:
        if item.path in self.cache:
            del self.cache[item.path]
        self._trigger_load(item)

    def _copy_paths_to_clipboard(self, paths: list[str]) -> None:
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("\n".join(paths))

    def select_by_index(self, idx: int) -> None:
        """image_data のインデックスで選択。範囲外はクランプ。"""
        if not self.image_data:
            return
        idx = max(0, min(idx, len(self.image_data) - 1))
        _, path, _, _, _ = self.image_data[idx]
        self.select_by_path(path)

    def current_index(self) -> int:
        """現在選択中のアイテムのインデックス。未選択は -1。"""
        if not self.selected_item_path:
            return -1
        return self._path_to_index(self.selected_item_path)

    def _cols_count(self) -> int:
        return getattr(self, "_current_cols", max(1, self.width() // 185))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """
        ←→↑↓ でグリッド内を移動。PageUp/Down でページ送り。
        """
        key = event.key()
        cols = self._cols_count()
        idx = self.current_index()
        if idx == -1 and self.image_data:
            idx = 0

        if key == Qt.Key.Key_A and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            # Ctrl+A: 現在のフォルダの画像を全選択
            # （LoRAトリガーワードの一括セット/除去等の用途を想定）
            self._select_all()
        elif key == Qt.Key.Key_Right:
            self.select_by_index(idx + 1)
        elif key == Qt.Key.Key_Left:
            self.select_by_index(idx - 1)
        elif key == Qt.Key.Key_Down:
            self.select_by_index(idx + cols)
        elif key == Qt.Key.Key_Up:
            self.select_by_index(idx - cols)
        elif key == Qt.Key.Key_PageDown:
            self.select_by_index(idx + cols * 3)
        elif key == Qt.Key.Key_PageUp:
            self.select_by_index(idx - cols * 3)
        elif key in (Qt.Key.Key_Home,):
            self.select_by_index(0)
        elif key in (Qt.Key.Key_End,):
            self.select_by_index(len(self.image_data) - 1)
        elif key == Qt.Key.Key_Delete:
            # バグ修正: C/M/V キー（Linarベアキー方式）は self.selected_item
            # （選択中ウィジェットへの参照）を直接 _copy_file()/_move_file()
            # へ渡しているのに対し、Deleteキーだけは _delete_file() を
            # 引数無しで呼び、_resolve_targets() 内部の selected_paths
            # 依存の分岐だけに委ねていた。右クリック削除はコンテキスト
            # メニュー側で target_item を直接渡すため確実に動作するのに、
            # Deleteキー削除だけが無反応になる非対称性があったため、
            # 他のキー同様 self.selected_item を明示的に渡すようにする。
            if self.selected_item is not None or self.selected_paths:
                self._delete_file(self.selected_item)
        elif key == Qt.Key.Key_C:
            # C: 選択画像をコピー（Linar準拠）
            if self.selected_item is not None:
                self._copy_file(self.selected_item)
        elif key == Qt.Key.Key_M:
            # M: 選択画像を移動（Linar準拠）
            if self.selected_item is not None:
                self._move_file(self.selected_item)
        elif key == Qt.Key.Key_V:
            # V: 選択画像をSDIで表示（Linar準拠）
            if self.selected_item is not None:
                self.open_in_sdi.emit(self.selected_item.path)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Enter/Return: 選択画像をSDIで表示（ダブルクリック・Vキーと同じ動作）
            if self.selected_item is not None:
                self.open_in_sdi.emit(self.selected_item.path)
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            # + : サムネイル拡大 (Linar準拠)
            self._change_thumb_size(+1)
        elif key == Qt.Key.Key_Minus:
            # - : サムネイル縮小 (Linar準拠)
            self._change_thumb_size(-1)
        elif key == Qt.Key.Key_T:
            # T: 選択画像へタグを一括追加/削除
            # （LoRAトリガーワードの一括セット/除去等の用途を想定）
            self.bulk_tag_requested.emit()
        else:
            super().keyPressEvent(event)

    # --- D&D ドロップ受け入れ (フォルダツリーやOS等からのドロップ対応) ---
    def dragEnterEvent(self, event) -> None:
        """ファイルURLを含むD&Dのみ受け入れる"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        """
        外部（OS、フォルダツリー等）からのD&Dでファイルをこのフォルダへコピー/移動する。
        - Shift押下またはMoveAction: 移動
        - それ以外: コピー
        """
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if not urls:
            event.ignore()
            return
        is_move = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier) or \
                  (event.proposedAction() == Qt.DropAction.MoveAction)
        paths = [u.toLocalFile().replace("\\", "/") for u in urls]
        self.drop_requested.emit(paths, is_move)
        event.acceptProposedAction()

    def wheelEvent(self, event: QWheelEvent) -> None:
        """
        1行単位での垂直スクロールに変更する。
        """
        if not self.image_data:
            super().wheelEvent(event)
            return

        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return

        size = self.THUMB_SIZES[self._thumb_size_idx]
        item_h = size + 30
        spacing = 10
        row_pitch = item_h + spacing

        steps = delta / 120.0
        scroll_amount = int(steps * row_pitch)

        bar = self.verticalScrollBar()
        new_val = bar.value() - scroll_amount
        # setValue() が実際に値を変えれば valueChanged → 間引き付きの
        # _on_scroll_value_changed() 経由で load_visible_thumbnails() が
        # 呼ばれる。以前はここでも明示的に呼んでおり、ホイール1目盛り
        # ごとに二重実行されていた。
        bar.setValue(max(bar.minimum(), min(new_val, bar.maximum())))
        event.accept()
