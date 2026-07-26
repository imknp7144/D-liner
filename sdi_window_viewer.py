"""
sdi_window_viewer.py — SDI（単一画像表示）ウィンドウ本体
======================================================================
セッション27〜29の高速化・保守性検討（候補2・第2段階）により、
タグパネル関連クラス（tag_panel.py）・画像描画クラス（sdi_image_label.py）を
機械的に分離した。このファイルには SDIWindow 本体のみが残っている。

【重要】この分割はクラス定義の「移動」のみを目的としており、
ロジックの変更は一切行っていない（メソッド本文は1文字も変えていない）。
main_window.py 側の `from sdi_window_viewer import SDIWindow` は
変更不要（このファイルが引き続き SDIWindow を公開する）。
"""

from __future__ import annotations

import os
import sys
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QSize, QTimer, QSettings, QStringListModel
from PyQt6.QtGui import (
    QImage, QPixmap, QPainter, QTransform, QKeyEvent, QAction, 
    QGuiApplication, QPalette, QColor, QFont, QCursor, QIcon, QPen
)
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QScrollArea, QStatusBar, QMenuBar, QMenu, QMessageBox,
    QFileDialog, QSizePolicy, QPushButton, QFrame, QToolButton,
    QDialog, QLineEdit, QDialogButtonBox, QListWidget, QListWidgetItem,
    QCompleter
)

# バグ修正: 削除確認ダイアログのデザイン・既定ボタンがサムネイルビュー側
# (thumbnail_grid.py) と不一致だったため、共通の実装を共有する。
from thumbnail_grid import show_delete_confirm_dialog

# セッション27〜29の分割により追加: タグパネル関連・画像描画クラスは
# それぞれ専用ファイルへ移動済み（ロジック変更なしの機械的移動）。
from tag_panel import TagPanel
from sdi_image_label import SDIImageLabel


class SDIWindow(QMainWindow):
    """
    Linar仕様に完全準拠した単一画像表示用SDIウインドウ
    """
    closed = pyqtSignal(str)  # 閉じたときにパスを通知する
    selection_request = pyqtSignal(str)  # メインウインドウに選択同期をリクエスト

    # smart モードでの最小表示エリアサイズ（論理px）の決定方法。
    # HD/2.5K/4K等、ディスプレイ解像度によって適切な絶対値は変わるため、
    # 固定pxではなく画面の使用可能領域（availableGeometry）に対する
    # 比率で動的に計算する。画面の短辺の10%を幅・高さ共通の最小値とする
    # （例: HD 1920x1080 → 短辺1080の10% = 108px、4K 3840x2160 →
    # 短辺2160の10% = 216px）。極端に小さい画面でも最低限は確保する
    # よう、下限のみ設けている（上限は設けない。大画面ほど画像を
    # 見やすい大きさで表示したいという自然な要求に合わせるため）。
    MIN_DISPLAY_SHORT_EDGE_RATIO = 0.10
    MIN_DISPLAY_FLOOR = 100  # 極端に小さい画面向けの最低保証値

    def _min_display_size(self, available_w: int, available_h: int) -> tuple[int, int]:
        """
        現在の画面の使用可能領域サイズから、smart モードでの最小表示
        エリアサイズ（論理px）を動的に算出する。画面の短辺の10%を
        幅・高さ共通の最小値として使う。
        """
        short_edge = min(available_w, available_h)
        min_side = max(self.MIN_DISPLAY_FLOOR, int(short_edge * self.MIN_DISPLAY_SHORT_EDGE_RATIO))
        return min_side, min_side

    def __init__(self, file_path: str, all_files: list[str] = [], main_window=None, parent=None) -> None:
        super().__init__(parent)  # parent=None → 独立トップレベルウィンドウ
        self._main_window = main_window   # Qt親子関係とは別に保持
        self.file_path = file_path.replace("\\", "/")
        self.all_files = [f.replace("\\", "/") for f in all_files]
        self.current_index = 0
        if self.file_path in self.all_files:
            self.current_index = self.all_files.index(self.file_path)
        else:
            self.all_files.append(self.file_path)
            self.current_index = len(self.all_files) - 1

        self.linked_mode = False  # 操作連動
        self._first_show_done = False   # show()後の初回再描画フラグ
        self._is_loading = False        # ロード中フラグ（連続ホイール/キー入力の多重ロード防止）
        self.init_ui()

        # 前回の表示モードを復元
        saved_mode = QSettings("D-liner", "D-liner").value("sdi/fit_mode", "smart")
        self.set_fit_mode(saved_mode)

        self.load_image(self.file_path)

    def init_ui(self) -> None:
        # 実機回帰チェックリストで発覚: window/window_aspect/width モードは
        # 画像サイズに自動追従しないため、前回closeEvent()で記憶した
        # サイズがあればそれを復元する。raw/smart はこの直後の
        # set_fit_mode()→load_image()の流れで_auto_resize_window_if_raw()
        # が画像サイズに合わせて上書きするため、800x600のままでよい
        # （復元しても意味が無いのと、raw/smart中に記憶されたサイズを
        # 誤って復元しないようにするため、fit_modeの判定を挟む）。
        _init_settings = QSettings("D-liner", "D-liner")
        _saved_fit_mode = _init_settings.value("sdi/fit_mode", "smart")
        if _saved_fit_mode in ("window", "window_aspect", "width"):
            _saved_w = int(_init_settings.value("sdi/window_size_w", 800))
            _saved_h = int(_init_settings.value("sdi/window_size_h", 600))
            self.resize(_saved_w, _saved_h)
        else:
            self.resize(800, 600)
        # WA_DeleteOnClose: 閉じた瞬間にC++オブジェクトも破棄しメモリリークを防ぐ
        # main_window側はclosedシグナル+sender()でリスト管理するため isHidden() は使用しない
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        # 背景色をダークテーマに
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#121212"))
        self.setPalette(palette)

        # --- centralWidget: 画像エリア + タグパネルを縦に並べるコンテナ ---
        central_container = QWidget(self)
        central_container.setStyleSheet("background-color: #121212;")
        central_layout = QVBoxLayout(central_container)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        # スクロールエリア（画像）
        self.scroll_area = QScrollArea(central_container)
        self.scroll_area.setWidgetResizable(False)  # smart/rawモードではラベルを固定サイズで管理
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll_area.setStyleSheet("""
            QScrollArea { border: none; background-color: #121212; }
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
            QScrollBar:horizontal {
                background: #2a2a2a;
                height: 10px;
                margin: 0px;
                border-radius: 5px;
            }
            QScrollBar::handle:horizontal {
                background: #5a8ab5;
                min-width: 24px;
                border-radius: 5px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #4db3ff;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                background: none;
            }
        """)

        self.image_label = SDIImageLabel(self.scroll_area)
        self.image_label._scroll_area = self.scroll_area  # ビューポートサイズ参照用
        self.image_label._sdi_window = self               # ウィンドウ内側サイズ参照用
        self.scroll_area.setWidget(self.image_label)

        central_layout.addWidget(self.scroll_area, stretch=1)

        # AI自動タグ付け ロック/アンロック ボタン（画像右上への浮遊オーバーレイ）。
        # central_layout には addWidget しない＝レイアウトのフロー外に置く
        # ことで、タグパネルの高さ計算・折り返しロジック（過去に何度も
        # バグを踏んだ繊細な部分）に一切影響を与えない設計にしている。
        # 位置は resizeEvent / showEvent 側で毎回 _reposition_lock_btn() を
        # 呼んで固定し直す。
        self._lock_target_image_id: int = -1
        self.lock_btn = QPushButton("🔓", central_container)
        self.lock_btn.setFixedSize(32, 32)
        self.lock_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.lock_btn.clicked.connect(self._on_lock_btn_clicked)
        self.lock_btn.setVisible(False)
        self.lock_btn.raise_()

        # タグパネル（下部、タグがある時だけ表示）
        self.tag_panel = TagPanel(central_container)
        self.tag_panel.set_main_window(self._main_window)
        central_layout.addWidget(self.tag_panel, stretch=0)
        # バグ修正: タグパネルの高さは非同期取得完了後に確定するため、
        # load_image() 時点のリサイズ計算だけでは間に合わない
        # （初回表示・小解像度画像でスクロールバーが出る原因）。
        # 高さが実際に変化した瞬間に、現在表示中の画像サイズで
        # リサイズ計算をやり直す。
        self.tag_panel.panel_resized.connect(self._on_tag_panel_resized)

        self.setCentralWidget(central_container)

        # ステータスバー
        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)

        self.create_menu_bar()
        self._ensure_overflow_icon_visible()

    def create_menu_bar(self) -> None:
        self.menu_bar = QMenuBar(self)
        self.setMenuBar(self.menu_bar)

        # バグ修正: ウィンドウが狭くメニューの一部が収まらない場合、
        # Qt標準の「>>」オーバーフローボタンが表示されるが、ダーク
        # テーマ下では矢印の色が背景に近く視認しづらいという報告が
        # あった（Linarの明るいテーマでは自然に見えていたが、D-liner
        # のダーク背景では埋もれてしまう）。
        # 追記: スタイルシートの color 指定とパレット上書きだけでは
        # 解消しないことが実機で確認された。原因はWindowsのネイティブ
        # スタイル（windowsvista等）が、この矢印をQt側のパレット/
        # スタイルシートを経由せず、OSのテーマ描画APIで直接描画して
        # いるためと考えられる（Qtでよく知られる制約）。そのため、
        # 下記 _ensure_overflow_icon_visible() で、オーバーフロー用の
        # QToolButton を実際に探し出し、自前で描いた明るい色の矢印
        # アイコンを直接セットすることで、OS側の描画を完全に迂回する。
        self.menu_bar.setStyleSheet("""
            QMenuBar {
                background-color: #1e1e1e;
                color: #e8e8e8;
            }
            QMenuBar::item {
                background-color: transparent;
                color: #e8e8e8;
                padding: 4px 8px;
            }
            QMenuBar::item:selected {
                background-color: #3a3a3a;
            }
            QMenuBar QToolButton {
                background-color: #1e1e1e;
                color: #e8e8e8;
                border: none;
                padding: 2px 6px;
            }
            QMenuBar QToolButton:hover {
                background-color: #3a3a3a;
            }
        """)
        menu_bar_palette = self.menu_bar.palette()
        light_text = QColor("#e8e8e8")
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.ButtonText,
            QPalette.ColorRole.Text,
        ):
            menu_bar_palette.setColor(role, light_text)
        self.menu_bar.setPalette(menu_bar_palette)

        # --- ファイル (F) ---
        file_menu = self.menu_bar.addMenu("ファイル(&F)")
        
        copy_action = QAction("複写(&C)...", self)
        copy_action.setShortcut("C")
        copy_action.triggered.connect(self.file_copy)
        file_menu.addAction(copy_action)

        move_action = QAction("移動(&M)...", self)
        move_action.setShortcut("M")
        move_action.triggered.connect(self.file_move)
        file_menu.addAction(move_action)

        del_action = QAction("削除(&D)", self)
        del_action.setShortcut("Delete")
        del_action.triggered.connect(self.file_delete)
        file_menu.addAction(del_action)

        file_menu.addSeparator()

        save_action = QAction("ファイル保存(&S)", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self.file_save)
        file_menu.addAction(save_action)

        print_action = QAction("印刷(&P)...", self)
        print_action.setShortcut("Ctrl+P")
        print_action.triggered.connect(self.file_print)
        file_menu.addAction(print_action)

        # --- 画像 (G) ---
        img_menu = self.menu_bar.addMenu("画像(&G)")
        
        zoom_in_act = QAction("拡大(&U)", self)
        zoom_in_act.setShortcut("+")
        zoom_in_act.triggered.connect(self.zoom_in)
        img_menu.addAction(zoom_in_act)

        zoom_out_act = QAction("縮小(&D)", self)
        zoom_out_act.setShortcut("-")
        zoom_out_act.triggered.connect(self.zoom_out)
        img_menu.addAction(zoom_out_act)

        raw_size_act = QAction("原寸(&O)", self)
        raw_size_act.setShortcut("Num+0")
        raw_size_act.triggered.connect(self.zoom_reset)
        img_menu.addAction(raw_size_act)

        img_menu.addSeparator()

        rot_l_act = QAction("左回転(&L)", self)
        rot_l_act.setShortcut("L")
        rot_l_act.triggered.connect(self.rotate_left)
        img_menu.addAction(rot_l_act)

        rot_r_act = QAction("右回転(&R)", self)
        rot_r_act.setShortcut("R")
        rot_r_act.triggered.connect(self.rotate_right)
        img_menu.addAction(rot_r_act)

        img_menu.addSeparator()

        flip_v_act = QAction("上下反転(&U)", self)
        flip_v_act.setShortcut("U")
        flip_v_act.triggered.connect(self.flip_vertical)
        img_menu.addAction(flip_v_act)

        flip_h_act = QAction("左右反転(&H)", self)
        # バグ修正: move_action（移動...）と共に "M" が割り当てられており、
        # 同一ウィンドウ内でショートカットが重複（曖昧）していたため、
        # Qtはどちらのアクションも発火させていなかった（Mキーで移動が
        # 効かない不具合の直接原因）。移動側はthumbnail_grid.py側のMキー
        # 移動操作とも一致させる必要があるため、こちらを別キーに変更する。
        flip_h_act.setShortcut("H")
        flip_h_act.triggered.connect(self.flip_horizontal)
        img_menu.addAction(flip_h_act)

        img_menu.addSeparator()

        wallpaper_act = QAction("壁紙設定(&W)", self)
        wallpaper_act.triggered.connect(self.set_wallpaper)
        img_menu.addAction(wallpaper_act)

        # --- 編集 (E) ---
        edit_menu = self.menu_bar.addMenu("編集(&E)")
        
        clip_act = QAction("クリップボードにコピー(&C)", self)
        clip_act.setShortcut("Ctrl+C")
        clip_act.triggered.connect(self.copy_to_clipboard)
        edit_menu.addAction(clip_act)

        edit_menu.addSeparator()

        # 指示書02 タスクB: T キーで「タグを追加」ダイアログを直接起動できるように。
        # 現状 T は未使用であることを確認済み。
        add_tag_act = QAction("タグを追加(&T)...", self)
        add_tag_act.setShortcut("T")
        add_tag_act.triggered.connect(lambda: self.tag_panel.open_add_tag_dialog())
        edit_menu.addAction(add_tag_act)

        # 検索/コピーモード切替のショートカット（次セッション申し送り事項対応）。
        # 候補として最初は Z が挙がったが、実ソース確認の結果 Z は既に
        # fullscreen_act（全画面表示）で使用済みと判明したため不採用。
        # A/S/D はいずれも未使用だったが、ユーザー判断により「検索(Search)」を
        # 連想しやすい S を採用（edit_menu内のニーモニックとも非衝突）。
        self.toggle_mode_act = QAction("検索/コピーモード切替(&S)", self)
        self.toggle_mode_act.setShortcut("S")
        self.toggle_mode_act.triggered.connect(self._on_toggle_tag_panel_mode)
        edit_menu.addAction(self.toggle_mode_act)

        edit_menu.addSeparator()

        unsharp_act = QAction("アンシャープマスク(&U)", self)
        unsharp_act.triggered.connect(self.apply_unsharp)
        edit_menu.addAction(unsharp_act)

        quantize_act = QAction("減色 256色(&Q)", self)
        quantize_act.triggered.connect(self.apply_quantize)
        edit_menu.addAction(quantize_act)

        gray_act = QAction("グレースケール(&G)", self)
        gray_act.triggered.connect(self.apply_grayscale)
        edit_menu.addAction(gray_act)

        contrast_act = QAction("明るさ/コントラスト(&L)", self)
        contrast_act.triggered.connect(self.apply_brightness_contrast)
        edit_menu.addAction(contrast_act)

        # --- 表示 (V) ---
        view_menu = self.menu_bar.addMenu("表示(&V)")
        
        first_act = QAction("先頭画像(&T)", self)
        first_act.setShortcut("Home")
        first_act.triggered.connect(self.go_to_first)
        view_menu.addAction(first_act)

        prev_act = QAction("前の画像(&P)", self)
        prev_act.setShortcut("Left")
        prev_act.triggered.connect(self.go_to_previous)
        view_menu.addAction(prev_act)

        next_act = QAction("次の画像(&N)", self)
        next_act.setShortcut("Right")
        next_act.triggered.connect(self.go_to_next)
        view_menu.addAction(next_act)

        last_act = QAction("最終画像(&B)", self)
        last_act.setShortcut("End")
        last_act.triggered.connect(self.go_to_last)
        view_menu.addAction(last_act)

        view_menu.addSeparator()

        self.toggle_menu_act = QAction("メニューバー(&M)", self)
        self.toggle_menu_act.setCheckable(True)
        self.toggle_menu_act.setChecked(True)
        self.toggle_menu_act.triggered.connect(self.toggle_menubar)
        view_menu.addAction(self.toggle_menu_act)

        self.toggle_status_act = QAction("ステータスバー(&S)", self)
        self.toggle_status_act.setCheckable(True)
        self.toggle_status_act.setChecked(True)
        self.toggle_status_act.triggered.connect(self.toggle_statusbar)
        view_menu.addAction(self.toggle_status_act)

        # --- オプション (O) ---
        opt_menu = self.menu_bar.addMenu("オプション(&O)")
        
        mode_menu = opt_menu.addMenu("表示モード設定(&O)")
        modes = [
            ("そのまま(&S)", "raw"),
            ("ウインドウサイズへ伸縮(&W)", "window"),
            ("縦横固定して伸縮(&A)", "window_aspect"),
            ("幅に合わせる(&H)", "width"),
            ("大きい画像のみ縮小(&R)", "smart"),
        ]
        self.mode_group = []
        for name, key in modes:
            act = QAction(name, self)
            act.setCheckable(True)
            if key == "smart":
                act.setChecked(True)
            act.triggered.connect(lambda checked, k=key: self.set_fit_mode(k))
            mode_menu.addAction(act)
            self.mode_group.append((key, act))

        interp_menu = opt_menu.addMenu("補間法設定")
        interps = [
            ("通常最近傍法(&E)", "fast"),
            ("美麗スムーズモード(&L)", "smooth"),
        ]
        self.interp_group = []
        for name, key in interps:
            act = QAction(name, self)
            act.setCheckable(True)
            if key == "smooth":
                act.setChecked(True)
            act.triggered.connect(lambda checked, k=key: self.set_interpolation_mode(k))
            interp_menu.addAction(act)
            self.interp_group.append((key, act))

        # 指示書06 機能追加2: SDIウィンドウを閉じた時にタグパネルの
        # モード（検索/コピー）をどう扱うかを設定可能にする。既定は
        # 「閉じたら検索モードに戻す」（コピーモードのまま次の画像を
        # 開いてしまう誤操作を防ぐため）。
        close_mode_menu = opt_menu.addMenu("コピーモード終了時の挙動")
        close_mode_options = [
            ("閉じたら検索モードに戻す(&S)", "reset_to_search"),
            ("コピーモードを維持する(&K)", "keep"),
        ]
        self.close_mode_group = []
        close_mode_settings = QSettings("D-liner", "D-liner")
        saved_close_mode = close_mode_settings.value(
            "sdi/tag_panel_mode_on_close", "reset_to_search", type=str
        )
        if saved_close_mode not in ("reset_to_search", "keep"):
            # 不正値フォールバック（既存の他設定と同じ方針）
            saved_close_mode = "reset_to_search"
        for name, key in close_mode_options:
            act = QAction(name, self)
            act.setCheckable(True)
            if key == saved_close_mode:
                act.setChecked(True)
            act.triggered.connect(lambda checked, k=key: self._set_tag_panel_close_mode(k))
            close_mode_menu.addAction(act)
            self.close_mode_group.append((key, act))

        # --- ウインドウ (W) ---
        win_menu = self.menu_bar.addMenu("ウインドウ(&W)")
        
        close_act = QAction("閉じる(&C)", self)
        close_act.setShortcut("Ctrl+Q")
        close_act.triggered.connect(self.close)
        win_menu.addAction(close_act)

        close_all_act = QAction("すべて閉じる(&A)", self)
        close_all_act.triggered.connect(self.close_all_sdi)
        win_menu.addAction(close_all_act)

        win_menu.addSeparator()

        tab_next = QAction("次へ(&N)", self)
        tab_next.setShortcut("Tab")
        tab_next.triggered.connect(self.go_to_next)
        win_menu.addAction(tab_next)

        tab_prev = QAction("前へ(&P)", self)
        tab_prev.setShortcut("Shift+Tab")
        tab_prev.triggered.connect(self.go_to_previous)
        win_menu.addAction(tab_prev)

        main_win_act = QAction("メインウインドウへ(&F)", self)
        main_win_act.setShortcut("Ctrl+F")
        main_win_act.triggered.connect(self.focus_main_window)
        win_menu.addAction(main_win_act)

        win_menu.addSeparator()

        fullscreen_act = QAction("全画面表示(&Z)", self)
        fullscreen_act.setShortcut("Z")
        fullscreen_act.triggered.connect(self.toggle_fullscreen)
        win_menu.addAction(fullscreen_act)

        self.link_act = QAction("操作連動(&S)", self)
        self.link_act.setCheckable(True)
        self.link_act.triggered.connect(self.toggle_linked_mode)
        win_menu.addAction(self.link_act)

        # --- 指示書03 タスクB: モード切替 + 全タグコピー コーナーウィジェット ---
        # 重要: QToolButton は使わない。_ensure_overflow_icon_visible()（下記）
        # が menu_bar.findChildren(QToolButton) で「>>」オーバーフローボタンを
        # 探すが、setCornerWidget() で追加するコンテナは Qt の仕様上 menu_bar
        # の子になるため、QToolButton で実装すると誤って本物のオーバーフロー
        # ボタンと区別されずアイコン上書き対象に巻き込まれてしまう。
        # QPushButton であればこの関数の対象から外れ、既存の「>>」視認性
        # 修正と衝突しない。
        corner = QWidget(self)
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(4, 0, 4, 0)
        corner_layout.setSpacing(4)

        settings = QSettings("D-liner", "D-liner")
        saved_mode = settings.value("sdi/tag_panel_mode", "search", type=str)
        if saved_mode not in ("search", "copy"):
            # 不正値（QSettings破損・想定外の値等）は必ず "search" にフォール
            # バックする（指示書03「全体設計」の要求）。
            saved_mode = "search"
        self.tag_panel.set_mode(saved_mode)

        self.mode_toggle_btn = QPushButton(corner)
        self.mode_toggle_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        # 【追記・実機再確認で判明】WA_StyledBackground 追加後もホバー前は
        # 見分けがつかない状態が再現。原因を setFlat(True) 自体に切り替えて
        # 疑う: windowsvista スタイルの「フラットボタン」描画は、State_
        # MouseOver が立っていない（＝ホバーされていない）間は背景を
        # 描画しない仕様がネイティブスタイル側に組み込まれており、
        # QSS で border/background を指定していてもこの「フラット時は
        # 素通し」という前提自体は上書きしきれていなかった可能性が高い。
        # setFlat(True) をやめ、QSS側で完全に見た目を定義する通常の
        # QPushButtonとして描画させる。
        self.mode_toggle_btn.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # バグ修正（指示書06 バグ2）: テキスト付きの幅広ボタンだと、狭い
        # ウィンドウ幅で通常メニュー項目(ファイル/画像/編集...)が「>>」への
        # 退避すら起きずに消えてしまう実機不具合が確認された。コーナー
        # ウィジェットの専有幅を固定し、アイコンのみ＋ツールチップに変更する。
        self.mode_toggle_btn.setFixedWidth(30)
        self._update_mode_toggle_btn_appearance()
        self.mode_toggle_btn.clicked.connect(self._on_toggle_tag_panel_mode)
        corner_layout.addWidget(self.mode_toggle_btn)

        self.copy_all_btn = QPushButton("🏷", corner)
        self.copy_all_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.copy_all_btn.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.copy_all_btn.setFixedWidth(30)
        self.copy_all_btn.setToolTip(
            "全タグをコピー\n"
            "表示中の全タグをクリップボードへコピーします"
            "（3行に折りたたまれている分も含めて全件対象）"
        )
        # 常時視認できる中立配色（後述 _apply_corner_btn_style 参照）。
        # mode_toggle_btn と異なりモードに連動しないため active=False 固定。
        self._apply_corner_btn_style(self.copy_all_btn, active=False)
        self.copy_all_btn.clicked.connect(self._on_copy_all_tags)
        corner_layout.addWidget(self.copy_all_btn)

        self.menu_bar.setCornerWidget(corner, Qt.Corner.TopRightCorner)

    def _apply_corner_btn_style(self, btn: QPushButton, *, active: bool) -> None:
        """
        コーナーウィジェット（🔍/📋・🏷ボタン）の共通スタイル適用。

        経緯: 従来は background: transparent; border: none; の完全透過スタイル
        だったため、ホバーするまでボタンの存在自体に気付けない（実機で
        マウスオーバーして初めて枠が見える）という視認性の指摘を受けた。
        枠線・薄い背景を常時つけることで、ホバー前から「ここにボタンが
        ある」と分かる状態にする。

        【追記・実機確認で判明】一度目の対応（本メソッド新設）では
        ホバー前後で見た目に差が出ない不具合が発生した。原因は指示書06
        バグ1と同じで、setFlat(True) の QPushButton は Windows ネイティブ
        スタイル環境下では WA_StyledBackground が無いと背景/枠線のQSSが
        無視される。ボタン生成側で setAttribute(WA_StyledBackground, True)
        を追加し、あわせて非アクティブ時の不透明度も引き上げた
        （18/255→35/255、枠線70/255→90/255）。

        【追記2・実機再確認で判明】上記対応後も再現。原因を setFlat(True)
        自体に切り替えて疑い、ボタン生成側で setFlat(True) を撤去した
        （windowsvistaスタイルの「フラットボタンはホバー時のみ背景を
        描画する」という仕様自体がQSSより優先されていた可能性が高い）。
        通常（非flat）のQPushButtonとして border/background を明示指定
        することで、Qt側のボックスモデル描画に切り替わりネイティブの
        3D装飾も含めて上書きされることを期待する。

        【追記3・ユーザー判断】中間的な微調整を繰り返すより、まず「誰の目にも
        明らかに変わった」状態を作って判定する方針に変更。半透明(rgba)を
        やめ、不透明の単色背景・太め(2px)の枠線に振り切る。これで実機で
        改善が見られない場合は、色による視認性強化自体を諦め仕様として
        受け入れる（ユーザー合意済み）。
        """
        if active:
            bg = "#5a332c"
            border = "2px solid #c0776b"
            hover_bg = "#7a453c"
        else:
            bg = "#4a4a4a"
            border = "2px solid #8a8a8a"
            hover_bg = "#5e5e5e"

        btn.setStyleSheet(
            f"QPushButton {{ color: #eeeeee; background: {bg}; "
            f"border: {border}; border-radius: 4px; padding: 2px 2px; "
            f"font-size: 14px; }}"
            f"QPushButton:hover {{ background: {hover_bg}; border-radius: 4px; }}"
        )

    def _update_mode_toggle_btn_appearance(self) -> None:
        """
        モード切替ボタンのアイコン・ツールチップ・配色を現在のモードに合わせる。
        指示書06 バグ2対応でテキストを持たないアイコンのみ表示に変更。
        配色は _apply_corner_btn_style() 参照（視認性強化、次セッション対応）。
        """
        is_copy = self.tag_panel._mode == "copy"
        if is_copy:
            self.mode_toggle_btn.setText("📋")
            self.mode_toggle_btn.setToolTip("コピーモード中\nクリックで検索モードに切り替えます")
        else:
            self.mode_toggle_btn.setText("🔍")
            self.mode_toggle_btn.setToolTip("検索モード中\nクリックでコピーモードに切り替えます")
        self._apply_corner_btn_style(self.mode_toggle_btn, active=is_copy)

    def _on_toggle_tag_panel_mode(self) -> None:
        new_mode = "copy" if self.tag_panel._mode == "search" else "search"
        self.tag_panel.set_mode(new_mode)
        QSettings("D-liner", "D-liner").setValue("sdi/tag_panel_mode", new_mode)
        self._update_mode_toggle_btn_appearance()

    def _on_copy_all_tags(self) -> None:
        """
        「全タグをコピー」: 検索/コピーどちらのモードでも常時使えるボタン。
        情報源は必ず tag_panel._current_tags からとする。tag_panel._buttons
        （表示中ボタン一覧）から集めてはいけない。3行超過時は非表示チップが
        hide() されているだけで _buttons には残っており、「表示中ボタンから
        収集」する実装をすると展開前のタグが欠落する（指示書03タスクB
        【改訂・重要】）。
        """
        tags = self.tag_panel._current_tags
        if not tags:
            return
        from workers import format_tags_for_copy
        text = format_tags_for_copy(tags)
        QGuiApplication.clipboard().setText(text)

    def _make_overflow_arrow_icon(self, color: str = "#e8e8e8") -> QIcon:
        """
        QMenuBarのオーバーフロー（">>"）ボタン用に、自前で描画した
        明るい色の二重矢印アイコンを作る。Windowsのネイティブスタイル
        はこのボタンの矢印をOSのテーマ描画APIで直接描画しており、
        Qtのパレット/スタイルシートでは色を変えられないことがある
        ため、アイコンそのものを明示的に差し替えることで迂回する。
        """
        size = 16
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(color))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        # ">>" の二重山形矢印
        painter.drawLine(4, 3, 8, 8)
        painter.drawLine(8, 8, 4, 13)
        painter.drawLine(9, 3, 13, 8)
        painter.drawLine(13, 8, 9, 13)
        painter.end()
        return QIcon(pixmap)

    def _ensure_overflow_icon_visible(self) -> None:
        """
        QMenuBarのオーバーフロー用QToolButtonは、実際にオーバーフロー
        が発生した時点でQt内部が遅延生成するため、create_menu_bar()の
        時点ではまだ存在しないことが多い。resizeEvent()のたびに探索し、
        見つかったボタンにまだ自前アイコンをセットしていなければ
        セットする（"d_liner_overflow_icon_set" プロパティで二重適用を
        防止）。
        """
        if not hasattr(self, "menu_bar") or self.menu_bar is None:
            return
        for btn in self.menu_bar.findChildren(QToolButton):
            if btn.property("d_liner_overflow_icon_set"):
                continue
            btn.setIcon(self._make_overflow_arrow_icon())
            btn.setText("")
            btn.setProperty("d_liner_overflow_icon_set", True)

    def load_image(self, path: str) -> bool:
        """
        画像をロードして表示する。
        成功時 True、失敗時（ファイルなし・読み込みエラー）は False を返す。
        ロード中の重複呼び出しは無視して False を返す。
        """
        if self._is_loading:
            return False  # 前のロードが完了するまで追加リクエストを無視

        self._is_loading = True
        success = False
        try:
            self.file_path = path.replace("\\", "/")
            if not os.path.exists(self.file_path):
                self.status_bar.showMessage("ファイルが存在しません。")
                return False

            qimg = QImage(self.file_path)
            if qimg.isNull():
                self.status_bar.showMessage("画像のロードに失敗しました。")
                return False

            filename = os.path.basename(self.file_path)
            w, h = qimg.width(), qimg.height()
            depth = qimg.depth()
            size_mb = os.path.getsize(self.file_path) / (1024 * 1024)

            # ウインドウタイトルとステータス表示の更新
            self.setWindowTitle(f"{filename} {w}x{h} {depth}b {size_mb:.1f}MB ({self.current_index + 1}/{len(self.all_files)})")
            self.status_bar.showMessage(f"パス: {self.file_path} | サイズ: {w}x{h} | 深度: {depth}bit")

            # バグ修正: 従来は _auto_resize_window_if_raw() の後に
            # _update_tag_panel() を呼んでいたため、リサイズ計算が「前の
            # 画像のタグパネルの高さ（展開状態含む）」のまま行われていた。
            # タグ全展開機能の追加により、この高さの差（3行 → 数十行）が
            # 大きくなり得るようになったため、まずタグパネルを同期的に
            # クリア（0高さ・展開状態リセット）してから smart モードの
            # リサイズ計算を行う。新しい画像のタグは直後の
            # _update_tag_panel() で非同期に取得され、実際の高さが
            # 確定した時点で panel_resized 経由の再計算が改めて走る。
            self.tag_panel.clear()

            self.image_label.set_raw_image(qimg)
            self._auto_resize_window_if_raw(qimg.width(), qimg.height())
            self._schedule_deferred_scrollbar_check()
            self.selection_request.emit(self.file_path)

            # タグパネルを更新（DBからimage_idを引いてタグを非同期取得）
            self._update_tag_panel(self.file_path)

            success = True
            return True
        finally:
            self._is_loading = False

    def _update_tag_panel(self, file_path: str) -> None:
        """ファイルパスからDBのimage_idを解決してタグパネルを更新する。"""
        # DBのpathカラムはas_posix()で統一されているためスラッシュに正規化して検索
        from pathlib import Path as _Path
        norm_path = _Path(file_path).as_posix()
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM images WHERE path = ? AND status = 'ACTIVE'",
                (norm_path,),
            )
            row = cursor.fetchone()
            conn.close()
            image_id = row[0] if row else -1
        except Exception:
            image_id = -1

        if image_id >= 0:
            self.tag_panel.load_tags_for(image_id)
        else:
            self.tag_panel.clear()

        self._refresh_lock_indicator(image_id)

    # ------------------------------------------------------------------
    # AI自動タグ付け ロック/アンロック（画像右上のオーバーレイボタン）
    # ------------------------------------------------------------------

    def _reposition_lock_btn(self) -> None:
        """ロックボタンを画像表示エリア右上へ固定表示する。"""
        margin = 10
        container = self.centralWidget()
        if container is None:
            return
        x = container.width() - self.lock_btn.width() - margin
        y = margin
        self.lock_btn.move(max(0, x), y)
        self.lock_btn.raise_()

    def _refresh_lock_indicator(self, image_id: int) -> None:
        """現在表示中の画像のロック状態をDBから取得し、ボタンに反映する。"""
        self._lock_target_image_id = image_id
        if image_id < 0:
            self.lock_btn.setVisible(False)
            return
        locked = self._query_lock_state(image_id)
        self._set_lock_icon(locked)
        self.lock_btn.setVisible(True)
        self._reposition_lock_btn()

    def _query_lock_state(self, image_id: int) -> bool:
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ai_tagging_suppressed FROM images WHERE id = ?",
                (image_id,),
            )
            row = cursor.fetchone()
            conn.close()
            return bool(row[0]) if row else False
        except Exception:
            return False

    def _set_lock_icon(self, locked: bool) -> None:
        if locked:
            self.lock_btn.setText("🔒")
            self.lock_btn.setToolTip(
                "この画像はAI自動タグ付けから除外されています\nクリックで解除"
            )
            self.lock_btn.setStyleSheet(
                "QPushButton {"
                "  background-color: #d4a017; color: #3a2c00;"
                "  border: 2px solid #ffd54f; border-radius: 16px;"
                "  font-size: 14px;"
                "}"
                "QPushButton:hover { background-color: #e8b923; }"
            )
        else:
            self.lock_btn.setText("🔓")
            self.lock_btn.setToolTip(
                "クリックでこの画像をAI自動タグ付けから除外（ロック）"
            )
            self.lock_btn.setStyleSheet(
                "QPushButton {"
                "  background-color: rgba(60,60,60,150); color: #cccccc;"
                "  border: 1px solid #555555; border-radius: 16px;"
                "  font-size: 14px;"
                "}"
                "QPushButton:hover { background-color: rgba(90,90,90,180); }"
            )

    def _on_lock_btn_clicked(self) -> None:
        """
        ロック状態を明示的にトグルする。バックグラウンド自動タグ付けの
        対象から除外/復帰させるのはこのボタンからの操作のみとする
        （AI由来タグの削除や再タグ付けからは連動して変化しない、
        ロックボタン方式への一本化）。
        """
        image_id = self._lock_target_image_id
        if image_id < 0:
            return
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ai_tagging_suppressed FROM images WHERE id = ?",
                (image_id,),
            )
            row = cursor.fetchone()
            currently_locked = bool(row[0]) if row else False
            new_locked = not currently_locked
            conn.execute(
                "UPDATE images SET ai_tagging_suppressed = ? WHERE id = ?",
                (1 if new_locked else 0, image_id),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"ロック状態の変更に失敗しました: {e}")
            return

        self._set_lock_icon(new_locked)
        msg = (
            "この画像をAI自動タグ付けから除外しました（ロック）"
            if new_locked else
            "ロックを解除しました（AI自動タグ付け対象に戻ります）"
        )
        self.status_bar.showMessage(msg, 4000)

    def _auto_resize_window_if_raw(self, img_w: int, img_h: int) -> tuple[int, int] | None:
        """
        _auto_resize_window_if_raw_impl() の再入防止ラッパー。

        バグ修正: resize() -> resizeEvent() -> image_label.update_view()
        -> _auto_resize_window_if_raw() という呼び出し連鎖があり、
        ここからさらに resize() を呼ぶ構造のため、本来は同一サイズへの
        resize() で収束するはずだった。しかし、マウスホイールでの高速な
        連続送りにより短時間に大量の load_image() が呼ばれると、
        panel_resized シグナル発火（タグ取得完了タイミング）や
        smart モードの安全策（update_view 側の追加 resize）が絡み合い、
        この再帰呼び出しが積み重なって収束せず、ウィンドウが延々と
        リサイズを繰り返して画面がちらつき、入力を受け付けなくなる
        （UIスレッドが戻ってこない）不具合が発生していた。
        同一スレッド内で既にこの関数が実行中であれば、追加の呼び出しは
        即座に抜けることで、この種の再帰的リサイズの積み重なりを断つ。
        """
        if getattr(self, "_auto_resize_in_progress", False):
            return None
        self._auto_resize_in_progress = True
        try:
            return self._auto_resize_window_if_raw_impl(img_w, img_h)
        finally:
            self._auto_resize_in_progress = False

    def _auto_resize_window_if_raw_impl(self, img_w: int, img_h: int) -> tuple[int, int] | None:
        """
        fit_mode が 'smart' のとき画像解像度に合わせてウィンドウをリサイズする。
        長辺側がデスクトップの使用可能領域に収まるよう縮小する（Linar準拠）。
        画像がデスクトップより小さい場合は原寸でウィンドウをリサイズする。

        戻り値: 実際に採用した表示サイズ（論理px, display_w, display_h）。
        呼び出し側（update_view）はこれをラベル/pixmapのサイズにも使うことで、
        ウィンドウとラベルの表示サイズを一致させ、不要なスクロールバーの
        発生を防ぐ。何もしなかった場合は None。
        """
        if self.image_label.fit_mode not in ("raw", "smart"):
            return None

        screen = self.screen()
        if screen is None:
            return None
        available = screen.availableGeometry()

        # QImage.width()/height() は物理ピクセルを返す。
        # self.resize() / availableGeometry() は論理ピクセル単位なので DPR で割る。
        dpr = self.devicePixelRatio()
        if dpr <= 0:
            dpr = 1.0
        logical_img_w = img_w / dpr
        logical_img_h = img_h / dpr

        # chrome サイズ（フレーム・メニュー・ステータスバー・タグパネル）
        #
        # バグ修正: 「画面に対してどれだけ縮小すべきか」を判定する
        # content_max_w/h には OS枠（タイトルバー・ウィンドウ境界）を
        # 含めた frameGeometry() ベースの値が必要だが、この関数の最後で
        # 呼ぶ self.resize(target_w, target_h) の resize() は OS枠を
        # 含まないウィジェット自身のサイズを指定するメソッドである。
        # 同じ chrome_w/h をどちらにも使い回していたため、
        # target_w/h が実際に必要なサイズよりOS枠の分だけ大きく
        # 要求されてしまい、画面ギリギリの解像度でOS側がウィンドウを
        # クリップした際に image_label が要求するサイズより centralWidget
        # の実際の表示領域が狭くなり、不要なスクロールバーが出る原因に
        # なっていた。画面判定用（frame_chrome）とresize()用
        # （widget_chrome、OS枠を含まない self.height()/width() ベース）
        # を分けて計算する。
        # バグ修正: 以前は「self.tag_panel.isVisible()」で判定していたが、
        # _on_tags_fetched() が setVisible(True) の直後に _reflow()
        # （_update_height() 経由で panel_resized を発火）を呼ぶ際、
        # まさにその瞬間は Qt内部の表示状態確定が _reflow() 呼び出しに
        # 追いついておらず isVisible() が一時的に False を返すことが
        # 実機ログで確認された。これにより panel_resized 経由の再計算が
        # tag_panel_h=0 として計算してしまい、初回タグ付き画像表示時に
        # 必ずタグエリア分だけウィンドウが小さく確定してスクロールバーが
        # 出る、という不具合が残っていた。_update_height() はタグが無い
        # 場合に常に setFixedHeight(0) を明示的に呼んでいるため、
        # height() 自体が「今必要な高さ」を正しく表す。isVisible() の
        # 判定は不要かつ有害だったため撤去する。
        tag_panel_h = self.tag_panel.height()

        # バグ修正: 以前は widget_chrome_w/h を
        # 「self.width()/height() - self.centralWidget().width()/height()」
        # という、2つの独立したウィジェットのサイズの引き算で求めていた。
        # Qt内部のレイアウト処理タイミングにより、self（ウィンドウ全体）
        # と centralWidget が瞬間的に不整合な状態（例: 前の画像の古い
        # self.height() と、まだ更新途中の centralWidget().height() の
        # 組み合わせ）で読み取られると、この引き算が実際のchrome量と
        # かけ離れた巨大な値になることがあり、結果としてウィンドウが
        # 画面をほぼ覆うほど巨大化し、画像自体は正しいサイズのまま
        # 中央にレターボックス表示される、という不具合が実機で発生した
        # （小さいGIF等で顕著）。
        # menuBar()/statusBar() の高さ、および self 自身の
        # frameGeometry() と geometry() の差分（OS枠の厚み。同一ウィジェ
        # ットの2つのプロパティなので、他ウィジェットとの不整合が起き
        # ない）はいずれも直接測定できる安定した値のため、引き算に頼ら
        # ずこれらを直接積み上げる方式に変更した。
        menu_h = self.menuBar().height() if self.menuBar() is not None else 0
        status_h = self.statusBar().height() if self.statusBar() is not None else 0
        frame_margin_w = self.frameGeometry().width() - self.width()
        frame_margin_h = self.frameGeometry().height() - self.height()

        widget_chrome_w = 0
        widget_chrome_h = menu_h + status_h + tag_panel_h
        frame_chrome_w = frame_margin_w + widget_chrome_w
        frame_chrome_h = frame_margin_h + widget_chrome_h

        # 利用可能領域（余白10%）
        max_w = int(available.width()  * 0.90)
        max_h = int(available.height() * 0.90)
        content_max_w = max_w - frame_chrome_w
        content_max_h = max_h - frame_chrome_h

        if self.image_label.fit_mode == "smart":
            # 長辺がデスクトップを超える場合はアスペクト比維持で縮小
            if logical_img_w > content_max_w or logical_img_h > content_max_h:
                scale = min(content_max_w / logical_img_w, content_max_h / logical_img_h)
                content_w = int(logical_img_w * scale)
                content_h = int(logical_img_h * scale)
            else:
                content_w = int(logical_img_w)
                content_h = int(logical_img_h)

            # バグ修正: 32x32アイコンのような極端に小さい画像の場合、
            # display_w/hが画像の実寸のまま採用され、ウィンドウ全体が
            # メニューバーすら正しく描画できないほど小さくなり、UIが
            # 壊れて見える不具合があった（ユーザー報告のSS参照）。
            # 無理に画像サイズへウィンドウを合わせるのをやめ、最小表示
            # サイズを設ける。HD/2.5K/4K等ディスプレイ解像度により
            # 適切な絶対値が変わるため、画面の使用可能領域に対する
            # 比率（_min_display_size、下限・上限クランプ付き）で
            # 動的に算出する。実際の画像は無理に拡大せず原寸のまま
            # 中央に配置し、余白は黒で埋める（レターボックス）。
            # content_w/h（画像を実際に描画するサイズ）と
            # display_w/h（ウィンドウ内の表示エリア全体のサイズ）を
            # 分離して返し、呼び出し側（update_view）でレターボックス
            # 合成を行えるようにする。
            min_display_w, min_display_h = self._min_display_size(
                available.width(), available.height()
            )
            display_w = max(content_w, min_display_w)
            display_h = max(content_h, min_display_h)
        else:
            # rawモード: 100%表示（スクリーン内に収める、画面判定はOS枠込みで行う）
            # raw は「実ピクセルを見る」ための明示的な等倍/ズームモードのため、
            # smart のような最小表示サイズの床は設けない（現状維持）。
            content_w = min(int(logical_img_w), max_w - frame_chrome_w)
            content_h = min(int(logical_img_h), max_h - frame_chrome_h)
            display_w = content_w
            display_h = content_h

        target_w = display_w + widget_chrome_w
        target_h = display_h + widget_chrome_h

        self.resize(target_w, target_h)
        self._clamp_to_available()
        return display_w, display_h, content_w, content_h

    def _on_tag_panel_resized(self) -> None:
        """
        タグパネルの高さが（非同期のタグ取得完了・行数変化等により）
        実際に変わったタイミングで呼ばれる。load_image() 内で行った
        _auto_resize_window_if_raw() は、その時点でのタグパネルの
        高さ（前の画像のもの、または初回は非表示＝0）を前提にしていて
        既に古くなっているため、現在表示中の画像サイズを使って
        リサイズ計算をやり直す。fit_mode が raw/smart 以外のときは
        _auto_resize_window_if_raw() 側で何もせず抜けるので安全。
        """
        raw_image = self.image_label.raw_image
        if raw_image is None or raw_image.isNull():
            return
        self._auto_resize_window_if_raw(raw_image.width(), raw_image.height())
        self._schedule_deferred_scrollbar_check()

    def _schedule_deferred_scrollbar_check(self) -> None:
        """
        バグ修正: menuBar()/statusBar() の高さは、レイアウトが完全に
        確定する前に問い合わせると、実際の値（例: 21px）ではなく
        Qtの仮の初期値（例: 30px）を返すことがあり、centralWidget()の
        「レイアウト未確定時は極端に小さいデフォルト値を返す」問題と
        同種の、より軽微だが根は同じ不具合だった。数px単位のズレは
        毎回のナビゲーションで即座には気付かれなくても、連続してタグ
        パネルの表示/非表示が切り替わる操作（ホイール/キー連続送り）を
        繰り返すうちに蓄積し、最終的にスクロールバーとして顕在化する
        ことが実機ログで確認された。
        事前の予測計算だけに頼るのをやめ、イベントループが一度落ち着き
        Qt側のレイアウトが完全に確定した後に、実際に確定している
        scroll_area のサイズを見て最終確認・補正を行う。
        バグ修正: 実機（高DPIスケーリング環境）では、レイアウトの確定が
        1回のイベントループティック（0ms後）だけでは間に合わないケースが
        確認されたため、間隔を空けた複数回のチェックに強化した。
        """
        for delay_ms in (0, 30, 100, 250):
            QTimer.singleShot(delay_ms, self._verify_no_scrollbar)
        # バグ修正（推測含む・要実機確認）: resize()/move() をこの一連の
        # 処理内で短時間に何度も呼んでいるため、Windows側が把握している
        # 「このウィンドウの実際の位置」とQt側の認識がズレ、メニューバーの
        # オーバーフロー（">>"）から開くポップアップメニューが、ウィンドウ
        # とは無関係な場所に表示されてしまう不具合が報告された。他の
        # 遅延処理がすべて落ち着いた後に、現在位置へ改めて move() する
        # ことで、OS側にウィンドウ位置を明示的に再同期させる。
        QTimer.singleShot(300, self._resync_window_geometry)

    def _resync_window_geometry(self) -> None:
        self.move(self.pos())

    def _verify_no_scrollbar(self) -> None:
        if self.image_label.fit_mode != "smart":
            return
        raw_image = self.image_label.raw_image
        if raw_image is None or raw_image.isNull():
            return
        hbar = self.scroll_area.horizontalScrollBar()
        vbar = self.scroll_area.verticalScrollBar()
        if hbar.maximum() <= 0 and vbar.maximum() <= 0:
            return
        # バグ修正: scroll_area.width()/height() ではなく viewport() の
        # サイズを使う。scroll_area 自体のサイズは既に表示されている
        # スクロールバー分を差し引いていないため、それを基準に補正計算
        # すると「スクロールバーがある前提の広さ」までしか縮めきれず、
        # 縮小後に実際にはスクロールバーが不要なのに残ってしまう
        # （またはその逆）ことがあった。viewport() は実際にスクロール
        # バーが消費している分を除いた「本当に使える表示領域」を返す。
        viewport = self.scroll_area.viewport()
        avail_w = viewport.width()
        avail_h = viewport.height()
        label = self.image_label
        cur_w = label.width()
        cur_h = label.height()
        if avail_w <= 0 or avail_h <= 0 or cur_w <= 0 or cur_h <= 0:
            return
        scale = min(avail_w / cur_w, avail_h / cur_h)
        scale = max(0.01, min(1.0, scale))
        fixed_w = max(1, int(cur_w * scale))
        fixed_h = max(1, int(cur_h * scale))
        if (fixed_w, fixed_h) == (cur_w, cur_h):
            return
        trans_mode = (
            Qt.TransformationMode.SmoothTransformation
            if label.interpolation_mode == "smooth"
            else Qt.TransformationMode.FastTransformation
        )
        pm = label.pixmap()
        if pm is None or pm.isNull():
            return
        # 現在表示中のピクスマップ（レターボックス済みの可能性もある）を
        # そのまま縮小するのではなく、生画像から改めてスケーリングする
        # ことで画質劣化の重ね掛けを避ける。
        scaled = QPixmap.fromImage(label.display_image).scaled(
            fixed_w, fixed_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            trans_mode,
        )
        if scaled.width() < fixed_w or scaled.height() < fixed_h:
            canvas = QPixmap(fixed_w, fixed_h)
            canvas.fill(Qt.GlobalColor.black)
            painter = QPainter(canvas)
            x = (fixed_w - scaled.width()) // 2
            y = (fixed_h - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            painter.end()
            label.setPixmap(canvas)
        else:
            label.setPixmap(scaled)
        label.resize(fixed_w, fixed_h)

    def _clamp_to_available(self) -> None:
        """
        ウィンドウ位置を現在スクリーンの availableGeometry 内に収める。
        タスクバーが上/左/右/下どの辺にあっても安全。show()後に呼ぶこと。
        """
        screen = self.screen()
        if screen is None:
            return
        ag = screen.availableGeometry()
        fg = self.frameGeometry()
        nx = max(ag.left(), min(fg.x(), ag.right()  + 1 - fg.width()))
        ny = max(ag.top(),  min(fg.y(), ag.bottom() + 1 - fg.height()))
        if nx != fg.x() or ny != fg.y():
            self.move(nx, ny)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.image_label.update_view()
        self._ensure_overflow_icon_visible()
        self._reposition_lock_btn()

    def showEvent(self, event) -> None:
        """
        初回 show() 時のみ update_view を再実行する。
        __init__ 内の load_image() はウィンドウが非表示状態で呼ばれるため
        parentWidget().width/height が初期値(800x600)のまま計算されてしまう。
        show() 後に実サイズが確定してから再描画することで極小表示を防ぐ。
        """
        super().showEvent(event)
        if not self._first_show_done:
            self._first_show_done = True
            QTimer.singleShot(0, self.image_label.update_view)
            QTimer.singleShot(0, self._ensure_overflow_icon_visible)
            QTimer.singleShot(0, self._reposition_lock_btn)
            # バグC対策（セッション31・実機確認済み）: 非表示中に set_mode() で
            # 変更されたタグパネルのスタイルシートが、表示後の初回描画に
            # 反映されないことがある問題への回避策。
            QTimer.singleShot(0, self._force_tag_panel_restyle)

    def _force_tag_panel_restyle(self) -> None:
        """
        バグC対策（handoff30 C・セッション31で原因調査・実機確認済み）:
        「コピーモードを維持する」設定でSDIウィンドウを再オープンする際、
        create_menu_bar() が TagPanel がまだ非表示（isVisible()==False）の
        状態で set_mode() を呼ぶため、styleSheet() の値自体は正しいまま
        なのに実際の初回描画にだけ反映されない、という実機固有の症状への
        対応。show() 直後にスタイルの再計算・再描画を明示的に強制する。

        根本原因（Qt側のどの内部キャッシュが起因かまでは未特定）ではなく、
        症状に対する対応である点に注意。実機（Windows・4K・150%DPI）にて
        再現手順（コピーモード維持設定→閉じる→別画像を開く）で解消することを
        確認済み（セッション31）。
        """
        style = self.tag_panel.style()
        style.unpolish(self.tag_panel)
        style.polish(self.tag_panel)
        self.tag_panel.update()
        self.scroll_area.viewport().update()

    # --- 画像アフィン変換＆変形系 ---
    def zoom_in(self) -> None:
        self.image_label.fit_mode = "raw"
        self.scroll_area.setWidgetResizable(False)
        self.image_label.scale_factor *= 1.2
        self.image_label.update_view()

    def zoom_out(self) -> None:
        self.image_label.fit_mode = "raw"
        self.scroll_area.setWidgetResizable(False)
        self.image_label.scale_factor /= 1.2
        self.image_label.update_view()

    def zoom_reset(self) -> None:
        self.image_label.fit_mode = "raw"
        self.scroll_area.setWidgetResizable(False)
        self.image_label.scale_factor = 1.0
        self.image_label.update_view()

    def rotate_left(self) -> None:
        self.image_label.rotation_angle = (self.image_label.rotation_angle - 90) % 360
        self.image_label.apply_transforms()
        if self.linked_mode:
            self.trigger_linked_operation("rotate_left")

    def rotate_right(self) -> None:
        self.image_label.rotation_angle = (self.image_label.rotation_angle + 90) % 360
        self.image_label.apply_transforms()
        if self.linked_mode:
            self.trigger_linked_operation("rotate_right")

    def flip_vertical(self) -> None:
        self.image_label.flip_vertical = not self.image_label.flip_vertical
        self.image_label.apply_transforms()
        if self.linked_mode:
            self.trigger_linked_operation("flip_vertical")

    def flip_horizontal(self) -> None:
        self.image_label.flip_horizontal = not self.image_label.flip_horizontal
        self.image_label.apply_transforms()
        if self.linked_mode:
            self.trigger_linked_operation("flip_horizontal")

    # --- フィルタ＆加工系 ---
    def apply_grayscale(self) -> None:
        if self.image_label.raw_image:
            gray_img = self.image_label.raw_image.convertToFormat(QImage.Format.Format_Grayscale8)
            self.image_label.set_raw_image(gray_img)
            self.status_bar.showMessage("グレースケールを適用しました。")

    def apply_quantize(self) -> None:
        if self.image_label.raw_image:
            # 256色（Format_Indexed8）への高画質減色
            quant_img = self.image_label.raw_image.convertToFormat(QImage.Format.Format_Indexed8, Qt.ImageConversionFlag.OrderedDither)
            self.image_label.set_raw_image(quant_img)
            self.status_bar.showMessage("256色減色を適用しました。")

    def apply_unsharp(self) -> None:
        # 簡易アンシャープフィルター効果（輪郭強調）
        # 本実装ではアフィン・ピクセル畳み込み、Pillow等の活用も考慮
        self.status_bar.showMessage("アンシャープマスクを適用（スタブ）")

    def apply_brightness_contrast(self) -> None:
        self.status_bar.showMessage("明るさ/コントラスト調整（スタブ）")

    # --- ファイル系処理 ---
    def file_copy(self) -> None:
        from file_operation_dialog import FileOperationDialog
        dest_dir = FileOperationDialog.get_destination(
            is_move=False, current_dir=os.path.dirname(self.file_path), parent=self
        )
        if dest_dir:
            import shutil
            dest_path = os.path.join(dest_dir, os.path.basename(self.file_path))
            try:
                shutil.copy2(self.file_path, dest_path)
                self.status_bar.showMessage(f"複写完了: {dest_path}")
            except Exception as e:
                QMessageBox.critical(self, "エラー", f"複写に失敗しました:\n{e}")

    def file_move(self) -> None:
        from file_operation_dialog import FileOperationDialog
        dest_dir = FileOperationDialog.get_destination(
            is_move=True, current_dir=os.path.dirname(self.file_path), parent=self
        )
        if dest_dir:
            import shutil
            dest_path = os.path.join(dest_dir, os.path.basename(self.file_path)).replace("\\", "/")
            try:
                shutil.move(self.file_path, dest_path)
                old_path = self.file_path
                if old_path in self.all_files:
                    self.all_files.remove(old_path)
                # バグ修正: 以前はDBもサムネイルビューも一切更新しておらず、
                # メインウィンドウ側は移動があったことを知らないままだった
                # （移動先パスがACTIVEに更新されないままズレる／サムネイル
                # グリッドが即時反映されない一因）。thumbnail_grid.py の
                # _move_file と同じ更新をここでも行う。
                self._sync_db_path(old_path, dest_path)
                self._notify_main_window_refresh()
                self.go_to_next()
                self.status_bar.showMessage(f"移動完了: {dest_path}")
            except Exception as e:
                QMessageBox.critical(self, "エラー", f"移動に失敗しました:\n{e}")

    def file_delete(self) -> None:
        # バグ修正: サムネイルビュー側と見た目・既定ボタンが異なる
        # QMessageBox.question() を使っていたため、共通の
        # show_delete_confirm_dialog() に差し替える。
        confirmed = show_delete_confirm_dialog(
            self, f"「{os.path.basename(self.file_path)}」を削除しますか?\n\n{self.file_path}"
        )
        if confirmed:
            old_path = self.file_path
            try:
                # バグ修正: os.remove() による完全削除のみで、DB更新も
                # メインウィンドウへの通知も一切していなかった。そのため
                # サムネイルビュー側はこの削除を知らないまま古い一覧を
                # 保持し続け、他のSDIウィンドウや自分自身の all_files に
                # 残った古いパス参照を踏むとページ送りが止まる不具合の
                # 原因になっていた（go_to_next/previous側の耐性強化とは
                # 別に、そもそも削除自体を各所へ伝播させる）。
                # thumbnail_grid.py の _delete_file と同様 send2trash を
                # 優先し、DBは status='DELETED' に更新する。
                try:
                    import send2trash
                    # バグ修正: thumbnail_grid.py の _delete_file() と全く同じ
                    # 原因（send2trashが内部で組み立てる拡張長パスプレフィックス
                    # \\?\ は "/" → "\" の自動変換を行わないため、D-linerが
                    # "/" 区切りで保持しているパスをそのまま渡すと
                    # [Errno 2] 指定されたファイルが見つかりません、になる）
                    # がこちらの独立した実装にも存在していた。同じ修正
                    # （os.path.normpath()でOSネイティブ区切りに変換してから
                    # 渡す）をこちらにも適用する。
                    send2trash.send2trash(os.path.normpath(old_path))
                except ImportError:
                    os.remove(old_path)
                if old_path in self.all_files:
                    self.all_files.remove(old_path)
                self._sync_db_deleted(old_path)
                self._notify_main_window_refresh()
                self.go_to_next()
            except Exception as e:
                QMessageBox.critical(self, "エラー", f"削除に失敗しました:\n{e}")

    def _sync_db_deleted(self, path: str) -> None:
        """指定パスのDBレコードを status='DELETED' に更新する（存在すれば）。"""
        from pathlib import Path as _Path
        norm_path = _Path(path).as_posix()
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            conn.execute(
                "UPDATE images SET status = 'DELETED' WHERE path = ?", (norm_path,)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _sync_db_path(self, old_path: str, new_path: str) -> None:
        """移動に伴うDBのpath更新（存在すれば）。"""
        from pathlib import Path as _Path
        norm_old = _Path(old_path).as_posix()
        norm_new = _Path(new_path).as_posix()
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            conn.execute(
                "UPDATE images SET path = ?, status = 'ACTIVE' WHERE path = ?",
                (norm_new, norm_old),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _notify_main_window_refresh(self) -> None:
        """メインウィンドウのサムネイルビューへ即時反映を依頼する。"""
        mw = self._main_window
        if mw is not None and hasattr(mw, "trigger_search"):
            try:
                mw.trigger_search()
            except Exception:
                pass

    def file_save(self) -> None:
        save_path, _ = QFileDialog.getSaveFileName(self, "別名で保存", self.file_path)
        if save_path:
            if self.image_label.raw_image:
                self.image_label.raw_image.save(save_path)
                self.status_bar.showMessage(f"保存しました: {save_path}")

    def file_print(self) -> None:
        self.status_bar.showMessage("印刷ジョブを開始（スタブ）")

    # --- 壁紙・クリップボード設定 ---
    def copy_to_clipboard(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if self.image_label.raw_image:
            clipboard.setImage(self.image_label.raw_image)
            self.status_bar.showMessage("画像をクリップボードにコピーしました。")

    def set_wallpaper(self) -> None:
        # Windowsの壁紙設定API（ctypes経由）
        import ctypes
        try:
            # SPI_SETDESKWALLPAPER = 20
            # SPIF_UPDATEINIFILE = 0x01, SPIF_SENDCHANGE = 0x02
            ctypes.windll.user32.SystemParametersInfoW(20, 0, self.file_path, 3)
            self.status_bar.showMessage("デスクトップ壁紙に設定しました。")
        except Exception as e:
            self.status_bar.showMessage(f"壁紙設定エラー: {e}")

    # --- 表示順移動系 ---
    def go_to_first(self) -> None:
        if self.all_files:
            self.current_index = 0
            self.load_image(self.all_files[0])

    def go_to_last(self) -> None:
        if self.all_files:
            self.current_index = len(self.all_files) - 1
            self.load_image(self.all_files[-1])

    def navigate_to(self, path: str) -> None:
        """単一SDIモードで既存ウィンドウに別画像を表示する"""
        norm = path.replace("\\", "/")
        if norm in self.all_files:
            self.current_index = self.all_files.index(norm)
        else:
            self.all_files.append(norm)
            self.current_index = len(self.all_files) - 1
        self.load_image(norm)
        self.selection_request.emit(norm)

    def go_to_next(self) -> None:
        if not self.all_files or self._is_loading:
            return
        # バグ修正: 以前は次のファイルが存在しない場合、current_indexを
        # 元に戻して即returnしていた。サムネイルビュー側は他経路（SDI
        # ウィンドウからの削除等）での削除を即座に反映しないため、
        # all_files に古い（既に削除済みの）パスが残ったままになりやすく、
        # 一度欠番を踏むと以降 next を押すたびに同じ欠番へ戻ってロード
        # 失敗を繰り返すだけで、それより先へ進めなくなっていた。
        # 存在しないパスは all_files から取り除きつつ、次の有効な
        # ファイルまで自動的にスキップする。
        idx = self.current_index
        while True:
            if idx >= len(self.all_files) - 1:
                return  # これ以上、有効な次ファイルがない
            idx += 1
            candidate = self.all_files[idx]
            if not os.path.exists(candidate):
                del self.all_files[idx]
                idx -= 1  # 削除で1つ詰まった分を戻し、同じ位置を再チェック
                continue
            if self.load_image(candidate):
                self.current_index = idx
                if self.linked_mode:
                    self.trigger_linked_operation("next")
                return
            return  # 存在はするがロード自体に失敗 → これ以上は進めない

    def go_to_previous(self) -> None:
        if not self.all_files or self._is_loading:
            return
        # バグ修正: go_to_next と同様、存在しないファイルを自動スキップする
        idx = self.current_index
        while True:
            if idx <= 0:
                return  # これ以上、有効な前ファイルがない
            idx -= 1
            candidate = self.all_files[idx]
            if not os.path.exists(candidate):
                del self.all_files[idx]
                continue  # 後続要素は既に1つ前へ詰まっているのでidxは据え置き
            if self.load_image(candidate):
                self.current_index = idx
                if self.linked_mode:
                    self.trigger_linked_operation("prev")
                return
            return  # 存在はするがロード自体に失敗 → これ以上は進めない

    # --- 各種表示＆オプション設定 ---
    _VALID_FIT_MODES = ("smart", "raw", "window", "window_aspect", "width")

    def set_fit_mode(self, mode: str) -> None:
        # 不正な値（QSettingsの破損等）をガード
        if mode not in self._VALID_FIT_MODES:
            mode = "smart"
        self.image_label.fit_mode = mode
        QSettings("D-liner", "D-liner").setValue("sdi/fit_mode", mode)
        for k, act in self.mode_group:
            act.setChecked(k == mode)

        # smart / raw: ウィンドウを画像サイズに合わせる → ラベルもそのサイズで固定
        # window / window_aspect / width: ウィンドウサイズに合わせて画像を縮小 → ビューポートに委ねる
        self.scroll_area.setWidgetResizable(mode not in ("raw", "smart"))

        self.image_label.update_view()
        # そのままモードに切り替えたとき画像サイズに合わせてウィンドウをリサイズ
        if mode == "raw" and self.image_label.raw_image:
            img = self.image_label.raw_image
            self._auto_resize_window_if_raw(img.width(), img.height())

    def set_interpolation_mode(self, mode: str) -> None:
        self.image_label.interpolation_mode = mode
        for k, act in self.interp_group:
            act.setChecked(k == mode)
        self.image_label.update_view()

    def _set_tag_panel_close_mode(self, key: str) -> None:
        """
        指示書06 機能追加2: 「コピーモード終了時の挙動」設定の保存・
        チェック状態更新。set_fit_mode()と同じパターン。
        """
        if key not in ("reset_to_search", "keep"):
            key = "reset_to_search"
        QSettings("D-liner", "D-liner").setValue("sdi/tag_panel_mode_on_close", key)
        for k, act in self.close_mode_group:
            act.setChecked(k == key)

    def toggle_menubar(self, checked: bool) -> None:
        self.menu_bar.setVisible(checked)

    def toggle_statusbar(self, checked: bool) -> None:
        self.status_bar.setVisible(checked)

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.menu_bar.show()
            self.toggle_menu_act.setChecked(True)
        else:
            self.showFullScreen()
            self.menu_bar.hide()
            self.toggle_menu_act.setChecked(False)

    def toggle_linked_mode(self, checked: bool) -> None:
        self.linked_mode = checked
        self.status_bar.showMessage(f"操作連動: {'ON' if checked else 'OFF'}")

    def trigger_linked_operation(self, op_type: str) -> None:
        """他の起動中SDIWindowに対して操作をブロードキャストする"""
        main_win = self._main_window
        if main_win and hasattr(main_win, "sdi_windows"):
            for sdi in main_win.sdi_windows:
                if sdi != self and sdi.linked_mode:
                    if op_type == "next":
                        sdi.go_to_next()
                    elif op_type == "prev":
                        sdi.go_to_previous()
                    elif op_type == "rotate_left":
                        sdi.rotate_left()
                    elif op_type == "rotate_right":
                        sdi.rotate_right()
                    elif op_type == "flip_horizontal":
                        sdi.flip_horizontal()
                    elif op_type == "flip_vertical":
                        sdi.flip_vertical()

    def focus_main_window(self) -> None:
        if self._main_window:
            self._main_window.raise_()
            self._main_window.activateWindow()

    def close_all_sdi(self) -> None:
        if self._main_window and hasattr(self._main_window, "close_all_sdi_windows"):
            self._main_window.close_all_sdi_windows()

    def closeEvent(self, event) -> None:
        # 指示書06 機能追加2: 設定が「閉じたら検索モードに戻す」（既定）の
        # 場合、永続化キーの値を"search"へ書き換える。このウィンドウの
        # self.tag_panel インスタンス自体はこの後すぐ破棄されるため触らず、
        # 次に新しく開くSDIウィンドウのcreate_menu_bar()がこの値を
        # 読み込むことで反映される。
        settings = QSettings("D-liner", "D-liner")
        close_mode = settings.value("sdi/tag_panel_mode_on_close", "reset_to_search", type=str)
        if close_mode not in ("reset_to_search", "keep"):
            close_mode = "reset_to_search"
        if close_mode == "reset_to_search":
            settings.setValue("sdi/tag_panel_mode", "search")

        # 実機回帰チェックリストで発覚: 「そのまま」「大きい画像のみ縮小」
        # 以外のモード（window/window_aspect/width）は、画像サイズに
        # 自動追従しないユーザー任意のウィンドウサイズであるため、次回
        # SDIウィンドウを開く際にも引き継げるよう記憶しておく。
        # raw/smart は _auto_resize_window_if_raw() が画像サイズに合わせて
        # 毎回上書きするため、ここでは対象外（記憶しても意味がないため）。
        if self.image_label.fit_mode in ("window", "window_aspect", "width"):
            settings.setValue("sdi/window_size_w", self.width())
            settings.setValue("sdi/window_size_h", self.height())

        self.closed.emit(self.file_path)
        super().closeEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Space or key == Qt.Key.Key_Right or key == Qt.Key.Key_PageDown:
            self.go_to_next()
        elif key == Qt.Key.Key_Backspace or key == Qt.Key.Key_Left or key == Qt.Key.Key_PageUp:
            self.go_to_previous()
        elif key == Qt.Key.Key_Escape:
            if self.isFullScreen():
                self.toggle_fullscreen()
            else:
                self.close()
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        """マウスホイールで前後の画像に移動（Linar本家準拠）"""
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        if delta > 0:
            self.go_to_previous()
        else:
            self.go_to_next()

    def mousePressEvent(self, event) -> None:
        """左クリック→前へ、右クリック→次へ、ホイールクリック→閉じる（Linar本家準拠）"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.go_to_previous()
        elif event.button() == Qt.MouseButton.RightButton:
            self.go_to_next()
        elif event.button() == Qt.MouseButton.MiddleButton:
            self.close()
        else:
            super().mousePressEvent(event)