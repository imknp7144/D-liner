from __future__ import annotations

import sys
import os
from pathlib import Path
from PyQt6.QtCore import Qt, QSettings, QTimer, QThread, QObject, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QSplitter,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QLineEdit,
    QComboBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QStatusBar,
    QToolBar,
    QMessageBox,
    QDialogButtonBox,
    QDialog,
    QPlainTextEdit,
    QProgressBar,
    QSizePolicy,
    QRadioButton,
)
from PyQt6.QtGui import QIcon, QAction, QKeySequence, QShortcut

# 自作モジュール群
import lifecycle_manager
from workers import SearchWorker, LifecycleSyncWorker, FilesystemSearchWorker
from folder_tree import FolderTreeWidget
from thumbnail_grid import ThumbnailGridWidget
from lora_export_dialog import LoraExportDialog


def _resource_path(*parts: str) -> str:
    """
    アセット（アイコン等）のパス解決。
    main_window.py と同階層に直接置く運用に合わせ、開発時はこのファイルと
    同階層を、PyInstaller等で.exe化した場合は展開先一時フォルダ
    (sys._MEIPASS)直下を見る。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)
from sdi_window_viewer import SDIWindow


# --- 一般タグ閾値の低下警告（実機回帰チェックリストで発覚・v0.8で共通化） ---
# 一般タグ閾値を極端に低く設定すると、1枚の画像に数百件規模のタグが
# 付与されることがあり、SDIウィンドウでその画像を開いた際にタグパネルの
# 折り返し処理（TagPanel._reflow()）が一時的にフリーズする不具合が実機で
# 確認された（放置すれば解放されるため実害は軽微だが、原因に気づきにくい
# ため注意喚起のみ行う。処理自体を止めるものではなく、閾値の変更を強制
# しない）。
#
# バグ修正（v0.8）: 以前は _on_tagger_settings() 内にローカル変数として
# 埋め込まれており、比較も `spin_gen.value() <= LOW_THRESHOLD_WARNING_LIMIT`
# という浮動小数点の直接比較だった。QDoubleSpinBox（singleStep=0.05）の
# スピナーで 0.30 → 0.15 まで下げる操作を行うと、表示上は「0.15」でも
# 内部値が浮動小数点誤差で 0.15000000000000002 になり、この比較が
# False になって警告が出ないことが実機検証で判明した（キーボードで直接
# "0.15" と入力した場合は誤差が乗らないため再現しなかった）。
# 表示桁数（decimals=2）に合わせて round(value, 2) してから比較すること
# で誤差を吸収する。
#
# また、以前は _on_tagger_settings()（タグ付け設定ダイアログ）にのみ
# この警告が実装されており、_prompt_retag_settings_dialog()（「別設定で
# タグ付けし直す」ダイアログ）には実装されていなかった（ハンドオフ資料
# には「両方に追加した」と記載されていたが、実ソースには反映されて
# いなかった）。今回、共通ヘルパーとして1箇所に切り出し、両方の呼び出し
# 元から使うことで、今後同種の「横展開漏れ」が起きないようにする。
LOW_THRESHOLD_WARNING_LIMIT = 0.15


def _warn_if_low_general_threshold(parent: QWidget, value: float) -> None:
    """
    一般タグ閾値が LOW_THRESHOLD_WARNING_LIMIT 以下の場合に警告ダイアログを
    表示する。表示は行うが処理はブロックしない（呼び出し側の後続処理は
    そのまま続行してよい）。
    """
    if round(value, 2) <= LOW_THRESHOLD_WARNING_LIMIT:
        QMessageBox.warning(
            parent,
            "一般タグ閾値が低く設定されています",
            f"一般タグ閾値が {value:.2f} に設定されています"
            f"（{LOW_THRESHOLD_WARNING_LIMIT:.2f}以下）。\n\n"
            "閾値を極端に低くすると、1枚の画像に非常に多くのタグが\n"
            "付与されることがあります。タグ数が数百件規模になると、\n"
            "SDIウィンドウでその画像を開いた際に一時的にフリーズした\n"
            "状態になることが確認されています\n"
            "（強制終了ではなく、しばらく待てば解放されます）。\n\n"
            "気になる場合は閾値を少し上げることをご検討ください。"
        )

class MetaUpdateWorker(QThread):
    """
    width=0/height=0の画像にPillowでメタデータを一括補完するWorker。
    scan --with-meta相当の処理をGUIから実行する。
    """
    finished = pyqtSignal(int)   # 更新件数
    progress = pyqtSignal(str)   # 進捗メッセージ
    error    = pyqtSignal(str)

    def run(self) -> None:
        # バグ修正: cursor.execute()/executemany()が例外を投げると
        # conn.close()に到達せずリークしていた（conn=None+finally方式に統一）。
        # ※ループ内の画像単位except:passは意図的設計として維持（1件の
        #   読み込み失敗で全体の補完処理を止めないため）。
        conn = None
        try:
            from PIL import Image as PilImage
            conn = lifecycle_manager.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, path FROM images
                WHERE status = 'ACTIVE' AND (width = 0 OR height = 0)
            """)
            targets = cursor.fetchall()
            total = len(targets)
            self.progress.emit(f"メタデータ補完中... 対象 {total} 件")

            updates = []
            for i, (img_id, img_path) in enumerate(targets):
                if not os.path.exists(img_path):
                    continue
                try:
                    with PilImage.open(img_path) as im:
                        w, h = im.size
                    filesize = os.path.getsize(img_path)
                    updates.append((w, h, filesize, img_id))
                except Exception:
                    pass
                if i % 50 == 0:
                    self.progress.emit(f"メタデータ補完中... {i}/{total}")

            if updates:
                cursor.executemany("""
                    UPDATE images SET width=?, height=?, filesize=? WHERE id=?
                """, updates)
                conn.commit()
            self.finished.emit(len(updates))
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


# instruction_tagger_idle_release_bug.md 修正3: アイドル解放タイマーの
# schedule/cancel を可視化するデバッグログのトグル（env var方式は
# D_LINER_THUMB_CACHE_DEBUG と同じ流儀）。
_TAGGER_IDLE_DEBUG = os.environ.get("D_LINER_TAGGER_IDLE_DEBUG", "0") == "1"


# ステータスバーの稼働状況アイコン（サムネ／タグ付け共通）。
# セッション17: 完了メッセージを数秒表示して消す方式から、常時表示の
# 3状態アイコンに変更した（稼働状況が常に一目でわかるようにするため）。
_ICON_ACTIVE     = "🟢"   # ②③: 今開いているフォルダに対する処理が進行中
_ICON_BACKGROUND = "🌙"   # ④: アイドル時の他フォルダ処理が進行中
_ICON_STANDBY    = "⚪"   # 何も動いていない（一時停止中を含む）


class _BulkTagDialog(QDialog):
    """
    複数画像へのタグ一括追加/削除ダイアログ（LoRAトリガーワード等の
    用途を想定）。タグ名は複数可、スペース区切りで受け付ける。
    """

    def __init__(self, target_count: int, target_label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("タグの一括追加/削除")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"対象: {target_label}（{target_count} 件）"))

        layout.addWidget(QLabel("タグ名（複数はスペース区切り）:"))
        self.tag_input = QLineEdit(self)
        self.tag_input.setPlaceholderText("例: my_lora_trigger another_tag")
        layout.addWidget(self.tag_input)

        mode_row = QHBoxLayout()
        self.add_radio = QRadioButton("追加（manualカテゴリとして）", self)
        self.add_radio.setChecked(True)
        self.delete_radio = QRadioButton("削除", self)
        mode_row.addWidget(self.add_radio)
        mode_row.addWidget(self.delete_radio)
        layout.addLayout(mode_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.tag_input.setFocus()

    def _on_accept(self) -> None:
        if not self.tags():
            QMessageBox.warning(self, "エラー", "タグ名を入力してください。")
            return
        self.accept()

    def tags(self) -> list[str]:
        return [t for t in self.tag_input.text().strip().split() if t]

    def mode(self) -> str:
        return "add" if self.add_radio.isChecked() else "delete"


class MainWindow(QMainWindow):
    """
    Linar風デザインを踏襲した2ペイン + メタ詳細リスト + サムネイルグリッド、プレビュー、複合検索
    """
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("D-liner v0.8")
        icon_path = _resource_path("d_liner_icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.resize(1200, 800)

        self.current_folder_path: str = ""
        self.search_results: list[tuple] = [] # (id, path, width, height, filesize)
        self.active_search_worker: SearchWorker | FilesystemSearchWorker | None = None
        self.folder_is_registered: bool = False  # 現在フォルダがDB登録済みか
        self.sdi_windows: list[SDIWindow] = []
        self.single_sdi_mode: bool = True  # True: SDIウィンドウは常に1枚（既存を再利用）

        # タガーエンジン（起動後に非同期で初期化）
        self._tagger_engine = None
        self._tagger_worker = None
        self._tag_progress_dlg = None
        self._bulk_tag_worker = None
        self._bulk_tag_progress_dlg = None
        self._lora_export_worker = None
        self._lora_export_progress_dlg = None

        # バックグラウンドタガー
        self._bg_tagger_worker = None   # BackgroundTaggerWorker（優先度1: 現在フォルダ）

        # セッション23再設計: 個別に散在していた起動判断（_bg_paused,
        # _bg_restart_pending 等）を撤去し、_reschedule() への一本化に伴い
        # 「再チェックすべきか」を表す2つのdirtyフラグに置き換えた。
        # _p1_dirty: 優先度1（現在フォルダのタグ付け）を再試行すべきか
        # _p2_dirty: 優先度2（他フォルダのタグ付け）を再試行すべきか
        # 起動直後はTrueにしておき、初回の_reschedule()呼び出しで自然に
        # 試行される。ワーカーが「本当に何もなかった」(queue_empty)場合に
        # Falseへ、「新しい作業が発生したかもしれない」契機
        # （フォルダ切替・DB新規登録・設定変更・起動時同期/F5完了・
        # 手動タグ付け開始/完了・タガー接続完了）で再びTrueへ戻す
        # （_mark_pipeline_dirty() 参照）。中断により未処理分を残して
        # 終了した場合も、queue_emptyと区別してTrueに戻す
        # （_on_bg_finished/_on_idle_tagger_finished 参照）。
        self._p1_dirty: bool = True
        self._p2_dirty: bool = True
        # バグ修正: 優先度3(サムネイル先読み)には元々このような
        # 「確認済みなので再試行不要」の記憶が無く、_on_bg_thumb_finished/
        # _queue_empty/_interrupted が呼ぶ _reschedule() が、他に優先度1・2の
        # 仕事が無い限り毎回即座に優先度3を再起動する無限ループになって
        # いた（同一フォルダに対しI/O負荷が高いまま完了/開始を繰り返す
        # 不具合として実機で発覚）。_p3_done_for に「このフォルダは既に
        # 確認済み（対象なしor完了）」を記録し、_reschedule() 側で
        # 一致する間は優先度3の再起動を抑止する。フォルダ切替・新規登録・
        # 設定変更等の「新しい作業が発生したかもしれない」契機
        # （_mark_pipeline_dirty()）でNoneに戻す。
        self._p3_done_for: str | None = None

        # セッション10: タガーエンジン アイドル解放タイマー（イベント駆動）
        # タグ付けキュー（手動・BG問わず）が空になった瞬間に単発60秒
        # タイマーを仕掛け、release_idle_sessions() を呼んでモデルセッション
        # （NPU/GPU/CPU 推論用メモリ）を解放する。新しいタグ付けが始まったら
        # 即座にキャンセルする。tagger_engine.py 側の IDLE_TIMEOUT_SECONDS
        # ポーリングは、このイベント駆動方式が働かなかった場合の保険。
        self._tagger_idle_timer = None  # QTimer（単発）

        # バックグラウンドサムネイル生成
        self._bg_thumb_worker = None    # BackgroundThumbWorker（優先度③: 現在フォルダの見えない範囲）

        # --- v2再設計（サムネイル・タグ付けトリガー再設計）追加分 ---
        # フォルダ選択のデバウンスは、以下の2本に分離している
        # （高速化検討・akaakaさんとの熟議により、表示用のみ短縮）。
        #
        # ① 表示用デバウンス（300ms）: サムネイル表示・SDIウィンドウの
        #    整理のみを担当。これは体感速度に直結するため、②③起動判断
        #    用より短くしてある。
        # ② 背景ワーカー起動判断用デバウンス（3000ms。元は900msだったが、
        #    実機確認の上で本採用値として3000msに変更）:
        #    _mark_pipeline_dirty() の呼び出しのみを担当。クリック連打・
        #    矢印キー連続移動で毎回②③(バックグラウンドタグ付け等)を
        #    起動・中断しないための安全マージン。
        #    ※900ms→3000msへの延長理由: フォルダ切替直後に優先度2
        #    (他フォルダのタグ付け)が再始動しCPU/GPU等を奪うことで、
        #    新しく開いたフォルダのサムネイル生成ポップインが悪化する
        #    現象を実機（Windows・4K・150%DPI）で確認・akaakaさんと
        #    検証済み。3000msはあえて差が分かりやすいよう大きめに設定
        #    した診断値だったが、実機確認の結果このまま本採用と決定
        #    （高速化検討セッション、akaakaさん承認済み）。
        #
        # 両者は on_folder_selected() で常に同時に再始動されるため、
        # ①（300ms）は②（3000ms）より必ず先に確定する。②のハンドラは
        # 「そのフォルダが確定済みかどうか」を問わず _mark_pipeline_dirty()
        # を呼ぶだけの設計（同関数は他の複数箇所からも同様に呼ばれる
        # 冪等な「再チェック依頼」であり、フォルダの再選択(ノーオペ)で
        # 余分に呼ばれても実害はないと判断。詳細は各メソッドのdocstring
        # 参照）。
        self._folder_select_timer = QTimer(self)
        self._folder_select_timer.setSingleShot(True)
        self._folder_select_timer.timeout.connect(self._on_folder_select_debounced)

        self._folder_bg_timer = QTimer(self)
        self._folder_bg_timer.setSingleShot(True)
        self._folder_bg_timer.timeout.connect(self._on_folder_bg_debounced)

        self._pending_folder_path: str = ""

        # セッション23再設計: 従来はフォルダ切替のたびに世代カウンタを
        # インクリメントし、②完了時に「発行時と世代が変わっていないか」で
        # ③起動要否を判定していたが、この比較には非同期の間隙
        # （on_folder_selected()の即時中断要求 vs 900msデバウンス後の
        # 世代インクリメントとの間隙で旧世代のまま完了通知が届く）による
        # タイミングずれのリスクがあった（d_liner_handoff23.md参照）。
        # _reschedule()への一本化に伴い、起動要否は常にその場で
        # self.current_folder_path と直接比較する方式に統一したため、
        # 世代カウンタ自体が不要になり撤去した。
        self._current_folder_recursive: bool = True  # 現在フォルダのrecursive設定

        # ④ 他フォルダのバックグラウンドタグ付け（アイドル時のみ・優先度2）
        self._idle_tagger_worker = None   # BackgroundTaggerWorker(scope='other')
        # セッション23再設計: 起動判断は _reschedule() に一本化された。
        # このタイマーは「イベントを取りこぼした場合の保険」として、
        # 十分に長い間隔で _mark_pipeline_dirty() を呼び全パイプラインの
        # 再評価を強制する（個別に④だけを起動判定していた旧
        # _check_idle_and_start_other_tagging() は撤去）。
        self._idle_check_timer = QTimer(self)
        self._idle_check_timer.timeout.connect(self._mark_pipeline_dirty)
        self._idle_check_timer.start(300_000)  # 5分間隔（保険のみ。主経路はイベント駆動）

        # セッション17: 「⑤ クイックアクセス登録フォルダのバックグラウンド
        # サムネイル先読み」を一度実装したが、設計方針の見直しにより撤去。
        # 方針: タグ付け(②④)はDB登録フォルダのみを対象に構築する。
        # サムネイル(③)はDB登録orクイックアクセス登録フォルダを対象に
        # 構築するが、現時点ではアイドル時の先回りウォームアップまでは
        # 不要と判断（実際に開いたときにその場で③が生成すれば足りる）。
        # 将来ウォームアップを再検討する場合は上記スコープ定義を踏襲する。

        # サムネイルキャッシュ
        self._thumb_cache = None

        self.init_db()
        self.init_ui()
        self.load_settings()

        # サムネイルキャッシュを初期化してグリッドに注入
        self._init_thumb_cache()

        # 起動時差分スキャン（非同期）をキック
        self.trigger_startup_lifecycle_check()

        # タガーエンジンをバックグラウンドで初期化（UI表示後に実行）
        QTimer.singleShot(800, self._init_tagger_engine)

    def init_db(self) -> None:
        conn = lifecycle_manager.get_connection()
        lifecycle_manager.ensure_schema(conn)
        conn.close()

    def init_ui(self) -> None:
        # --- ステータスバー ---
        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("準備完了")

        # バックグラウンド進捗枠（右端固定、通常は非表示）
        # 1行に「サムネ進捗」と「タグ進捗」を並べる。
        # 狭い場合はタイトルバー表示に切り替えることを検討。
        self._bg_status_widget = QWidget(self.status_bar)
        _bg_outer = QHBoxLayout(self._bg_status_widget)
        _bg_outer.setContentsMargins(6, 0, 4, 0)
        _bg_outer.setSpacing(6)

        _label_style = "font-size: 11px; color: #c8c8c8;"
        _bar_style = """
            QProgressBar {
                border: 1px solid #555555;
                border-radius: 3px;
                background: #2a2a2a;
            }
            QProgressBar::chunk { background: %s; border-radius: 2px; }
        """

        # ── サムネイル状態 ──
        self._bg_thumb_label = QLabel(f"{_ICON_STANDBY} サムネ 待機中", self._bg_status_widget)
        self._bg_thumb_label.setStyleSheet(_label_style)
        self._bg_thumb_bar = QProgressBar(self._bg_status_widget)
        self._bg_thumb_bar.setFixedWidth(80)
        self._bg_thumb_bar.setFixedHeight(10)
        self._bg_thumb_bar.setTextVisible(False)
        self._bg_thumb_bar.setStyleSheet(_bar_style % "#7ab8d4")
        self._bg_thumb_bar.setVisible(False)  # 待機中はバー非表示（アイコン+テキストのみ）

        # ── セパレータ ──
        _sep = QLabel("│", self._bg_status_widget)
        _sep.setStyleSheet("color: #555555; font-size: 11px;")

        # ── タグ付け状態 ──
        self._bg_tag_label = QLabel(f"{_ICON_STANDBY} タグ 待機中", self._bg_status_widget)
        self._bg_tag_label.setStyleSheet(_label_style)
        self._bg_tag_bar = QProgressBar(self._bg_status_widget)
        self._bg_tag_bar.setFixedWidth(80)
        self._bg_tag_bar.setFixedHeight(10)
        self._bg_tag_bar.setTextVisible(False)
        self._bg_tag_bar.setStyleSheet(_bar_style % "#7aad6e")
        self._bg_tag_bar.setVisible(False)

        _bg_outer.addWidget(self._bg_thumb_label)
        _bg_outer.addWidget(self._bg_thumb_bar)
        _bg_outer.addWidget(_sep)
        _bg_outer.addWidget(self._bg_tag_label)
        _bg_outer.addWidget(self._bg_tag_bar)

        # セッション17: 稼働状況を常時アイコンで可視化する方針に変更した
        # ため、以前のような「処理中のみ表示・完了後は数秒で消す」形の
        # setVisible(False)による初期非表示は廃止し、常時表示にする。
        self.status_bar.addPermanentWidget(self._bg_status_widget)

        # --- メニューバー ---
        menubar = self.menuBar()

        # ファイルメニュー
        file_menu = menubar.addMenu("ファイル(&F)")
        action_rename = QAction("一括リネーム(&R)...", self)
        action_rename.triggered.connect(self._batch_rename_dialog)
        file_menu.addAction(action_rename)
        file_menu.addSeparator()
        action_exit = QAction("終了(&X)", self)
        action_exit.triggered.connect(self.close)
        file_menu.addAction(action_exit)

        # 表示メニュー
        view_menu = menubar.addMenu("表示(&V)")
        action_refresh_menu = QAction("更新(&F5)", self)
        action_refresh_menu.setShortcut("F5")
        action_refresh_menu.triggered.connect(self.trigger_full_refresh)
        view_menu.addAction(action_refresh_menu)

        view_menu.addSeparator()
        # linar/XnView式の「サブフォルダのファイルも表示対象」トグル。
        # 登録フォルダのrecursive設定（DBスキャン範囲）とは独立した、
        # 表示専用のON/OFF。デフォルトOFF（直下のみ表示）。
        self.action_show_subfolder_files = QAction("サブフォルダのファイルも表示対象(&U)", self)
        self.action_show_subfolder_files.setCheckable(True)
        self.action_show_subfolder_files.setChecked(False)
        self.action_show_subfolder_files.toggled.connect(self._on_show_subfolder_files_toggled)
        view_menu.addAction(self.action_show_subfolder_files)

        # タグメニュー
        tag_menu = menubar.addMenu("タグ(&T)")
        self.action_tag_one = QAction("選択中1枚をタグ付け(&1)", self)
        self.action_tag_one.triggered.connect(self._on_tag_selected)
        self.action_tag_one.setEnabled(False)   # エンジン接続後に有効化
        tag_menu.addAction(self.action_tag_one)
        self.action_tag_folder = QAction("フォルダ全件タグ付け(&A)", self)
        self.action_tag_folder.triggered.connect(self._on_tag_folder)
        self.action_tag_folder.setEnabled(False)
        tag_menu.addAction(self.action_tag_folder)
        tag_menu.addSeparator()
        action_tagger_reconnect = QAction("タガーエンジンに再接続(&R)", self)
        action_tagger_reconnect.triggered.connect(self._init_tagger_engine)
        tag_menu.addAction(action_tagger_reconnect)
        tag_menu.addSeparator()
        action_tagger_settings = QAction("タグ付け設定(&S)...", self)
        action_tagger_settings.triggered.connect(self._on_tagger_settings)
        tag_menu.addAction(action_tagger_settings)
        tag_menu.addSeparator()
        action_bulk_tag = QAction("選択中の画像にタグを一括追加/削除(&B)...", self)
        action_bulk_tag.triggered.connect(self._on_bulk_tag_edit)
        tag_menu.addAction(action_bulk_tag)
        # 実機確認（A項目）フィードバック対応: 自動タグの結果に満足できない
        # 場合に、選択中の画像（未選択なら絞り込み結果全体）を別の閾値/
        # モデルで一回限りタグ付けし直す。個別設定の保存は行わない
        # （ユーザー判断: 保持不要、自動タグ付け済みか否かだけを見ればよい）。
        # tag_menu内で &R は既に「タガーエンジンに再接続」で使用済みのため
        # 「やり直す」から &Y を採用。
        action_retag_settings = QAction("選択中の画像を別設定でタグ付けし直す(&Y)...", self)
        action_retag_settings.triggered.connect(self._on_retag_with_settings)
        tag_menu.addAction(action_retag_settings)

        # LoRA作成支援機構（セッション27）: 選択中の画像（未選択なら絞り込み
        # 結果全体）を新規フォルダへコピー＋同名.txtキャプションとして
        # エクスポートする。DB上のタグ・元画像は一切変更しない非破壊的操作。
        # 出力先フォルダはwatched_foldersに登録しないため、既存の
        # 「未登録フォルダは自動タグ付け対象外」の原則により再タグ付けの
        # 心配なくLoRA向けの整形ができる。
        action_lora_export = QAction("LoRA用にエクスポート(&E)...", self)
        action_lora_export.triggered.connect(self._on_lora_export)
        tag_menu.addAction(action_lora_export)

        # ツールメニュー（将来: キャッシュメンテナンス等）
        tools_menu = menubar.addMenu("ツール(&L)")
        self.action_cache_maintenance = QAction("キャッシュメンテナンス(&C)...", self)
        self.action_cache_maintenance.triggered.connect(self._on_cache_maintenance)
        tools_menu.addAction(self.action_cache_maintenance)

        # ヘルプメニュー
        help_menu = menubar.addMenu("ヘルプ(&H)")
        action_about = QAction("D-liner について(&A)", self)
        action_about.triggered.connect(lambda: QMessageBox.information(
            self, "D-liner について",
            "D-liner v0.8\n\nDanbooru タグ × ファイル名ハイブリッド検索対応\nAI生成画像ビューア"
        ))
        help_menu.addAction(action_about)

        help_menu.addSeparator()
        action_debug = QAction("デバッグウィンドウ(&D)", self)
        action_debug.setShortcut("Ctrl+Shift+D")
        action_debug.triggered.connect(self._show_debug_window)
        help_menu.addAction(action_debug)

        # --- ツールバー / 検索条件パネル ---
        toolbar_widget = QWidget(self)
        toolbar_layout = QHBoxLayout(toolbar_widget)
        toolbar_layout.setContentsMargins(6, 6, 6, 6)
        toolbar_layout.setSpacing(10)

        # 1. 複合タグ検索バー
        toolbar_layout.addWidget(QLabel("タグ検索:", self))
        self.search_input = QLineEdit(self)
        self.search_input.setPlaceholderText("スペース区切りでAND検索 (例: girl blue_hair)")
        self.search_input.textChanged.connect(self.trigger_search_debounce)
        toolbar_layout.addWidget(self.search_input)

        # クリアボタン（×）
        self.btn_search_clear = QPushButton("✕", self)
        self.btn_search_clear.setFixedWidth(28)
        self.btn_search_clear.setToolTip("検索をクリア")
        self.btn_search_clear.setStyleSheet(
            "QPushButton { border: none; color: palette(mid); font-size: 13px; }"
            "QPushButton:hover { color: palette(text); }"
        )
        self.btn_search_clear.clicked.connect(self.search_input.clear)
        toolbar_layout.addWidget(self.btn_search_clear)

        # 2. ソートキー選択 (Task 2)
        toolbar_layout.addWidget(QLabel("ソート順:", self))
        self.sort_combo = QComboBox(self)
        self.sort_combo.addItem("ファイル名",  "path")
        self.sort_combo.addItem("サイズ(MB)", "filesize")
        self.sort_combo.addItem("幅",         "width")
        self.sort_combo.addItem("高さ",       "height")
        self.sort_combo.addItem("幅×高さ",    "resolution")
        self.sort_combo.addItem("追加日時",   "added")
        self.sort_combo.currentIndexChanged.connect(self.trigger_search)
        toolbar_layout.addWidget(self.sort_combo)

        # 3. 昇順・降順切替
        self.sort_order_combo = QComboBox(self)
        self.sort_order_combo.addItem("昇順", "ASC")
        self.sort_order_combo.addItem("降順", "DESC")
        self.sort_order_combo.currentIndexChanged.connect(self.trigger_search)
        toolbar_layout.addWidget(self.sort_order_combo)

        # 4. 更新ボタン（F5）: 差分検出 → メタ補完 → グリッド再描画 を一括実行
        self.btn_refresh = QPushButton("更新 (F5)", self)
        self.btn_refresh.setToolTip("ファイル差分検出・メタデータ補完・表示更新を一括実行します")
        self.btn_refresh.clicked.connect(self.trigger_full_refresh)
        toolbar_layout.addWidget(self.btn_refresh)

        # ツールバー本体へ登録
        self.top_toolbar = QToolBar("メインツールバー", self)
        self.top_toolbar.addWidget(toolbar_widget)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.top_toolbar)

        # --- メイン領域スプリッター（左右） ---
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        
        # 左ペイン: フォルダツリー
        self.folder_tree = FolderTreeWidget(self)
        self.folder_tree.folder_selected.connect(self.on_folder_selected)
        self.folder_tree.scan_requested.connect(self._on_folder_scan_requested)
        # バグ修正(タスクA): フォルダ監視解除時に、対象範囲に含まれる
        # バックグラウンドタグ付けワーカーへ中断要求を出す
        self.folder_tree.folder_unwatched.connect(self._on_folder_unwatched)
        # バグ修正ではなく新機能: サムネイルビュー→フォルダツリーへの
        # D&Dでコピー/移動を行った際、メインウィンドウ側の表示を
        # 即座に反映させる。
        self.folder_tree.files_operation_done.connect(self.trigger_search)
        # タグ一覧タブ: タグクリックで検索バーに追記
        self.folder_tree.bookmark_pane.tag_list_pane.tag_clicked.connect(
            self._on_tag_list_clicked
        )
        self.main_splitter.addWidget(self.folder_tree)

        # 右ペイン: 縦分割 (右上: 詳細リスト / 右下: サムネイルグリッド) と 下部: プレビュー (別スプリッタ)
        right_container = QWidget(self)
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 右側のメインスプリッター (詳細リスト & グリッド) と 下部プレビューを分ける
        self.right_vertical_splitter = QSplitter(Qt.Orientation.Vertical, self)

        # 右上の「詳細リスト ＆ グリッド」エリアスプリッター
        self.item_view_splitter = QSplitter(Qt.Orientation.Vertical, self)

        # (1) 詳細リスト (Task 5)
        self.details_table = QTableWidget(self)
        self.details_table.setColumnCount(4)
        self.details_table.setHorizontalHeaderLabels(["ファイル名", "サイズ", "解像度", "パス"])
        self.details_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.details_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.details_table.setAlternatingRowColors(True)
        self.details_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.details_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.details_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.details_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        # フォーカス有無に関わらず選択色・テキスト色を統一
        # Fusionスタイル + Windowsダークモード環境では palette(text) が薄くなるため明示指定
        self.details_table.setStyleSheet("""
            QTableWidget {
                color: #e8e8e8;
                gridline-color: #3a3a3a;
                alternate-background-color: rgba(255,255,255,8);
            }
            QTableWidget::item {
                color: #e8e8e8;
                padding: 2px 4px;
            }
            QTableWidget::item:selected {
                background-color: #2a6496;
                color: #ffffff;
            }
            QTableWidget::item:selected:!active {
                background-color: #2a6496;
                color: #ffffff;
            }
            QHeaderView::section {
                color: #cccccc;
                padding: 4px;
                border: none;
                border-bottom: 1px solid #3a3a3a;
            }
        """)
        self.details_table.currentItemChanged.connect(self.on_table_current_item_changed)

        # Ctrl++ / Ctrl+= : 詳細テーブルの列幅を内容に合わせて自動調整
        sc_fit = QShortcut(QKeySequence("Ctrl++"), self)
        sc_fit.activated.connect(self._fit_table_columns)
        sc_fit2 = QShortcut(QKeySequence("Ctrl+="), self)
        sc_fit2.activated.connect(self._fit_table_columns)
        # ダブルクリックでも調整（列ヘッダーをダブルクリック）
        self.details_table.horizontalHeader().sectionDoubleClicked.connect(
            lambda _: self._fit_table_columns()
        )
        
        self.item_view_splitter.addWidget(self.details_table)

        # (2) サムネイルグリッド
        self.thumbnail_grid = ThumbnailGridWidget(self)
        self.thumbnail_grid.selection_changed.connect(self.on_grid_selection_changed)
        self.thumbnail_grid.open_in_sdi.connect(self.open_sdi_window)
        self.thumbnail_grid.file_operation_done.connect(self.trigger_search)
        self.thumbnail_grid.drop_requested.connect(self._on_drop_requested)
        self.thumbnail_grid.folder_navigate.connect(self._on_folder_navigate)
        self.thumbnail_grid.bulk_tag_requested.connect(self._on_bulk_tag_edit)
        self.thumbnail_grid.retag_with_settings_requested.connect(self._on_retag_with_settings)
        self.thumbnail_grid.lora_export_requested.connect(self._on_lora_export)
        self.thumbnail_grid.similar_tag_search_requested.connect(self._on_similar_tag_search_requested)
        self.item_view_splitter.addWidget(self.thumbnail_grid)

        # 均等に配置
        self.item_view_splitter.setStretchFactor(0, 3)
        self.item_view_splitter.setStretchFactor(1, 7)

        self.right_vertical_splitter.addWidget(self.item_view_splitter)

        # (3) タグペイン — 撤去済み（外部レビュワー指摘対応）。
        # 従来は非表示(hide())のまま保持していたが、preview_pane.py の
        # TagPaneWidget.select_image() が同期的な quit()+wait() で
        # TagFetchWorker の完了を待つブロッキング実装のままであり、
        # グリッドの矢印キー/クリックによる選択変更のたびに、誰にも見えない
        # 非表示ウィジェットのためだけにGUIスレッドがSQLite I/O完了まで
        # 実際にブロックされていた（tag_panel.py TagPanel は
        # セッション21-22で非同期パターンに修正済みだが、この非表示ペインには
        # 移植されていなかった）。表示する予定が無いため、修正して残すのではなく
        # 配線ごと撤去する（preview_pane.py 自体も削除対象）。

        right_layout.addWidget(self.right_vertical_splitter)
        self.main_splitter.addWidget(right_container)

        # 左右スプリッタ比率設定 (左25%, 右75%)
        self.main_splitter.setStretchFactor(0, 2)
        self.main_splitter.setStretchFactor(1, 8)

        self.setCentralWidget(self.main_splitter)

        # タイマーによる検索のデバウンス（入力ラグ防止）
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.trigger_search)

        # F5キー用のQActionバインド
        self.refresh_action = QAction(self)
        self.refresh_action.setShortcut("F5")
        self.refresh_action.triggered.connect(self.trigger_full_refresh)
        self.addAction(self.refresh_action)

        # --- ファイラー系ショートカット（Ctrl+C/X/V, F2, F8, Ctrl+F） ---
        #
        # いずれも QAction.setShortcut() + self.addAction() でウィンドウ
        # 全体（Qt.ShortcutContext.WindowShortcut、既定値）に登録する。
        # これにより、thumbnail_grid.py の keyPressEvent 内にある素の
        # "C"/"V" キー判定（修飾キーを見ていない）とは、Qtの修飾キー
        # 完全一致によるショートカット解決の時点で分離される
        # （QShortcutOverride は修飾キー付きの組み合わせを先に横取りする
        # ため、素キー側の keyPressEvent には届かない）。フォーカスが
        # 検索欄やフォルダツリーにあっても発火する。

        self.copy_clip_action = QAction(self)
        self.copy_clip_action.setShortcut("Ctrl+C")
        self.copy_clip_action.triggered.connect(self._on_shortcut_copy)
        self.addAction(self.copy_clip_action)

        self.cut_clip_action = QAction(self)
        self.cut_clip_action.setShortcut("Ctrl+X")
        self.cut_clip_action.triggered.connect(self._on_shortcut_cut)
        self.addAction(self.cut_clip_action)

        self.paste_clip_action = QAction(self)
        self.paste_clip_action.setShortcut("Ctrl+V")
        self.paste_clip_action.triggered.connect(self._on_shortcut_paste)
        self.addAction(self.paste_clip_action)

        self.rename_action_f2 = QAction(self)
        self.rename_action_f2.setShortcut("F2")
        self.rename_action_f2.triggered.connect(self._on_shortcut_rename)
        self.addAction(self.rename_action_f2)

        self.new_folder_action_f8 = QAction(self)
        self.new_folder_action_f8.setShortcut("F8")
        self.new_folder_action_f8.triggered.connect(self._on_shortcut_new_folder)
        self.addAction(self.new_folder_action_f8)

        self.focus_search_action = QAction(self)
        self.focus_search_action.setShortcut("Ctrl+F")
        self.focus_search_action.triggered.connect(self._on_shortcut_focus_search)
        self.addAction(self.focus_search_action)

        # Escapeは検索欄にフォーカスがある時だけ有効にしたいので、
        # ウィンドウ全体のQActionではなく search_input 自身に
        # WidgetShortcut として登録する（他ダイアログのEscape=キャンセルと
        # 衝突させないため）。
        self.search_escape_shortcut = QShortcut(QKeySequence("Escape"), self.search_input)
        self.search_escape_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.search_escape_shortcut.activated.connect(self._on_search_escape)

    def _on_shortcut_copy(self) -> None:
        """Ctrl+C: サムネイルグリッドの選択ファイルをクリップボードにコピー登録する。"""
        self.thumbnail_grid.copy_selected_to_clipboard()

    def _on_shortcut_cut(self) -> None:
        """Ctrl+X: サムネイルグリッドの選択ファイルをクリップボードに切り取り登録する。"""
        self.thumbnail_grid.cut_selected_to_clipboard()

    def _on_shortcut_paste(self) -> None:
        """Ctrl+V: クリップボードのファイルを現在表示中フォルダへ貼り付ける。"""
        self.thumbnail_grid.paste_from_clipboard(self.current_folder_path)

    def _on_shortcut_rename(self) -> None:
        """F2: サムネイルグリッドの選択中1件をリネームする（複数選択時は無視）。"""
        self.thumbnail_grid.rename_selected()

    def _on_shortcut_new_folder(self) -> None:
        """F8: 現在表示中フォルダの直下に新規フォルダを作成する。"""
        if not self.current_folder_path or not os.path.isdir(self.current_folder_path):
            return
        self.folder_tree._create_folder(self.current_folder_path)

    def _on_shortcut_focus_search(self) -> None:
        """Ctrl+F: タグ検索欄にフォーカスを移し、既存テキストを全選択する。"""
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _on_search_escape(self) -> None:
        """Escape（検索欄フォーカス時）: 検索テキストをクリアし、グリッドにフォーカスを戻す。"""
        self.search_input.clear()
        self.thumbnail_grid.setFocus()

    def trigger_startup_lifecycle_check(self) -> None:
        """
        起動時にバックグラウンドで watched_folders の startup_check スキャンを実行する。
        """
        self.status_bar.showMessage("バックグラウンド同期を実行中...")
        self.lifecycle_worker = LifecycleSyncWorker(self)
        self.lifecycle_worker.finished.connect(self.on_lifecycle_finished)
        self.lifecycle_worker.error.connect(self.on_lifecycle_error)
        self.lifecycle_worker.start()

    def on_lifecycle_finished(self, results: dict) -> None:
        self.status_bar.showMessage(
            f"同期完了: 新規 {results['added']} / 復帰 {results['recovered']} / 行方不明 {results['missing']} / スキップ {results['skipped']}"
        )
        # スキャン同期が終わったため、自動的に現在の表示を最新化
        self.trigger_search()
        # セッション23再設計: 新規/復帰ファイルが増えた＝優先度1・2の対象が
        # 増えた可能性がある契機なので _mark_pipeline_dirty() を呼ぶ。
        # 起動要否・優先度2/3のどちらが動くべきかの判断は_reschedule()に
        # 一本化されており、ここで個別に判断する必要はない。
        QTimer.singleShot(500, self._mark_pipeline_dirty)

    def on_lifecycle_error(self, err_msg: str) -> None:
        self.status_bar.showMessage(f"起動時同期エラー: {err_msg}")

    def trigger_full_refresh(self) -> None:
        """
        F5 / 更新ボタン: lifecycle sync → メタ補完 → グリッド再描画 を一括実行。
        Step1: LifecycleSyncWorker（差分検出）→ 完了後 Step2 へ
        Step2: MetaUpdateWorker（メタ補完）→ 完了後 trigger_search
        """
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("更新中...")
        self.status_bar.showMessage("ファイル差分チェック中...")
        self.sync_worker = LifecycleSyncWorker(self)
        self.sync_worker.finished.connect(self._on_sync_done_then_meta)
        self.sync_worker.error.connect(self._on_refresh_error)
        self.sync_worker.start()

    def _on_sync_done_then_meta(self, result: dict) -> None:
        added   = result.get("added",   0)
        missing = result.get("missing", 0)
        self.status_bar.showMessage(
            f"差分検出完了: 新規 {added} 件 / 行方不明 {missing} 件 → メタデータ補完中..."
        )
        # 同上（新規/復帰ファイルが増えた可能性がある契機なのでパイプライン
        # 全体を再評価する）
        self._mark_pipeline_dirty()
        self.meta_worker = MetaUpdateWorker(self)
        self.meta_worker.progress.connect(self.status_bar.showMessage)
        self.meta_worker.finished.connect(self._on_full_refresh_done)
        self.meta_worker.error.connect(self._on_refresh_error)
        self.meta_worker.start()

    def _on_full_refresh_done(self, meta_count: int) -> None:
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("更新 (F5)")
        self.status_bar.showMessage(f"更新完了: メタデータ補完 {meta_count} 件")
        self.trigger_search()
        # セッション23再設計: 優先度1〜3の起動判断は_reschedule()に一本化
        # されている（on_lifecycle_finishedと同じ理由）。
        QTimer.singleShot(500, self._mark_pipeline_dirty)

    def _on_refresh_error(self, err_msg: str) -> None:
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("更新 (F5)")
        self.status_bar.showMessage(f"更新エラー: {err_msg}")

    def _is_quick_access_folder(self, path: str) -> bool:
        """
        指定フォルダが「クイックアクセス登録済み」(quick_access=1)かどうかを返す。

        バグ修正: folder_is_registered（watch_mode != 'none'）だけでは
        「クイックアクセス登録のみ（watch_mode='none', quick_access=1）」と
        「一度も触れていない無関係なフォルダ」の両方が False になり区別が
        つかなかった。未登録フォルダでのサムネイル先読み（_try_start_priority3
        のpathsフォールバック）は、後者にまで及ぶと際限がないため、
        意図的に登録されたクイックアクセスフォルダのみに絞るための判定。
        """
        if not path:
            return False
        try:
            conn = lifecycle_manager.get_connection()
            norm = path.replace("\\", "/").rstrip("/")
            row = conn.execute(
                "SELECT quick_access FROM watched_folders WHERE path = ?",
                (norm,),
            ).fetchone()
            conn.close()
            return bool(row and row[0])
        except Exception:
            return False

    def _resolve_folder_watch_info(self, path: str) -> tuple[bool, bool]:
        """
        指定フォルダが watched_folders に登録済みかどうかと、
        その recursive 設定を返す。

        Returns:
            (is_registered, recursive)
            - 自身が直接登録済み: そのフォルダの recursive 値
            - 祖先が recursive=1 で登録済み: (True, True)
            - 未登録: (False, False) — v2指示書5章「未登録フォルダは
              直下のみ（再帰しない）」に従う
        """
        if not path:
            return False, False
        try:
            conn = lifecycle_manager.get_connection()
            norm = path.replace("\\", "/").rstrip("/")
            row = conn.execute(
                "SELECT watch_mode, recursive FROM watched_folders WHERE path = ?",
                (norm,),
            ).fetchone()
            if row and row[0] and row[0] != "none":
                conn.close()
                return True, bool(row[1])
            rows = conn.execute(
                "SELECT path FROM watched_folders "
                "WHERE recursive = 1 AND watch_mode != 'none'"
            ).fetchall()
            conn.close()
            is_ancestor_registered = any(
                norm.startswith(r[0].replace("\\", "/").rstrip("/") + "/")
                for r in rows
            )
            return is_ancestor_registered, is_ancestor_registered
        except Exception:
            return False, False

    def on_folder_selected(self, path: str) -> None:
        # v2再設計: フォルダ切替時は即座に①〜③(下記ヘルパー参照)へ
        # 中断要求を出す（実際の新フォルダでの再始動はデバウンス後、
        # _reschedule()経由）。
        self._interrupt_worker(self._bg_tagger_worker, "[BGTagger]")
        self._interrupt_worker(self._idle_tagger_worker, "[IdleTagger]")
        self._interrupt_worker(self._bg_thumb_worker, "[BGThumb]")

        self._pending_folder_path = path
        self._folder_select_timer.start(300)  # 表示用デバウンス（実機確認: 150msでは未登録フォルダ初回表示時のサムネ生成ポップインが目立ったため300msに調整）
        self._folder_bg_timer.start(3000)     # 背景ワーカー起動判断用デバウンス（900ms→3000msへ変更・本採用。優先度2〈他フォルダタグ付け〉の再始動によるサムネイル生成ポップイン悪化を実機確認の上で対応）

    def _on_folder_select_debounced(self) -> None:
        """
        フォルダ選択デバウンス（表示用・300ms）確定後の実処理。
        ①(表示)のみを担当する。②③(背景ワーカー)の起動判断は
        _on_folder_bg_debounced()（900ms側）に分離済み。
        """
        path = self._pending_folder_path

        # 同一フォルダの再選択（フォルダツリーの再クリック等）では何もしない。
        if path == self.current_folder_path:
            return

        self.current_folder_path = path
        self.thumbnail_grid.set_images([])

        self._current_folder_recursive = self._resolve_folder_watch_info(path)[1]

        # フォルダ切り替え時: 表示フォルダと一致しないSDIウィンドウを閉じる
        norm_new = path.replace("\\", "/").rstrip("/")
        for sdi in list(self.sdi_windows):
            try:
                sdi_folder = os.path.dirname(
                    sdi.file_path.replace("\\", "/")
                ).rstrip("/")
                if sdi_folder != norm_new:
                    sdi.close()
            except RuntimeError:
                pass  # すでに破棄済み

        # ①表示中の範囲を描画（即時）
        self.trigger_search()

    def _on_folder_bg_debounced(self) -> None:
        """
        フォルダ選択デバウンス（背景ワーカー起動判断用・3000ms）確定後の
        実処理。②③(バックグラウンドタグ付け・サムネイル先読み)の起動
        要否判断（_reschedule()）のみを、表示側(300ms)とは独立して行う
        （高速化検討・akaakaさんとの熟議により分離。表示側デバウンスは
        必ずこちらより先に確定するため、この時点では current_folder_path
        は既に確定済みの値になっている）。
        _mark_pipeline_dirty() は他の複数箇所からも呼ばれる冪等な
        「再チェック依頼」のため、同一フォルダの再選択（ノーオペ）で
        余分に呼ばれても実害はないと判断し、ここでは無条件に呼ぶ。
        """
        self._mark_pipeline_dirty()

    def _on_show_subfolder_files_toggled(self, checked: bool) -> None:
        print(f"[View] サブフォルダ表示: {'ON' if checked else 'OFF'}", flush=True)
        self.trigger_search()

    def trigger_search_debounce(self) -> None:
        # バグ修正: 300msでは早すぎ、タイプ完了前に絞り込みが発生していた。
        # 検索結果反映(set_images())は選択復元のため thumbnail_grid.setFocus()
        # を呼ぶ関係上、絞り込みが走るたびに検索欄からグリッドへフォーカスが
        # 奪われる。タイプ中に何度も絞り込みが挟まるとフォーカスが行き来し、
        # 「まだ入力中のつもりが直前の文字がグリッド側のベアキーショートカット
        # （C/M/V等）として誤反応する」不具合の原因になっていた。
        # 1000msに延ばし、入力が一区切りついてから検索する。
        self.search_timer.start(1000)  # 1000ms静止後にクエリ発行

    def trigger_search(self) -> None:
        """
        フォルダがDB登録済みか判定し、適切なWorkerで検索する。
        - 登録済み: SearchWorker（タグ+ファイル名、DBソート）
        - 未登録  : FilesystemSearchWorker（ファイル名のみ、OS直読み）
        """
        # v2再設計: ①(表示)が動き出すので優先度2(他フォルダタグ付け)が
        # 動いていれば即座に中断する（邪魔をしない、が第一原則）。
        self._interrupt_worker(self._idle_tagger_worker, "[IdleTagger]")

        if self.active_search_worker is not None:
            if self.active_search_worker.isRunning():
                # requestInterruptionでWorker側にキャンセルを通知し、
                # quit()でイベントループ終了を要求。wait()はしない
                # （メインスレッドをブロックして遅延させないため）
                self.active_search_worker.requestInterruption()
                self.active_search_worker.quit()
                # finished シグナルが来ても on_search_finished を呼ばないよう切断
                try:
                    self.active_search_worker.finished.disconnect()
                except Exception:
                    pass
            self.active_search_worker = None

        sort_key   = self.sort_combo.currentData()
        sort_order = self.sort_order_combo.currentData()

        # DB登録状態を確認。
        # 選択フォルダ自身が登録されている場合だけでなく、
        # 親フォルダが recursive=True で登録されている場合も「登録済み」とみなす。
        # 注意: folder_recursive はDBスキャン範囲（watched_folders設定）。
        # 表示上のサブフォルダ表示可否はこれとは独立した
        # action_show_subfolder_files トグル（デフォルトOFF）で制御する。
        self.folder_is_registered, _folder_scan_recursive = self._resolve_folder_watch_info(
            self.current_folder_path
        )
        display_recursive = self.action_show_subfolder_files.isChecked()

        if self.folder_is_registered or not self.current_folder_path:
            # --- DB登録済み or 全件表示 ---
            self.search_input.setPlaceholderText(
                "スペース区切りでAND検索 (例: girl blue_hair)"
            )
            self.active_search_worker = SearchWorker(
                folder_path=self.current_folder_path,
                tag_query=self.search_input.text(),
                sort_key=sort_key,
                sort_order=sort_order,
                recursive=display_recursive,
                parent=self
            )
        else:
            # --- 未登録フォルダ: OSファイルシステム直読み ---
            self.search_input.setPlaceholderText(
                "ファイル名で絞り込み（未登録フォルダ: タグ検索無効）"
            )
            self.active_search_worker = FilesystemSearchWorker(
                folder_path=self.current_folder_path,
                name_filter=self.search_input.text(),
                sort_key=sort_key,
                sort_order=sort_order,
                parent=self
            )

        self.active_search_worker.finished.connect(self.on_search_finished)
        self.active_search_worker.error.connect(self.on_search_error)
        self.active_search_worker.start()

    def on_search_finished(self, results: list[tuple]) -> None:
        self.search_results = results

        # 1. サムネイルグリッド更新
        self.thumbnail_grid.set_images(results)

        # 2. 詳細メタリスト更新
        self.update_details_table(results)

        # 3. タグ一覧タブ更新（DB登録済み画像のタグを集計）
        self.folder_tree.bookmark_pane.tag_list_pane.update_for_results(results)

        # 4. ステータスバーに登録状態を付加
        if self.current_folder_path and not self.folder_is_registered:
            self.status_bar.showMessage(
                f"検索結果: {len(results)} 件　※未登録フォルダ（ファイル名検索のみ・タグ無効）"
            )
        else:
            self.status_bar.showMessage(f"検索結果: {len(results)} 件")

    def on_search_error(self, message: str) -> None:
        self.status_bar.showMessage(f"検索エラー: {message}")

    def update_details_table(self, results: list[tuple]) -> None:
        """詳細リスト (QTableWidget) のアイテムを全更新"""
        self.details_table.blockSignals(True)
        self.details_table.setRowCount(len(results))
        
        for idx, (img_id, path, w, h, size) in enumerate(results):
            
            # ファイル名
            filename_item = QTableWidgetItem(os.path.basename(path))
            filename_item.setData(Qt.ItemDataRole.UserRole, path)
            filename_item.setFlags(filename_item.flags() ^ Qt.ItemFlag.ItemIsEditable)
            self.details_table.setItem(idx, 0, filename_item)
            
            # サイズ表示 (KB / MB)
            size_str = "-"
            if size > 0:
                if size >= 1024 * 1024:
                    size_str = f"{size / (1024 * 1024):.2f} MB"
                else:
                    size_str = f"{size / 1024:.1f} KB"
            size_item = QTableWidgetItem(size_str)
            size_item.setFlags(size_item.flags() ^ Qt.ItemFlag.ItemIsEditable)
            self.details_table.setItem(idx, 1, size_item)

            # 解像度 (w x h)
            res_str = "-" if (w == 0 or h == 0) else f"{w} x {h}"
            res_item = QTableWidgetItem(res_str)
            res_item.setFlags(res_item.flags() ^ Qt.ItemFlag.ItemIsEditable)
            self.details_table.setItem(idx, 2, res_item)

            # フルパス
            path_item = QTableWidgetItem(path)
            path_item.setFlags(path_item.flags() ^ Qt.ItemFlag.ItemIsEditable)
            self.details_table.setItem(idx, 3, path_item)

        self.details_table.blockSignals(False)
        # データ更新後に列幅を自動調整（Ctrl++ と同じ動作）
        self._fit_table_columns()

    # --- Task 5: 詳細リスト ＆ サムネイルグリッドの選択相互連動 ---
    def _batch_rename_dialog(self) -> None:
        """
        選択中またはグリッド全件を連番リネームするダイアログ。
        Linar準拠: 置換対象文字列 + 一括置換書式（$(FN)・#桁数#）
        例: 置換対象「$(FN)」、書式「test-#4#」→ test-0001.jpg, test-0002.jpg ...
        """
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel,
            QLineEdit, QDialogButtonBox, QCheckBox, QSpinBox
        )

        # 対象ファイルを決定（選択中があればそれ、なければ全件）
        if self.thumbnail_grid.selected_item:
            targets = [self.thumbnail_grid.selected_item.path]
        else:
            targets = [
                path for (img_id, path, _, _, _) in self.thumbnail_grid.image_data
                if img_id != -2  # フォルダアイテムを除外
            ]
        if not targets:
            QMessageBox.information(self, "一括リネーム", "対象ファイルがありません。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("一括連番リネーム")
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)

        # 説明
        layout.addWidget(QLabel(
            f"対象: {len(targets)} ファイル\n\n"
            "$(FN) = 元ファイル名（拡張子なし）\n"
        ))

        # 書式入力
        fmt_layout = QHBoxLayout()
        fmt_layout.addWidget(QLabel("ファイル名書式:"))
        fmt_edit = QLineEdit("$(FN)-#4#")
        fmt_layout.addWidget(fmt_edit)
        layout.addLayout(fmt_layout)

        # 開始番号
        start_layout = QHBoxLayout()
        start_layout.addWidget(QLabel("開始番号:"))
        start_spin = QSpinBox()
        start_spin.setRange(0, 99999)
        start_spin.setValue(1)
        start_layout.addWidget(start_spin)
        layout.addLayout(start_layout)

        # プレビュー
        preview_lbl = QLabel()
        preview_lbl.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        layout.addWidget(preview_lbl)

        def _update_preview():
            fmt = fmt_edit.text()
            start = start_spin.value()
            samples = []
            for i, path in enumerate(targets[:3]):
                stem = os.path.splitext(os.path.basename(path))[0]
                ext  = os.path.splitext(path)[1]
                name = fmt.replace("$(FN)", stem)
                # #N# を N桁の連番に置換
                import re
                def _repl(m):
                    digits = int(m.group(1))
                    return str(start + i).zfill(digits)
                name = re.sub(r"#(\d+)#", _repl, name)
                samples.append(name + ext)
            preview_lbl.setText("プレビュー: " + ", ".join(samples)
                                 + ("..." if len(targets) > 3 else ""))

        fmt_edit.textChanged.connect(_update_preview)
        start_spin.valueChanged.connect(_update_preview)
        _update_preview()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        # 実行
        import re
        fmt   = fmt_edit.text()
        start = start_spin.value()
        errors = []
        for i, path in enumerate(targets):
            stem = os.path.splitext(os.path.basename(path))[0]
            ext  = os.path.splitext(path)[1]
            name = fmt.replace("$(FN)", stem)
            def _repl(m, _n=i):
                digits = int(m.group(1))
                return str(start + _n).zfill(digits)
            name = re.sub(r"#(\d+)#", _repl, name)
            new_path = os.path.join(os.path.dirname(path), name + ext)
            if path == new_path:
                continue
            try:
                os.rename(path, new_path)
                # DB更新
                # バグ修正: 従来はこのUPDATEが失敗しても except: pass で
                # 握りつぶしており、「ファイルは新パスに移動済みだがDBは
                # 旧パスのまま」という不整合がユーザーに一切通知されない
                # まま発生し得た（次回スキャンで旧パスがMISSING化し、
                # 蓄積済みタグとの紐付けが失われるリスクがある）。
                # errors リストに集約してダイアログで可視化し、conn.close()
                # もfinallyで保証する（他のconnリーク修正と同じ方針）。
                db_conn = None
                try:
                    db_conn = lifecycle_manager.get_connection()
                    db_conn.execute(
                        "UPDATE images SET path = ? WHERE path = ?",
                        (new_path.replace("\\", "/"),
                         path.replace("\\", "/"))
                    )
                    db_conn.commit()
                except Exception as db_e:
                    errors.append(
                        f"{os.path.basename(path)}: ファイル名は変更されましたが"
                        f"DB更新に失敗しました ({db_e})"
                    )
                finally:
                    if db_conn is not None:
                        try:
                            db_conn.close()
                        except Exception:
                            pass
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")

        if errors:
            QMessageBox.warning(self, "一括リネーム",
                f"一部失敗しました:\n" + "\n".join(errors[:5]))
        else:
            QMessageBox.information(self, "一括リネーム",
                f"{len(targets)} ファイルをリネームしました。")
        self.trigger_search()

    def _fit_table_columns(self) -> None:
        """
        詳細テーブルの列幅を内容（最初の100件）に合わせて高速自動調整する。
        Ctrl++ / Ctrl+= キー、または列ヘッダーのダブルクリックで実行。
        """
        row_count = self.details_table.rowCount()
        if row_count == 0:
            return
            
        fm = self.details_table.fontMetrics()
        
        # 各列の最小・最大幅の基準値
        min_widths = [150, 80, 80] # ファイル名、サイズ、解像度
        max_widths = [450, 150, 150]
        
        col_widths = list(min_widths)
        
        # 最初の100件のみを走査して最大幅を決定
        scan_limit = min(100, row_count)
        for row in range(scan_limit):
            for col in range(3):
                item = self.details_table.item(row, col)
                if item:
                    text_w = fm.horizontalAdvance(item.text()) + 16 # パディング追加
                    if text_w > col_widths[col]:
                        col_widths[col] = text_w
                        
        # 範囲内にクランプして列幅を適用
        hdr = self.details_table.horizontalHeader()
        for col in range(3):
            width = min(max_widths[col], max(min_widths[col], col_widths[col]))
            self.details_table.setColumnWidth(col, width)
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            
        # パス列は Stretch
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    def on_table_current_item_changed(self, current, previous) -> None:
        """
        詳細テーブルの currentItem が変わった時の連動処理。
        キーボード上下矢印・ホイールスクロール後のクリック等で発火する。
        """
        if current is None:
            return
        row = current.row()
        filename_item = self.details_table.item(row, 0)
        if filename_item is None:
            return
        target_path = filename_item.data(Qt.ItemDataRole.UserRole)
        if not target_path:
            return

        # グリッド側の選択を同期（シグナルループ防止のため blockSignals）
        self.thumbnail_grid.blockSignals(True)
        self.thumbnail_grid.select_by_path(target_path)
        self.thumbnail_grid.blockSignals(False)

    def on_grid_selection_changed(self, img_id: int, path: str) -> None:
        """
        サムネイルグリッド側で選択が変更された場合の連動処理。
        グリッドのキーボードナビ・クリックどちらからも呼ばれる。
        img_id == -1 は未登録フォルダ（DB IDなし）。
        """
        # 詳細テーブル側の同期（currentItemChanged が発火しないよう blockSignals）
        self.details_table.blockSignals(True)
        found = False
        for row in range(self.details_table.rowCount()):
            item = self.details_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == path:
                self.details_table.selectRow(row)
                self.details_table.scrollToItem(item)
                found = True
                break
        if not found:
            self.details_table.clearSelection()
        self.details_table.blockSignals(False)

        # キーボードナビ後もグリッドにフォーカスを維持
        self.thumbnail_grid.setFocus()

    # --- 状態復元 / 保存 (QSettings) ---
    def save_settings(self) -> None:
        settings = QSettings("D-liner", "D-liner")
        settings.setValue("mainWindowGeometry", self.saveGeometry())
        settings.setValue("mainSplitterState", self.main_splitter.saveState())
        settings.setValue("itemViewSplitterState", self.item_view_splitter.saveState())
        settings.setValue("singleSdiMode", self.single_sdi_mode)
        settings.setValue("showSubfolderFiles", self.action_show_subfolder_files.isChecked())
        if self.current_folder_path:
            settings.setValue("lastFolder", self.current_folder_path)

    def load_settings(self) -> None:
        settings = QSettings("D-liner", "D-liner")

        geom = settings.value("mainWindowGeometry")
        if geom:
            self.restoreGeometry(geom)

        state = settings.value("mainSplitterState")
        if state:
            self.main_splitter.restoreState(state)

        i_v_state = settings.value("itemViewSplitterState")
        if i_v_state:
            self.item_view_splitter.restoreState(i_v_state)

        self.single_sdi_mode = settings.value("singleSdiMode", True, type=bool)

        # サブフォルダ表示トグル復元（デフォルトOFF）。ここでは信号を止めて
        # おく。まだフォルダ未選択の段階で trigger_search() が空実行される
        # のを避けるため（実際の検索は下の select_path 経由で走る）。
        self.action_show_subfolder_files.blockSignals(True)
        self.action_show_subfolder_files.setChecked(
            settings.value("showSubfolderFiles", False, type=bool)
        )
        self.action_show_subfolder_files.blockSignals(False)

        last_folder = settings.value("lastFolder", "")
        if last_folder and os.path.exists(last_folder):
            self.current_folder_path = last_folder
            QTimer.singleShot(100, lambda: self.folder_tree.select_path(last_folder))

    # --- フォルダナビゲーション（サムネイル内フォルダをダブルクリック）---
    def _on_folder_navigate(self, path: str) -> None:
        """サムネイルグリッドのフォルダアイテムをダブルクリック → そのフォルダに移動"""
        self.folder_tree.select_path(path)
        self.on_folder_selected(path)

    # --- D&Dドロップハンドラ ---
    def _on_drop_requested(self, paths: list, is_move: bool) -> None:
        """
        サムネイルグリッドへのD&Dを処理する。
        ドロップ先は現在表示中フォルダ (self.current_folder_path)。
        同一フォルダへのドロップは無視。
        """
        import shutil as _shutil

        dest_dir = self.current_folder_path
        if not dest_dir:
            return

        errors = []
        for src_path in paths:
            src_path = src_path.replace("\\", "/")
            src_dir = os.path.dirname(src_path)
            if src_dir.replace("\\", "/").rstrip("/") == dest_dir.replace("\\", "/").rstrip("/"):
                continue  # 同一フォルダは無視

            filename = os.path.basename(src_path)
            dest_path = os.path.join(dest_dir, filename).replace("\\", "/")

            try:
                if os.path.exists(dest_path):
                    reply = QMessageBox.question(
                        self, "確認",
                        f"「{filename}」はコピー先に既に存在します。上書きしますか?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        continue

                if is_move:
                    _shutil.move(src_path, dest_path)
                    # バグ修正: DB更新失敗のexcept: passを撤去。ファイルは
                    # 移動済みだがDB pathが旧パスのままという不整合を
                    # errorsに集約して通知する（リネーム処理と同じ方針）。
                    db_conn = None
                    try:
                        db_conn = lifecycle_manager.get_connection()
                        db_conn.execute(
                            "UPDATE images SET path = ? WHERE path = ?",
                            (dest_path, src_path)
                        )
                        db_conn.commit()
                    except Exception as db_e:
                        errors.append(
                            f"{filename}: ファイルは移動されましたが"
                            f"DB更新に失敗しました ({db_e})"
                        )
                    finally:
                        if db_conn is not None:
                            try:
                                db_conn.close()
                            except Exception:
                                pass
                else:
                    _shutil.copy2(src_path, dest_path)
            except Exception as e:
                errors.append(f"{filename}: {e}")

        if errors:
            QMessageBox.critical(self, "エラー", "一部の操作が失敗しました:\n" + "\n".join(errors))

        # グリッドを再描画（コピー/移動後のファイルを表示）
        self.trigger_search()

    # --- フォルダ登録/解除後の反映 ---
    def _on_folder_scan_requested(self, path: str) -> None:
        norm = path.replace("\\", "/").rstrip("/")
        cur = self.current_folder_path.replace("\\", "/").rstrip("/")
        if norm == cur:
            self.trigger_search()
            # バグ修正: 未登録フォルダを「スキャンして登録」した直後は、
            # DB登録が完了して初めて優先度1(現在フォルダのタグ付け)が
            # 動ける状態になる。新しく対象が増えた契機として
            # _mark_pipeline_dirty() を呼ぶ。
            self._mark_pipeline_dirty()

    # --- SDIウィンドウ管理 ---
    def open_sdi_window(self, path: str) -> None:
        all_paths = [r[1] for r in self.search_results]

        if self.single_sdi_mode:
            # 単一SDIモード: 既存ウィンドウがあれば画像を切り替えて前面に
            # WA_DeleteOnClose使用のため isHidden() は使わず sdi_windows リストで管理
            if self.sdi_windows:
                sdi = self.sdi_windows[0]
                try:
                    sdi.all_files = all_paths
                    sdi.navigate_to(path)
                    sdi.raise_()
                    sdi.activateWindow()
                    return
                except RuntimeError:
                    # C++側オブジェクトがすでに破棄されていた場合
                    self.sdi_windows.clear()
        else:
            for sdi in list(self.sdi_windows):
                try:
                    if sdi.file_path == path.replace("\\", "/"):
                        sdi.raise_()
                        sdi.activateWindow()
                        return
                except RuntimeError:
                    self.sdi_windows.remove(sdi)

        sdi = SDIWindow(path, all_paths, main_window=self)
        sdi.closed.connect(self._on_sdi_closed)
        sdi.selection_request.connect(self._on_sdi_selection_request)
        self.sdi_windows.append(sdi)

        screen = self.screen()
        if screen is None:
            from PyQt6.QtGui import QGuiApplication as _QGA
            screen = _QGA.primaryScreen()
        sdi.show()
        _screen = screen
        def _center_sdi() -> None:
            ag2 = _screen.availableGeometry()
            fg2 = sdi.frameGeometry()
            # 中央座標を計算してから availableGeometry 内に clamp
            # タスクバーが上/左/右/下どこにあっても安全
            cx = ag2.left() + (ag2.width()  - fg2.width())  // 2
            cy = ag2.top()  + (ag2.height() - fg2.height()) // 2
            cx = max(ag2.left(), min(cx, ag2.right()  + 1 - fg2.width()))
            cy = max(ag2.top(),  min(cy, ag2.bottom() + 1 - fg2.height()))
            sdi.move(cx, cy)
        from PyQt6.QtCore import QTimer as _QT
        _QT.singleShot(0, _center_sdi)

    def _on_sdi_closed(self, path: str) -> None:
        # WA_DeleteOnClose: closedシグナルの送信者を直接除去
        # isHidden() は破棄済みオブジェクトで呼べないため sender() を使う
        sender = self.sender()
        if sender in self.sdi_windows:
            self.sdi_windows.remove(sender)

    def _on_sdi_selection_request(self, path: str) -> None:
        self.thumbnail_grid.select_by_path(path)

    def close_all_sdi_windows(self) -> None:
        for sdi in list(self.sdi_windows):
            try:
                sdi.close()
            except RuntimeError:
                pass  # すでに破棄済み
        self.sdi_windows.clear()

    # --- サムネイルキャッシュ ---

    def _init_thumb_cache(self) -> None:
        """サムネイルキャッシュを初期化してグリッドに注入する。"""
        try:
            from thumbnail_cache import ThumbnailCache
            settings = QSettings("D-liner", "D-liner")
            self._thumb_cache = ThumbnailCache(settings)
            self.thumbnail_grid.set_cache(self._thumb_cache)

            # サイズ警告チェック（起動時）
            size_mb = self._thumb_cache.get_size_mb()
            warn_mb = int(settings.value("cache/warn_size_mb", 1024))
            if size_mb >= warn_mb:
                self.status_bar.showMessage(
                    f"キャッシュが {size_mb:.0f} MB を超えています。"
                    "ツール > キャッシュメンテナンス で清掃できます。",
                    8000,
                )
        except Exception as e:
            print(f"[MainWindow] ThumbnailCache init failed: {e}", flush=True)

    def _on_cache_maintenance(self) -> None:
        """ツール > キャッシュメンテナンス ダイアログ"""
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QFormLayout, QLabel,
            QDialogButtonBox, QPushButton, QHBoxLayout,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle("キャッシュメンテナンス")
        dlg.setMinimumWidth(400)
        outer = QVBoxLayout(dlg)

        # 現在の状態
        if self._thumb_cache is not None:
            size_mb = self._thumb_cache.get_size_mb()
            count   = self._thumb_cache.get_entry_count()
            loc     = str(self._thumb_cache._db_path)
        else:
            size_mb, count, loc = 0.0, 0, "(利用不可)"

        info_layout = QFormLayout()
        info_layout.addRow("現在のキャッシュ:",
                           QLabel(f"{size_mb:.1f} MB  /  {count:,} 件"))
        info_layout.addRow("保存先:", QLabel(loc))
        outer.addLayout(info_layout)
        outer.addSpacing(8)

        # ボタン行
        btn_row = QHBoxLayout()

        btn_clean = QPushButton("今すぐ清掃")
        btn_clean.setToolTip("retention_days 以上アクセスのないエントリを削除します")
        btn_clean.setEnabled(self._thumb_cache is not None)

        btn_all = QPushButton("全削除")
        btn_all.setStyleSheet(
            "QPushButton { color: #ff6b6b; } QPushButton:hover { color: #ff4444; }"
        )
        btn_all.setEnabled(self._thumb_cache is not None)

        btn_row.addWidget(btn_clean)
        btn_row.addWidget(btn_all)
        btn_row.addStretch()
        outer.addLayout(btn_row)

        # 結果ラベル
        result_label = QLabel("")
        result_label.setStyleSheet("color: palette(mid); font-size: 11px;")
        outer.addWidget(result_label)

        outer.addStretch()
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        outer.addWidget(btns)

        def do_clean():
            deleted = self._thumb_cache.clean_now()
            new_mb = self._thumb_cache.get_size_mb()
            new_count = self._thumb_cache.get_entry_count()
            result_label.setText(
                f"清掃完了: {deleted} 件削除 → {new_mb:.1f} MB / {new_count:,} 件"
            )

        def do_delete_all():
            reply = QMessageBox.question(
                dlg, "全削除の確認",
                "キャッシュを全て削除しますか？\n次回起動時に再生成されます。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            self._thumb_cache.delete_all()
            # インメモリキャッシュもクリア
            self.thumbnail_grid.cache.clear()
            result_label.setText("全削除完了。")

        btn_clean.clicked.connect(do_clean)
        btn_all.clicked.connect(do_delete_all)

        dlg.exec()

    # --- タグ一覧タブ ---

    def _on_tag_list_clicked(self, tag: str) -> None:
        """
        タグ一覧タブでタグをクリック → 検索バーに追記して即検索。

        - 既に同じトークンが含まれていれば追記しない
        - アンダースコア形式のまま追記（SearchWorker 側で正規化済み）
        """
        current_tokens = self.search_input.text().split()
        if tag not in current_tokens:
            new_text = (self.search_input.text().strip() + " " + tag).strip()
            self.search_input.setText(new_text)
            # textChanged → trigger_search_debounce が自動で発火するため
            # 明示的な trigger_search 呼び出しは不要

    def _on_similar_tag_search_requested(self, tags_str: str) -> None:
        """
        サムネイル右クリック「似たタグの画像を探す」（指示書03 タスクD）。
        検索欄を抽出タグで全置換し、1回だけ検索を実行する。
        「この組み合わせで検索」（TagPanel._on_search_selected）と同じ
        二重検索防止パターンに揃える。
        """
        self.search_input.setText(tags_str)
        self.search_timer.stop()
        self.trigger_search()

    # --- タガーエンジン ---

    def _init_tagger_engine(self) -> None:
        """
        エンジン接続をバックグラウンドワーカーで非同期実行する。
        connect_or_launch() は最大60秒かかるためメインスレッドでは呼ばない。
        """
        print("[Tagger] 接続試行開始...")
        try:
            from tagger_engine import TaggerEngine
            from workers import TaggerConnectWorker
            settings = QSettings("D-liner", "D-liner")
            self._tagger_engine = TaggerEngine(settings)
            print("[Tagger] TaggerEngine 作成完了。バックグラウンドで接続試行中...")
            self.status_bar.showMessage("タガーエンジン: 接続中...", 0)

            self._tagger_connect_worker = TaggerConnectWorker(
                self._tagger_engine, parent=self
            )
            self._tagger_connect_worker.succeeded.connect(self._on_tagger_connected)
            self._tagger_connect_worker.failed.connect(self._on_tagger_connect_failed)
            self._tagger_connect_worker.start()

        except Exception as e:
            import traceback
            print(f"[Tagger] 初期化エラー:\n{traceback.format_exc()}")
            self.status_bar.showMessage(f"タガーエンジン初期化エラー: {e}", 6000)

    def _on_tagger_connected(self, mode: str) -> None:
        """TaggerConnectWorker 接続成功コールバック（メインスレッド）。"""
        print(f"[Tagger] 接続成功: mode={mode}")
        self.status_bar.showMessage(f"タガーエンジン接続完了 [{mode}]", 4000)
        self.action_tag_one.setEnabled(True)
        self.action_tag_folder.setEnabled(True)

        # --- 凍結中: ComfyUI piggyback ↔ standalone 自動切り替えタイマー ---
        # セッション8 で凍結。StandaloneWD14Backend では不要。
        # 将来の ComfyWorkerBackend 統合時に再有効化すること。
        # if not hasattr(self, "_rebalance_timer") or not self._rebalance_timer.isActive():
        #     self._rebalance_timer = QTimer(self)
        #     self._rebalance_timer.timeout.connect(self._on_rebalance_timer)
        #     self._rebalance_timer.start(30_000)

        # セッション23再設計: 起動要否・優先度間の中断判断は_reschedule()に
        # 一本化された。タガーが使えるようになった＝優先度1・2が動ける
        # ようになった契機として _mark_pipeline_dirty() を呼ぶだけでよい
        # （実行中の③があれば_reschedule()側が自動的に中断する）。
        QTimer.singleShot(500, self._mark_pipeline_dirty)

    def _on_tagger_connect_failed(self) -> None:
        """TaggerConnectWorker 接続失敗コールバック（メインスレッド）。"""
        print("[Tagger] 接続失敗: worker が起動できません")
        self.status_bar.showMessage(
            "タガーエンジン: 利用不可 — models/wd14/model.onnx と selected_tags.csv を配置してください", 8000
        )
        # タガー不可でも③(サムネイル)は動ける。_reschedule()に判断させる。
        self._reschedule()

    def _on_tag_selected(self) -> None:
        """タグ > 選択中1枚をタグ付け"""
        if not self._tagger_engine or not self._tagger_engine.is_available:
            QMessageBox.warning(self, "タグ付け", "タガーエンジンが利用できません。")
            return

        path = getattr(self.thumbnail_grid.selected_item, "path", None)
        if not path:
            QMessageBox.information(self, "タグ付け", "画像を選択してください。")
            return

        image_id = None
        for img_id, p, _, _, _ in self.search_results:
            if p == path and img_id >= 0:
                image_id = img_id
                break

        if image_id is None:
            QMessageBox.warning(self, "タグ付け",
                                "DB登録済み画像のみタグ付けできます。\n"
                                "このフォルダをスキャン登録してください。")
            return

        self._start_tagging([(image_id, path)])

    def _on_tag_folder(self) -> None:
        """タグ > フォルダ全件タグ付け"""
        if not self._tagger_engine or not self._tagger_engine.is_available:
            QMessageBox.warning(self, "タグ付け", "タガーエンジンが利用できません。")
            return

        targets = [
            (img_id, path)
            for img_id, path, _, _, _ in self.search_results
            if img_id >= 0
        ]
        if not targets:
            QMessageBox.information(self, "タグ付け",
                                    "DB登録済み画像がありません。\n"
                                    "先にフォルダをスキャン登録してください。")
            return

        reply = QMessageBox.question(
            self, "フォルダ全件タグ付け",
            f"{len(targets)} 件の画像をタグ付けしますか？\n"
            "既存のタグは上書きされます。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._start_tagging(targets)

    def _start_tagging(
        self,
        targets: list[tuple],
        *,
        model: str | None = None,
        device: str | None = None,
        threshold: float | None = None,
        threshold_character: float | None = None,
        threshold_copyright: float | None = None,
    ) -> None:
        """
        TaggerWorker を起動してプログレスダイアログを表示する。

        model/device/threshold* を明示指定した場合、その回だけ QSettings の
        グローバル設定を無視してその値を使う（_on_retag_with_settings()
        からの「別設定で一回限りタグ付けし直す」用途）。個別設定の保存は
        行わない（保存不要という前提のため、呼び出し元が毎回明示的に渡す）。
        指定が無い項目（None）は従来通り QSettings から読む。
        """
        from PyQt6.QtWidgets import QProgressDialog
        from workers import TaggerWorker

        # タグ付けキューが再発生したのでアイドル解放タイマーをキャンセル
        self._cancel_tagger_idle_release()

        settings = QSettings("D-liner", "D-liner")
        if model is None:
            model = settings.value("tagger/model", "wd14")
        if device is None:
            device = settings.value("tagger/device", "NPU")
        if threshold is None:
            threshold = float(settings.value("tagger/threshold", 0.30))
        if threshold_character is None:
            threshold_character = float(settings.value("tagger/threshold_character", 0.75))
        if threshold_copyright is None:
            threshold_copyright = float(settings.value("tagger/threshold_copyright", 0.50))

        thresh = threshold
        t_char = threshold_character
        t_copy = threshold_copyright

        self._tag_progress_dlg = QProgressDialog(
            "タグ付け中...", "キャンセル", 0, len(targets), self
        )
        self._tag_progress_dlg.setWindowTitle("タグ付け")
        self._tag_progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._tag_progress_dlg.setMinimumDuration(0)
        self._tag_progress_dlg.setValue(0)

        self._tagger_worker = TaggerWorker(
            engine=self._tagger_engine,
            image_paths=targets,
            model=model,
            device=device,
            threshold=thresh,
            threshold_character=t_char,
            threshold_copyright=t_copy,
            parent=self,
        )
        self._tag_progress_dlg.canceled.connect(
            self._tagger_worker.requestInterruption
        )
        self._tagger_worker.progress.connect(self._on_tag_progress)
        self._tagger_worker.finished.connect(self._on_tag_finished)
        self._tagger_worker.error.connect(self._on_tag_error)
        self._tagger_worker.start()
        # セッション23再設計: 手動タグ付け(優先度0)が動き出したことを
        # _reschedule() に伝え、優先度1〜3を自動的に中断させる。
        # （self._tagger_worker.start()の後に呼ぶ必要がある。
        #   isRunning()で判定するため、startより前に呼ぶと
        #   「動いていない」と誤判定されてしまう）
        self._reschedule()

    def _on_tag_progress(self, current: int, total: int, path: str) -> None:
        if self._tag_progress_dlg:
            self._tag_progress_dlg.setValue(current)
            self._tag_progress_dlg.setLabelText(
                f"タグ付け中... ({current}/{total})\n{os.path.basename(path)}"
            )

    def _on_tag_finished(self, tagged: int, skipped: int) -> None:
        if self._tag_progress_dlg:
            self._tag_progress_dlg.close()
            self._tag_progress_dlg = None
        self._tagger_worker = None
        self.status_bar.showMessage(
            f"タグ付け完了: {tagged} 件成功 / {skipped} 件スキップ", 5000
        )
        # タグが更新されたので検索を再実行
        self.trigger_search()
        # セッション23再設計: 手動タグ付け完了 → 優先度0が下りたので
        # 優先度1〜3を再評価する（新しく処理された分、対象が減っている
        # 可能性もあるが、逆に見ていなかった間に増えている可能性も
        # あるため一律dirty化する）。
        self._mark_pipeline_dirty()
        # BG再開が実際にキューを見つけるまでの間、念のためアイドル解放を
        # 予約しておく（BG側でキューが見つかれば _launch_priority1/2 が
        # 即座にキャンセルする）
        self._schedule_tagger_idle_release()

    def _on_tag_error(self, message: str) -> None:
        if self._tag_progress_dlg:
            self._tag_progress_dlg.close()
            self._tag_progress_dlg = None
        self._tagger_worker = None
        QMessageBox.critical(self, "タグ付けエラー", message)
        # 手動タグ付け終了 → 優先度1〜3を再評価する
        self._mark_pipeline_dirty()
        self._schedule_tagger_idle_release()

    # ------------------------------------------------------------------
    # 複数画像への一括タグ追加/削除（LoRAトリガーワード等の用途）
    # ------------------------------------------------------------------

    def _on_bulk_tag_edit(self) -> None:
        """
        サムネイルグリッドで1件以上選択中ならその画像群、
        何も選択していなければ現在の絞り込み結果（search_results）
        全体を対象に、タグの一括追加/削除ダイアログを開く。
        """
        selected = getattr(self.thumbnail_grid, "selected_paths", None) or set()

        # バグ修正（指示書08監査時に発覚）: 未登録フォルダの画像は
        # FilesystemSearchWorker経由で id=-1（DB未登録のダミー値）として
        # search_results に入ってくる。_on_retag_with_settings()・
        # _on_lora_export() は既にこれを除外していたが、本メソッドだけ
        # 除外が漏れていた。除外しないと image_id=-1 が BulkTagWorker に
        # 渡り、tags.image_id の外部キー制約違反で操作全体がエラーになる。
        if len(selected) >= 1:
            path_to_id = {r[1]: r[0] for r in self.search_results}
            target_ids = [
                path_to_id[p] for p in selected
                if p in path_to_id and path_to_id[p] >= 0
            ]
            target_label = f"選択中の画像"
        else:
            target_ids = [r[0] for r in self.search_results if r[0] >= 0]
            target_label = "絞り込み結果全て"

        if not target_ids:
            QMessageBox.information(self, "タグの一括追加/削除", "対象画像がありません。")
            return

        dlg = _BulkTagDialog(len(target_ids), target_label, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        tags = dlg.tags()
        mode = dlg.mode()

        op_label = "追加" if mode == "add" else "削除"
        reply = QMessageBox.question(
            self,
            "一括タグ操作の確認",
            f"{target_label}（{len(target_ids)} 件）に対し、\n"
            f"タグ「{', '.join(tags)}」を一括{op_label}します。\n"
            f"よろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._start_bulk_tag(target_ids, tags, mode)

    def _start_bulk_tag(self, target_ids: list[int], tags: list[str], mode: str) -> None:
        """BulkTagWorker を起動してプログレスダイアログを表示する。"""
        from PyQt6.QtWidgets import QProgressDialog
        from workers import BulkTagWorker

        op_label = "追加" if mode == "add" else "削除"

        self._bulk_tag_progress_dlg = QProgressDialog(
            f"タグを一括{op_label}中...", "キャンセル", 0, len(target_ids), self
        )
        self._bulk_tag_progress_dlg.setWindowTitle("タグの一括追加/削除")
        self._bulk_tag_progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._bulk_tag_progress_dlg.setMinimumDuration(0)
        self._bulk_tag_progress_dlg.setValue(0)

        self._bulk_tag_worker = BulkTagWorker(
            target_ids=target_ids, tags=tags, mode=mode, parent=self
        )
        self._bulk_tag_progress_dlg.canceled.connect(
            self._bulk_tag_worker.requestInterruption
        )
        self._bulk_tag_worker.progress.connect(self._on_bulk_tag_progress)
        self._bulk_tag_worker.finished.connect(self._on_bulk_tag_finished)
        self._bulk_tag_worker.error.connect(self._on_bulk_tag_error)
        self._bulk_tag_worker.start()

    # ------------------------------------------------------------------
    # 選択画像/絞り込み結果を別設定(閾値・モデル)で一回限りタグ付けし直す
    # （実機確認A項目フィードバック対応。個別設定の保存は行わない）
    # ------------------------------------------------------------------

    def _on_retag_with_settings(self) -> None:
        """
        タグ > 選択中の画像を別設定でタグ付けし直す

        対象決定は _on_bulk_tag_edit() と同じパターン（選択中1件以上なら
        その画像群、無ければ絞り込み結果全体）。選ばれた model/threshold*
        はこの実行1回限りで、保存はしない（ユーザー判断: 個別設定の永続化
        は不要、自動タグ付け済みか否かだけを見ればよいため）。
        """
        if not self._tagger_engine or not self._tagger_engine.is_available:
            QMessageBox.warning(self, "タグ付け", "タガーエンジンが利用できません。")
            return

        selected = getattr(self.thumbnail_grid, "selected_paths", None) or set()
        if len(selected) >= 1:
            # バグ修正: 未登録フォルダの画像は FilesystemSearchWorker 経由で
            # image_id=-1（DB未登録のダミー値）として search_results に入って
            # くる。_on_bulk_tag_edit()・_on_lora_export() と同じ >= 0 ガードが
            # ここだけ漏れており、未登録フォルダの画像を選択して本機能を実行
            # すると image_id=-1 が TaggerWorker に渡り、DB書き込み時にエラー
            # になる可能性があった（else分岐の絞り込み結果全体側には元々
            # ガードがあったため、選択画像側のみの漏れだった）。
            path_to_id = {r[1]: r[0] for r in self.search_results}
            targets = [(path_to_id[p], p) for p in selected if p in path_to_id and path_to_id[p] >= 0]
            target_label = "選択中の画像"
        else:
            targets = [(r[0], r[1]) for r in self.search_results if r[0] >= 0]
            target_label = "絞り込み結果全て"

        if not targets:
            QMessageBox.information(self, "タグ付け", "DB登録済み画像がありません。")
            return

        chosen = self._prompt_retag_settings_dialog()
        if chosen is None:
            return

        reply = QMessageBox.question(
            self,
            "別設定でタグ付けし直す",
            f"{target_label}（{len(targets)} 件）を、\n"
            f"モデル「{chosen['model']}」・一般タグ閾値{chosen['threshold']:.2f}・"
            f"キャラクター閾値{chosen['threshold_character']:.2f}・"
            f"版権タグ閾値{chosen['threshold_copyright']:.2f}で\n"
            "タグ付けし直します。既存のAI由来タグは上書きされます\n"
            "（手動タグ・ロック状態は保護されます）。\n"
            "この設定は今回限りで、保存はされません。\n"
            "よろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._start_tagging(
            targets,
            model=chosen["model"],
            threshold=chosen["threshold"],
            threshold_character=chosen["threshold_character"],
            threshold_copyright=chosen["threshold_copyright"],
        )

    def _prompt_retag_settings_dialog(self) -> dict | None:
        """
        「別設定でタグ付けし直す」用の簡易ダイアログ。
        _on_tagger_settings() のグローバル設定ダイアログとは別物で、
        QSettings への保存は一切行わない（今回限りの値を返すだけ）。
        デバイス（NPU/GPU/CPU）はこの機能の想定用途（誤タグの閾値調整）
        とは直接関係が薄いため項目を設けず、現在のグローバル設定を
        そのまま使う。キャンセル時は None を返す。
        """
        from PyQt6.QtWidgets import QDialog, QFormLayout, QComboBox, QDoubleSpinBox, QDialogButtonBox

        settings = QSettings("D-liner", "D-liner")

        dlg = QDialog(self)
        dlg.setWindowTitle("別設定でタグ付けし直す")
        dlg.setMinimumWidth(360)
        layout = QFormLayout(dlg)

        model_combo = QComboBox()
        model_combo.addItems(["wd14", "camie", "joytag"])
        model_combo.setCurrentText(settings.value("tagger/model", "wd14"))
        layout.addRow("モデル:", model_combo)

        def _spin(val: float) -> QDoubleSpinBox:
            s = QDoubleSpinBox()
            s.setRange(0.0, 1.0)
            s.setSingleStep(0.05)
            s.setDecimals(2)
            s.setValue(val)
            return s

        spin_gen = _spin(float(settings.value("tagger/threshold", 0.30)))
        spin_char = _spin(float(settings.value("tagger/threshold_character", 0.75)))
        spin_copy = _spin(float(settings.value("tagger/threshold_copyright", 0.50)))
        layout.addRow("一般タグ閾値:", spin_gen)
        layout.addRow("キャラクター閾値:", spin_char)
        layout.addRow("版権タグ閾値:", spin_copy)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addRow(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None

        # 低閾値警告（v0.8で追加。以前はこのダイアログにだけ実装が漏れて
        # いた。詳細はモジュール冒頭の _warn_if_low_general_threshold() の
        # コメント参照）。
        _warn_if_low_general_threshold(self, spin_gen.value())

        return {
            "model": model_combo.currentText(),
            "threshold": spin_gen.value(),
            "threshold_character": spin_char.value(),
            "threshold_copyright": spin_copy.value(),
        }

    def _on_bulk_tag_progress(self, done: int, total: int) -> None:
        # バグ修正: QProgressDialog.setValue()は内部でprocessEvents()相当の
        # 処理を行うことがあり、そのタイミングでワーカーのfinishedシグナルが
        # 再入的に配送されて_on_bulk_tag_finished()が
        # self._bulk_tag_progress_dlgをNoneにしてしまうことがある
        # （実機クラッシュログで確認済み：if self._bulk_tag_progress_dlg:の
        # 判定は通過したのに、次の行でNoneになっていた）。
        # ローカル変数にキャッシュしてから使うことで、setValue()呼び出し中に
        # self._bulk_tag_progress_dlg が差し替わっても後続の呼び出しが
        # 安全になるようにする。
        dlg = self._bulk_tag_progress_dlg
        if dlg is None:
            return
        try:
            dlg.setValue(done)
            dlg.setLabelText(f"処理中... ({done}/{total})")
        except RuntimeError:
            # setValue()内の再入でダイアログのC++側が既に破棄されていた
            #場合の保険（Qtオブジェクトのライフタイムの問題）。
            pass

    def _on_bulk_tag_finished(self, affected: int, tag_count: int, mode: str) -> None:
        if self._bulk_tag_progress_dlg:
            self._bulk_tag_progress_dlg.close()
            self._bulk_tag_progress_dlg = None
        self._bulk_tag_worker = None
        op_label = "追加" if mode == "add" else "削除"
        self.status_bar.showMessage(
            f"タグの一括{op_label}が完了しました（{affected} 件 × タグ{tag_count}種）", 5000
        )
        # タグが更新されたので検索・タグ集計ペインを再描画。
        # バグ調査対応: このリフレッシュ経路は大量データでの実地検証が
        # 無かった新しいコードパスのため、万一ここで例外が起きても
        # プロセスごと落ちないよう保護する（sys.excepthookによる保険とは
        # 別に、ここでも直接捕捉してユーザーに状況を伝える）。
        try:
            self.trigger_search()
        except Exception as e:
            import traceback as _traceback
            _traceback.print_exc()
            QMessageBox.warning(
                self, "警告",
                f"タグの一括{op_label}は完了しましたが、画面の再描画中に"
                f"エラーが発生しました:\n{e}"
            )

    def _on_bulk_tag_error(self, message: str) -> None:
        if self._bulk_tag_progress_dlg:
            self._bulk_tag_progress_dlg.close()
            self._bulk_tag_progress_dlg = None
        self._bulk_tag_worker = None
        QMessageBox.critical(self, "一括タグ操作エラー", message)

    # ------------------------------------------------------------------
    # LoRA作成支援機構: エクスポート（セッション27）
    #
    # 選択画像（未選択なら絞り込み結果全体）を新規フォルダへ画像コピー＋
    # 同名.txtキャプションとしてエクスポートする。DB上のタグ・元画像は
    # 一切変更しない。対象決定は _on_bulk_tag_edit() と同じパターン。
    # 出力先フォルダは意図的に watched_folders へ登録しない
    # （未登録フォルダは自動タグ付け対象外という既存原則により、
    # AI再タグ付けの心配なくLoRA向けの整形ができるようにするため）。
    # ------------------------------------------------------------------

    def _on_lora_export(self) -> None:
        selected = getattr(self.thumbnail_grid, "selected_paths", None) or set()
        if len(selected) >= 1:
            path_to_id = {r[1]: r[0] for r in self.search_results}
            targets = [(path_to_id[p], p) for p in selected if p in path_to_id and path_to_id[p] >= 0]
            target_label = "選択中の画像"
        else:
            targets = [(r[0], r[1]) for r in self.search_results if r[0] >= 0]
            target_label = "絞り込み結果全て"

        if not targets:
            QMessageBox.information(self, "LoRA用にエクスポート", "DB登録済み画像がありません。")
            return

        dlg = LoraExportDialog(len(targets), target_label, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        dest_dir = dlg.destination_dir()
        caption_mode = dlg.caption_mode()
        if not dest_dir:
            return

        self._start_lora_export(targets, dest_dir, caption_mode)

    def _start_lora_export(
        self, targets: list[tuple[int, str]], dest_dir: str, caption_mode: str
    ) -> None:
        """LoraExportWorker を起動してプログレスダイアログを表示する。"""
        from PyQt6.QtWidgets import QProgressDialog
        from workers import LoraExportWorker

        self._lora_export_progress_dlg = QProgressDialog(
            "LoRA用にエクスポート中...", "キャンセル", 0, len(targets), self
        )
        self._lora_export_progress_dlg.setWindowTitle("LoRA用にエクスポート")
        self._lora_export_progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._lora_export_progress_dlg.setMinimumDuration(0)
        self._lora_export_progress_dlg.setValue(0)

        self._lora_export_worker = LoraExportWorker(
            targets=targets, dest_dir=dest_dir, caption_mode=caption_mode, parent=self
        )
        self._lora_export_progress_dlg.canceled.connect(
            self._lora_export_worker.requestInterruption
        )
        self._lora_export_worker.progress.connect(self._on_lora_export_progress)
        self._lora_export_worker.finished.connect(self._on_lora_export_finished)
        self._lora_export_worker.error.connect(self._on_lora_export_error)
        self._lora_export_worker.start()

    def _on_lora_export_progress(self, done: int, total: int) -> None:
        # バグ修正対応と同じ理由（_on_bulk_tag_progress()参照）:
        # QProgressDialog.setValue()の再入に備えてローカル変数にキャッシュする。
        dlg = self._lora_export_progress_dlg
        if dlg is None:
            return
        try:
            dlg.setValue(done)
            dlg.setLabelText(f"エクスポート中... ({done}/{total})")
        except RuntimeError:
            pass

    def _on_lora_export_finished(self, summary: dict) -> None:
        if self._lora_export_progress_dlg:
            self._lora_export_progress_dlg.close()
            self._lora_export_progress_dlg = None
        self._lora_export_worker = None

        copied = summary.get("copied", 0)
        renamed = summary.get("renamed", [])
        errors = summary.get("errors", [])
        empty_captions = summary.get("empty_captions", 0)

        self.status_bar.showMessage(f"LoRA用エクスポートが完了しました（{copied} 件）", 5000)

        msg = QMessageBox(self)
        msg.setWindowTitle("LoRA用にエクスポート")
        msg.setIcon(QMessageBox.Icon.Information if not errors else QMessageBox.Icon.Warning)
        text = f"{copied} 件のファイルをエクスポートしました。"
        if renamed:
            text += f"\nファイル名の衝突により {len(renamed)} 件の名前を変更しました。"
        if empty_captions:
            text += f"\n{empty_captions} 件は該当タグが無く、空のキャプションになりました。"
        if errors:
            text += f"\n{len(errors)} 件でエラーが発生しました。"
        msg.setText(text)

        detail_lines: list[str] = []
        if renamed:
            detail_lines.append("[名前を変更したファイル]")
            detail_lines.extend(f"  {orig} → {new}" for orig, new in renamed)
        if errors:
            if detail_lines:
                detail_lines.append("")
            detail_lines.append("[エラー]")
            detail_lines.extend(f"  {e}" for e in errors)
        if detail_lines:
            msg.setDetailedText("\n".join(detail_lines))

        msg.exec()

    def _on_lora_export_error(self, message: str) -> None:
        if self._lora_export_progress_dlg:
            self._lora_export_progress_dlg.close()
            self._lora_export_progress_dlg = None
        self._lora_export_worker = None
        QMessageBox.critical(self, "LoRA用エクスポートエラー", message)

    # ------------------------------------------------------------------
    # バックグラウンドタガー制御
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # タガーエンジン アイドル解放（イベント駆動、セッション10）
    # ------------------------------------------------------------------

    def _cancel_tagger_idle_release(self) -> None:
        """新しいタグ付けが始まるときに呼ぶ。保留中の解放タイマーを止める。"""
        if self._tagger_idle_timer is not None:
            if _TAGGER_IDLE_DEBUG:
                print("[TaggerIdle][DEBUG] cancel", flush=True)
            self._tagger_idle_timer.stop()
            self._tagger_idle_timer = None

    def _schedule_tagger_idle_release(self) -> None:
        """
        タグ付けキュー（手動・BG問わず）が空になったときに呼ぶ。
        60秒後に release_idle_sessions() を呼んでモデルセッションを解放する
        単発タイマーを仕掛ける。その間に新しいタグ付けが始まれば
        _cancel_tagger_idle_release() でキャンセルされる。
        """
        self._cancel_tagger_idle_release()
        if self._tagger_engine is None:
            return
        if _TAGGER_IDLE_DEBUG:
            print("[TaggerIdle][DEBUG] schedule (60s)", flush=True)
        self._tagger_idle_timer = QTimer(self)
        self._tagger_idle_timer.setSingleShot(True)
        self._tagger_idle_timer.timeout.connect(self._on_tagger_idle_timeout)
        self._tagger_idle_timer.start(60_000)

    def _on_tagger_idle_timeout(self) -> None:
        self._tagger_idle_timer = None
        if self._tagger_engine is None:
            return
        try:
            released = self._tagger_engine.release_idle_sessions()
            if released:
                print(f"[Tagger] アイドル解放: {released}", flush=True)
        except Exception as e:
            print(f"[Tagger] アイドル解放エラー: {e}", flush=True)

    def _launch_priority1(self) -> None:
        """
        優先度1: 現在フォルダ全体の未タグ付け画像をタグ付けする。

        セッション23再設計: 起動すべきかどうかの判断（現在フォルダが
        あるか、タガーが使えるか、下位を中断すべきか等）はすべて
        _reschedule() 側に一本化された。このメソッドは
        _reschedule() から「今まさに優先度1を起動する」と決まった
        場合にのみ呼ばれる前提で、ワーカーの生成・起動だけを行う。
        """
        self._p1_dirty = False

        # タグ付けキューが再発生したのでアイドル解放タイマーをキャンセル
        self._cancel_tagger_idle_release()

        settings = QSettings("D-liner", "D-liner")
        from workers import BackgroundTaggerWorker
        self._bg_tagger_worker = BackgroundTaggerWorker(
            engine               = self._tagger_engine,
            model                = settings.value("tagger/model",                "wd14"),
            device               = settings.value("tagger/device",               "NPU"),
            threshold            = float(settings.value("tagger/threshold",           0.30)),
            threshold_character  = float(settings.value("tagger/threshold_character", 0.75)),
            threshold_copyright  = float(settings.value("tagger/threshold_copyright", 0.50)),
            folder_path          = self.current_folder_path,
            recursive            = self._current_folder_recursive,
            scope                = "current",
            parent               = self,
        )
        self._bg_tagger_worker.progress.connect(self._on_bg_progress)
        self._bg_tagger_worker.finished.connect(self._on_bg_finished)
        self._bg_tagger_worker.error.connect(self._on_bg_error)
        self._bg_tagger_worker.queue_empty.connect(self._on_bg_queue_empty)
        self._bg_tagger_worker.start()
        # 最初の progress シグナルが来るまでの間も「アクティブ」を
        # 即座に反映しておく（対象0件ならすぐ queue_empty で上書きされる）。
        self._set_tag_status(_ICON_ACTIVE, "タグ 準備中...")
        print(f"[BGTagger] 開始（現在フォルダ: {self.current_folder_path}）", flush=True)

    @staticmethod
    def _path_scope_overlaps(a: str, b: str) -> bool:
        """
        2つのフォルダパスが同一か、一方がもう一方の祖先/子孫かを判定する。
        workers.py の _fetch_untagged() と同じ「正規化パス + '/'」の
        前方一致方式に揃えている。
        """
        na = a.replace("\\", "/").rstrip("/")
        nb = b.replace("\\", "/").rstrip("/")
        return na == nb or na.startswith(nb + "/") or nb.startswith(na + "/")

    def _on_folder_unwatched(self, path: str) -> None:
        """
        バグ修正(タスクA): フォルダの監視登録解除が、進行中のバックグラウンド
        タグ付け（②現在フォルダ / ④他フォルダアイドル時）に反映されて
        いなかった問題への対応。解除されたフォルダが該当ワーカーの
        対象範囲に含まれる場合のみ requestInterruption() を呼ぶ。

        scope='current': folder_path 配下が対象 → 解除パスと重なれば中断。
        scope='other'  : folder_path 配下"以外"が対象 → 解除パスが
                         folder_path と重ならない（＝対象範囲内）なら中断。
        """
        for worker in (self._bg_tagger_worker, self._idle_tagger_worker):
            if worker is None or not worker.isRunning():
                continue
            if not worker.folder_path:
                # folder_path未指定 = current: 対象なし(通常発生しない) /
                # other: DB全体が対象なので常に含まれる
                if worker.scope == "other":
                    worker.requestInterruption()
                continue
            overlaps = self._path_scope_overlaps(worker.folder_path, path)
            if worker.scope == "current":
                if overlaps:
                    worker.requestInterruption()
            else:  # "other"
                if not overlaps:
                    worker.requestInterruption()

    @staticmethod
    def _interrupt_worker(worker, tag: str) -> bool:
        """
        ワーカーが実行中なら requestInterruption() を出す。
        要求を出した場合のみ True を返す（呼び出し元がステータス表示更新の
        要否を判断できるように）。要求のみで完了は待たない。
        """
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            print(f"{tag} 中断要求", flush=True)
            return True
        return False

    def _mark_pipeline_dirty(self) -> None:
        """
        「新しい作業が発生したかもしれない」契機で呼ぶ
        （d_liner_handoff23.md 3-4節）。優先度1・2の再チェックフラグを
        立てた上で _reschedule() を呼ぶ。
        フォルダ切替確定時・フォルダ新規登録時・起動時同期/F5更新完了時・
        タグ付け設定変更時・タガーエンジン接続完了時・手動タグ付け開始/
        完了時のいずれかで呼ばれる想定。
        """
        self._p1_dirty = True
        self._p2_dirty = True
        self._p3_done_for = None
        self._reschedule()

    def _reschedule(self) -> None:
        """
        パイプライン全体の単一スケジューラ（優先度0〜3、
        d_liner_handoff23.md 3-4節）。

          優先度0: 手動タグ付け（TaggerWorker、ユーザー明示操作）
          優先度1: 現在フォルダのタグ付け（BackgroundTaggerWorker scope='current'）
          優先度2: 他フォルダのタグ付け（BackgroundTaggerWorker scope='other'）
          優先度3: 現在フォルダのサムネイル先読み（BackgroundThumbWorker）
          （優先度4: 他フォルダのサムネイル先読みは実装しない。
            セッション17の撤去判断を維持）

        上位が必要とする作業があれば、それより下位の実行中ワーカーには
        requestInterruption() を出す（要求のみ。完了は待たない — Qt上、
        中断要求から実際の停止までには画像1件分程度の遅延が残ることは
        単一スケジューラ化後も変わらない）。

        呼び出しは「状態が変わりうるイベント」からのみ行うこと:
        各ワーカーのfinished/error/queue_empty、フォルダ切替確定時、
        設定変更時、起動時同期・F5完了時、手動タグ付け開始/完了時、
        タガーエンジン接続完了時。progressシグナルからは絶対に呼ばない
        （優先度2の対象リスト再取得はDB全体スキャン相当のSQLのため、
        高頻度呼び出しは既存のI/O競合対策を無意味化する）。
        """
        # 優先度0: 手動タグ付け中は他の全てを中断するだけで、ここでは何も
        # 起動しない（速達性より安全・単純さを優先する、という合意に基づき、
        # 「誰が何を待っているか」を個別に追跡せず一律で止める）。
        manual_running = self._tagger_worker is not None and self._tagger_worker.isRunning()
        if manual_running:
            tag_interrupted = self._interrupt_worker(self._bg_tagger_worker, "[BGTagger]")
            idle_interrupted = self._interrupt_worker(self._idle_tagger_worker, "[IdleTagger]")
            thumb_interrupted = self._interrupt_worker(self._bg_thumb_worker, "[BGThumb]")
            if tag_interrupted or idle_interrupted:
                self._set_tag_status(_ICON_STANDBY, "一時停止中")
            if thumb_interrupted:
                self._set_thumb_status(_ICON_STANDBY, "一時停止中")
            return

        tagger_available = bool(self._tagger_engine and self._tagger_engine.is_available)
        p1_running = self._bg_tagger_worker is not None and self._bg_tagger_worker.isRunning()
        p2_running = self._idle_tagger_worker is not None and self._idle_tagger_worker.isRunning()

        if tagger_available and self.current_folder_path and (self._p1_dirty or p1_running):
            target = 1
        elif tagger_available and (self._p2_dirty or p2_running):
            target = 2
        else:
            target = 3

        # target より下位（数値が大きい＝優先度が低い）を中断する
        if target != 2:
            self._interrupt_worker(self._idle_tagger_worker, "[IdleTagger]")
        if target != 3:
            self._interrupt_worker(self._bg_thumb_worker, "[BGThumb]")

        if target == 1:
            if not p1_running:
                self._launch_priority1()
        elif target == 2:
            if not p2_running:
                self._launch_priority2()
        else:
            p3_running = self._bg_thumb_worker is not None and self._bg_thumb_worker.isRunning()
            cur_norm = (self.current_folder_path or "").replace("\\", "/").rstrip("/")
            already_done = (
                self._p3_done_for is not None
                and self._p3_done_for == cur_norm
            )
            if not p3_running and not already_done:
                self._try_start_priority3()

    def _apply_new_tagger_settings(self) -> None:
        """
        タグ付け設定（モデル/デバイス/閾値等）が変更された後に呼ぶ。

        セッション23再設計: 優先度1・2のどちらが実行中であっても、設定を
        読み直した新しいワーカーに差し替えるために明示的に中断要求を出す
        （通常の優先度による中断は「より優先度の高い作業のため」だが、
        これは同一優先度のまま設定だけ更新するための特別なケースなので
        _reschedule() の優先度ロジックとは別に、ここで直接扱う）。
        停止後は _mark_pipeline_dirty() が新設定で自動的に再始動する。
        """
        restarting = False
        if self._bg_tagger_worker is not None and self._bg_tagger_worker.isRunning():
            self._bg_tagger_worker.requestInterruption()
            restarting = True
        if self._idle_tagger_worker is not None and self._idle_tagger_worker.isRunning():
            self._idle_tagger_worker.requestInterruption()
            restarting = True
        if restarting:
            self.status_bar.showMessage(
                "タグ付け設定を変更しました。実行中のバックグラウンドタグ付けを"
                "停止して新しい設定で再開します...", 5000
            )
            print("[BGTagger] 設定変更のため中断要求 → 停止後に新設定で再始動します", flush=True)
        QTimer.singleShot(300, self._mark_pipeline_dirty)

    def _on_bg_progress(self, current: int, total: int, path: str) -> None:
        """BackgroundTaggerWorker の進捗をタグ付け状態欄に反映する（②＝アクティブ）。"""
        self._set_tag_status(_ICON_ACTIVE, f"タグ {current}/{total}", current, total)

    def _on_bg_finished(self, tagged: int, skipped: int) -> None:
        worker = self.sender()
        # バグ修正: BackgroundTaggerWorkerは中断されても queue_empty ではなく
        # finished(tagged, skipped) を発火する（対象が残っているにも
        # 関わらず完了扱いされてしまう）。isInterruptionRequested()で
        # 「打ち切りによる完了」かどうかを判定し、打ち切りの場合は
        # 優先度1を「済み」扱いにせず次のreschedule()で再試行させる。
        was_interrupted = bool(worker is not None and worker.isInterruptionRequested())
        self._bg_tagger_worker = None
        # セッション17: 完了メッセージを数秒表示して消す方式は廃止し、
        # 常時表示の3状態アイコンに統一した。ただし直前の処理件数が
        # 完全に見えなくなるのは不便なので、待機中テキストに
        # 「前回+N」として残す（次にアクティブ/バックグラウンドに
        # なれば自然に上書きされる）。
        self._set_tag_status(
            _ICON_STANDBY,
            f"待機中（前回+{tagged}）" if tagged > 0 else "待機中",
        )
        if tagged > 0:
            self.trigger_search()
        print(f"[BGTagger] 完了: tagged={tagged} skipped={skipped}"
              f"{'（中断により打ち切り）' if was_interrupted else ''}", flush=True)
        if was_interrupted:
            self._p1_dirty = True
        else:
            # キューが空になった（このバッチ分は完了）のでアイドル解放を予約
            self._schedule_tagger_idle_release()
        self._reschedule()

    def _on_bg_error(self, message: str) -> None:
        self._bg_tagger_worker = None
        self._set_tag_status(_ICON_STANDBY, "待機中")
        print(f"[BGTagger] エラー: {message}", flush=True)
        # エラー時は「対象が本当に無いのか」が確認できていないため、
        # 保守的に優先度1を再試行対象のままにしておく。
        self._p1_dirty = True
        self._schedule_tagger_idle_release()
        # バグ修正: ここで即座に_reschedule()を呼ぶと、DB接続断など
        # 即時に再発するエラーの場合タイトな再試行ループになりうる。
        # 次の外部契機（フォルダ切替・設定変更等）まで待つ（旧設計の
        # _bg_restart_pending方式でもエラー時の即時自動再試行は
        # 行っていなかった）。

    def _on_bg_queue_empty(self) -> None:
        self._bg_tagger_worker = None
        self._set_tag_status(_ICON_STANDBY, "待機中")
        print("[BGTagger] 未タグ付け画像なし", flush=True)
        # タグ付け対象がなくなったので60秒後にモデルセッションを解放する
        self._schedule_tagger_idle_release()
        self._reschedule()

    # ------------------------------------------------------------------
    # ④ 他フォルダのバックグラウンドタグ付け（アイドル時のみ・優先度2）
    # ------------------------------------------------------------------
    # セッション23再設計: 起動可否の判断（①②③が動いていないか等）は
    # _reschedule() に一本化された。_interrupt_idle_tagger_if_running()と
    # _check_idle_and_start_other_tagging()はこれに伴い撤去。

    def _launch_priority2(self) -> None:
        """
        優先度2: 現在フォルダ以外の未タグ付け画像をタグ付けする。
        _reschedule() 経由でのみ呼ぶこと。
        """
        self._p2_dirty = False
        settings = QSettings("D-liner", "D-liner")
        from workers import BackgroundTaggerWorker
        # バグ修正(instruction_tagger_idle_release_bug.md): ここでの
        # _cancel_tagger_idle_release() 呼び出しは削除。ワーカーを起動した
        # だけの時点ではまだ対象があるかどうか分からず、対象0件が続く限り
        # ここで毎回キャンセルしてしまうと、_schedule_tagger_idle_release()
        # で予約した60秒タイマーが一度も満了できず release_idle_sessions()
        # が永久に呼ばれなくなる（30秒間隔のバックオフ<60秒のIDLE_TIMEOUT
        # のため）。キャンセルは実際にモデルを使い始めた瞬間
        # （_on_idle_tagger_progress）に移した。
        self._idle_tagger_worker = BackgroundTaggerWorker(
            engine               = self._tagger_engine,
            model                = settings.value("tagger/model",                "wd14"),
            device               = settings.value("tagger/device",               "NPU"),
            threshold            = float(settings.value("tagger/threshold",           0.30)),
            threshold_character  = float(settings.value("tagger/threshold_character", 0.75)),
            threshold_copyright  = float(settings.value("tagger/threshold_copyright", 0.50)),
            folder_path          = self.current_folder_path or None,
            recursive            = True,
            scope                = "other",
            parent               = self,
        )
        self._idle_tagger_worker.progress.connect(self._on_idle_tagger_progress)
        self._idle_tagger_worker.finished.connect(self._on_idle_tagger_finished)
        self._idle_tagger_worker.error.connect(self._on_idle_tagger_error)
        self._idle_tagger_worker.queue_empty.connect(self._on_idle_tagger_queue_empty)
        self._idle_tagger_worker.start()
        print("[IdleTagger] 開始（他フォルダ・アイドル時）", flush=True)

    def _on_idle_tagger_progress(self, current: int, total: int, path: str) -> None:
        # バグ修正(instruction_tagger_idle_release_bug.md): 実際にタグ付け
        # 処理が進行した＝モデルを使い始めたタイミングで初めて保留中の
        # アイドル解放タイマーをキャンセルする（_launch_priority2
        # からここに移動）。
        self._cancel_tagger_idle_release()
        # v2指示書: ④は最低優先度。専用の進捗バーは持たず、既存の
        # タグ付けステータス欄をそのまま流用する（②が動いていない時にのみ
        # ④が動くため表示が競合することはない）。バックグラウンド🌙で表示。
        self._set_tag_status(_ICON_BACKGROUND, f"他フォルダ {current}/{total}", current, total)

    def _on_idle_tagger_finished(self, tagged: int, skipped: int) -> None:
        worker = self.sender()
        was_interrupted = bool(worker is not None and worker.isInterruptionRequested())
        self._idle_tagger_worker = None
        self._set_tag_status(
            _ICON_STANDBY,
            f"待機中（前回+{tagged}）" if tagged > 0 else "待機中",
        )
        print(f"[IdleTagger] 完了: tagged={tagged} skipped={skipped}"
              f"{'（中断により打ち切り）' if was_interrupted else ''}", flush=True)
        if tagged > 0:
            self.trigger_search()
        if was_interrupted:
            # バグ修正: 中断により未処理分が残っている可能性があるため、
            # 優先度2を「済み」扱いにせず次回のreschedule()で再試行させる。
            self._p2_dirty = True
        else:
            self._schedule_tagger_idle_release()
        self._reschedule()

    def _on_idle_tagger_error(self, message: str) -> None:
        self._idle_tagger_worker = None
        self._set_tag_status(_ICON_STANDBY, "待機中")
        print(f"[IdleTagger] エラー: {message}", flush=True)
        self._p2_dirty = True
        self._schedule_tagger_idle_release()
        # _on_bg_error と同じ理由で、ここでは即座に_reschedule()を呼ばない
        # （即時に再発するエラーでのタイトな再試行ループを避けるため）。

    def _on_idle_tagger_queue_empty(self) -> None:
        self._idle_tagger_worker = None
        self._set_tag_status(_ICON_STANDBY, "待機中")
        print("[IdleTagger] 他フォルダに未タグ付け画像なし", flush=True)
        self._schedule_tagger_idle_release()
        self._reschedule()

    def _on_rebalance_timer(self) -> None:
        """
        30秒ごとに呼ばれ、piggyback ↔ standalone を自動切り替えする。

        standalone 中: ComfyUI Worker が起動してきたら piggyback に昇格
        piggyback 中:  ComfyUI Worker が落ちたら standalone に降格・再起動
        """
        if self._tagger_engine is None:
            return
        # BGタグ付け中は切り替えしない（_lock はエンジン内で制御するが
        # UI側でも不要な割り込みを避ける）
        new_mode = self._tagger_engine.check_and_rebalance()
        if new_mode is None:
            return  # 変化なし

        prev_mode = "piggyback" if new_mode == "standalone" else "standalone"
        print(f"[Tagger] モード切り替え: {prev_mode} → {new_mode}", flush=True)

        if new_mode == "piggyback":
            self.status_bar.showMessage("タガー: ComfyUI Worker に切り替えました [piggyback]", 5000)
        elif new_mode == "standalone":
            self.status_bar.showMessage("タガー: ComfyUI Worker 終了 → D-liner Worker を再起動しました", 5000)
        elif new_mode == "unavailable":
            self.status_bar.showMessage("タガー: Worker の再起動に失敗しました", 8000)
            self.action_tag_one.setEnabled(False)
            self.action_tag_folder.setEnabled(False)
            self._rebalance_timer.stop()

    # ------------------------------------------------------------------
    # バックグラウンドサムネイル生成
    # ------------------------------------------------------------------

    def _try_start_priority3(self) -> None:
        """
        優先度3: 現在フォルダの見えない範囲のサムネイルを先読みする。
        _reschedule() 経由でのみ呼ぶこと。
        """
        if self._thumb_cache is None:
            return
        if not self.current_folder_path:
            return
        if self._bg_thumb_worker is not None and self._bg_thumb_worker.isRunning():
            return

        from workers import BackgroundThumbWorker
        from thumbnail_grid import ThumbnailGridWidget
        thumb_size = ThumbnailGridWidget.THUMB_SIZES[ThumbnailGridWidget._thumb_size_idx]

        # バグ修正: folder_is_registered を見ずに無条件でpathsを渡していたため、
        # DB登録済みフォルダでもタグ検索フィルタ中の search_results に
        # 先読み対象がすり替わり、フォルダ全体の先読み（_fetch_active_paths
        # によるDB全件スコープ）が意図せず縮小する回帰があった（これは対応済み）。
        #
        # 追加のバグ修正: 「DB未登録」の条件だけでは、意図的にクイックアクセス
        # 登録したフォルダと、一度も触れていない無関係な通りがかりのフォルダを
        # 区別できず、後者にまでサムネイル先読みが及んでいた（ツリーで
        # クリックしただけの任意のフォルダが際限なくデコードされてしまう）。
        # pathsは「クイックアクセス登録済み」の場合のみ使う。
        # それ以外の未登録フォルダは paths=None のままとし、
        # BackgroundThumbWorker側のDBフェッチが空振り（queue_empty）に
        # なるだけで済ませる（デコード無し・仕様として据え置き）。
        paths = None
        if not self.folder_is_registered and self._is_quick_access_folder(self.current_folder_path):
            paths = [r[1] for r in self.search_results] if self.search_results else None

        self._bg_thumb_worker = BackgroundThumbWorker(
            thumb_cache = self._thumb_cache,
            thumb_size  = thumb_size,
            folder_path = self.current_folder_path,
            recursive   = self._current_folder_recursive,
            parent      = self,
            paths       = paths,
        )
        self._bg_thumb_worker.progress.connect(self._on_bg_thumb_progress)
        self._bg_thumb_worker.finished.connect(self._on_bg_thumb_finished)
        self._bg_thumb_worker.queue_empty.connect(self._on_bg_thumb_queue_empty)
        self._bg_thumb_worker.interrupted.connect(self._on_bg_thumb_interrupted)
        self._bg_thumb_worker.error.connect(self._on_bg_thumb_error)
        self._bg_thumb_worker.start()
        # 最初の progress シグナルが来るまでの間もアクティブを反映しておく。
        self._set_thumb_status(_ICON_ACTIVE, "サムネ 準備中...")
        print(f"[BGThumb] 開始（現在フォルダ: {self.current_folder_path}）", flush=True)

    def _on_bg_thumb_progress(self, current: int, total: int, path: str) -> None:
        self._set_thumb_status(_ICON_ACTIVE, f"サムネ {current}/{total}", current, total)

    def _on_bg_thumb_finished(self, generated: int, skipped: int) -> None:
        worker = self.sender()
        # セッション23再設計: 世代カウンタとの比較（タイミングずれの原因、
        # d_liner_handoff23.md参照）をやめ、そのワーカーの対象フォルダと
        # 現在フォルダを直接比較する方式に統一した。
        worker_folder = (getattr(worker, "folder_path", "") or "").replace("\\", "/").rstrip("/")
        cur_folder = (self.current_folder_path or "").replace("\\", "/").rstrip("/")
        is_stale = worker_folder != cur_folder
        self._bg_thumb_worker = None
        # セッション17: 完了メッセージを数秒表示して消す方式は廃止し、
        # 常時表示の3状態アイコンに統一した。直前の生成件数は待機中
        # テキストに「前回+N」として残す（フォルダ切替済みの旧バッチ分は
        # 今の画面と無関係なので件数表示に含めない）。
        show_count = generated > 0 and not is_stale
        self._set_thumb_status(
            _ICON_STANDBY,
            f"待機中（前回+{generated}）" if show_count else "待機中",
        )
        if show_count:
            # グリッドを再描画してキャッシュヒットさせる
            self.trigger_search()
        print(f"[BGThumb] 完了: generated={generated} skipped={skipped}"
              f"{' (旧フォルダ分・表示更新スキップ)' if is_stale else ''}", flush=True)
        # バグ修正: is_staleでない（＝今の現在フォルダに対する完了）場合、
        # このフォルダはもう確認済みとして記録する。これが無いと
        # _reschedule() が他に優先度1・2の仕事が無い間、完了直後に
        # 何度でも優先度3を即時再起動してしまう（無限ループ・I/O負荷過多）。
        if not is_stale:
            self._p3_done_for = worker_folder
        self._reschedule()

    def _on_bg_thumb_interrupted(self) -> None:
        """
        対象リスト構築中の中断（主にフォルダ切替）。
        finished(0,0)と違い「未処理分が残っている」状態なので、
        完了メッセージ等は一切出さずワーカー参照をクリアするのみ。
        """
        self._bg_thumb_worker = None
        self._set_thumb_status(_ICON_STANDBY, "待機中")
        print("[BGThumb] 中断（フォルダ切替等）", flush=True)
        self._reschedule()

    def _on_bg_thumb_queue_empty(self) -> None:
        worker = self.sender()
        worker_folder = (getattr(worker, "folder_path", "") or "").replace("\\", "/").rstrip("/")
        cur_folder = (self.current_folder_path or "").replace("\\", "/").rstrip("/")
        self._bg_thumb_worker = None
        self._set_thumb_status(_ICON_STANDBY, "待機中")
        print("[BGThumb] 未キャッシュ画像なし", flush=True)
        # バグ修正: このフォルダに対して「確認済み・対象なし」を記録する。
        # これが抜けていたため、他に優先度1・2の仕事が無い限り
        # _reschedule() が queue_empty 直後に何度でも優先度3を即時再起動
        # し続けていた（実機ログで確認された無限ループ・I/O負荷過多の
        # 直接原因）。フォルダが既に切り替わっていた場合(worker_folderが
        # 現在フォルダと不一致)は記録しない（次のreschedule()で
        # 現在フォルダに対して正しく再評価させるため）。
        if worker_folder == cur_folder:
            self._p3_done_for = worker_folder
        self._reschedule()

    def _on_bg_thumb_error(self, message: str) -> None:
        self._bg_thumb_worker = None
        self._set_thumb_status(_ICON_STANDBY, "待機中")
        print(f"[BGThumb] エラー: {message}", flush=True)
        # バグ修正: ここで即座に_reschedule()を呼ぶと、キャッシュDB破損等の
        # 即時に再発するエラーの場合タイトな再試行ループになりうる
        # （②④のエラーハンドラと同じ理由で、次の外部契機まで待つ）。

    def _set_thumb_status(
        self, icon: str, text: str,
        current: int | None = None, total: int | None = None,
    ) -> None:
        """
        サムネイル状態欄（③用）を更新する。icon は _ICON_ACTIVE
        （今のフォルダの処理中）/ _ICON_STANDBY（何もしていない）の
        いずれか。current/total を渡すとプログレスバーも表示する。
        """
        self._bg_thumb_label.setText(f"{icon} {text}")
        if total is not None and total > 0:
            self._bg_thumb_bar.setVisible(True)
            self._bg_thumb_bar.setMaximum(total)
            self._bg_thumb_bar.setValue(current or 0)
        else:
            self._bg_thumb_bar.setVisible(False)

    def _set_tag_status(
        self, icon: str, text: str,
        current: int | None = None, total: int | None = None,
    ) -> None:
        """
        タグ付け状態欄（②④共通）を更新する。icon は _ICON_ACTIVE
        （②＝今のフォルダ）/ _ICON_BACKGROUND（④＝アイドル時他フォルダ）/
        _ICON_STANDBY（何もしていない）のいずれか。
        current/total を渡すとプログレスバーも表示する。
        """
        self._bg_tag_label.setText(f"{icon} {text}")
        if total is not None and total > 0:
            self._bg_tag_bar.setVisible(True)
            self._bg_tag_bar.setMaximum(total)
            self._bg_tag_bar.setValue(current or 0)
        else:
            self._bg_tag_bar.setVisible(False)

    def _on_tagger_settings(self) -> None:
        """タグ > タグ付け設定 ダイアログ"""
        from PyQt6.QtWidgets import (
            QDialog, QFormLayout, QLineEdit, QComboBox,
            QDoubleSpinBox, QDialogButtonBox, QFileDialog,
        )

        settings = QSettings("D-liner", "D-liner")

        dlg = QDialog(self)
        dlg.setWindowTitle("タグ付け設定")
        dlg.setMinimumWidth(380)
        layout = QFormLayout(dlg)

        model_combo = QComboBox()
        # anima（pixai-tagger）はモノクロ画像でハルシネーションを起こす既知の
        # 不具合があり、合議制前提のモデルのため選択肢から除外している
        # （tagger_engine.py StandaloneTaggerBackend._SUPPORTED_MODELS 参照）。
        # セッション10: camie / joytag を実装。wd14 は版権キャラクターに
        # 弱い（学習データが古い）ため、より新しいタガーへの切り替えとして
        # 追加した。モデルファイル未配置時は初回タグ付け実行時に
        # HuggingFace から自動ダウンロードされる。
        model_combo.addItems(["wd14", "camie", "joytag"])
        _model_before_dialog = settings.value("tagger/model", "wd14")
        model_combo.setCurrentText(_model_before_dialog)
        layout.addRow("モデル:", model_combo)

        # GPUリストを取得
        from tagger_engine import get_gpu_names, is_npu_capable
        gpus = get_gpu_names()

        device_combo = QComboBox()
        # セッション10: "GPU" を追加。DirectML系統venv（NPU非搭載機向けに
        # setup_runtime_env.py --runtime auto/directml で構築）では NPU も
        # OpenVINO GPU プラグインも使えないため、GPU を明示選択することで
        # tagger_engine.py の _create_session() が DirectML GPU に直接
        # フォールバックする（NPU選択のままでも最終的にDirectMLへ
        # フォールバックはするが、ログ上「NPU要求→失敗→失敗→成功」の
        # ノイズが出るため、GPU環境では明示的にGPUを選ぶ方が分かりやすい）。
        #
        # セッション18 追記: NPU推論はOpenVINO ExecutionProvider経由でのみ
        # 成立する。onnxruntime-directml系統でセットアップされた
        # venv（NPU非搭載機向け）には onnxruntime-openvino が無く、NPUを
        # 選んでも常に失敗してフォールバックするだけの意味の無い選択肢に
        # なるため、is_npu_capable() が False の環境ではそもそも選択肢から
        # 除外する。
        device_options = ["GPU", "CPU"]
        npu_available = is_npu_capable()
        if npu_available:
            device_options.insert(0, "NPU")
        device_combo.addItems(device_options)

        saved_device = settings.value("tagger/device", "NPU" if npu_available else "GPU")
        if saved_device not in device_options:
            # 以前NPU搭載機で選んだ設定のままNPU非搭載機のvenvへ
            # 移行した場合等、選択肢に存在しない値が保存されているケースの救済
            saved_device = device_options[0]
        device_combo.setCurrentText(saved_device)
        layout.addRow("デバイス:", device_combo)

        # GPUが複数ある場合の使用GPU選択用コンボボックス
        gpu_combo = QComboBox()
        for idx, name in enumerate(gpus):
            gpu_combo.addItem(f"#{idx}: {name}", idx)
        
        saved_gpu_id = 0
        try:
            saved_gpu_id = int(settings.value("tagger/gpu_device_id", 0))
        except (ValueError, TypeError):
            saved_gpu_id = 0
        if saved_gpu_id >= len(gpus):
            saved_gpu_id = 0
            
        if gpus:
            gpu_combo.setCurrentIndex(saved_gpu_id)
            
        layout.addRow("使用GPU:", gpu_combo)

        def update_gpu_visibility():
            is_gpu = device_combo.currentText() == "GPU"
            has_multiple_gpus = len(gpus) > 1
            gpu_combo.setVisible(is_gpu and has_multiple_gpus)
            label = layout.labelForField(gpu_combo)
            if label:
                label.setVisible(is_gpu and has_multiple_gpus)

        device_combo.currentTextChanged.connect(update_gpu_visibility)
        update_gpu_visibility()

        def _spin(val: float) -> QDoubleSpinBox:
            s = QDoubleSpinBox()
            s.setRange(0.0, 1.0)
            s.setSingleStep(0.05)
            s.setDecimals(2)
            s.setValue(val)
            return s

        _thresh_gen_before  = float(settings.value("tagger/threshold",           0.30))
        _thresh_char_before = float(settings.value("tagger/threshold_character", 0.75))
        _thresh_copy_before = float(settings.value("tagger/threshold_copyright", 0.50))

        spin_gen  = _spin(_thresh_gen_before)
        spin_char = _spin(_thresh_char_before)
        spin_copy = _spin(_thresh_copy_before)
        layout.addRow("一般タグ閾値:", spin_gen)
        layout.addRow("キャラクター閾値:", spin_char)
        layout.addRow("版権タグ閾値:", spin_copy)

        # セッション18 追記: worker.py パス / PID ファイルパスは ComfyUI
        # piggyback 連携（現在凍結中、tagger_engine.py 側で未使用）専用の
        # 設定項目で、非ComfyUIユーザーには不要かつ紛らわしいため撤去した。
        # 代わりに「モデルフォルダの手動指定」を追加する。自動ダウンロードが
        # 何らかの事情（社内ポリシー等でHuggingFaceへのアクセスがブロック
        # されている場合等）で機能しないケースでも、手動でダウンロードした
        # model.onnx + タグ定義ファイルを任意のフォルダに置いてここで指定
        # すれば救済できる。モデルごとに別フォルダを指定できるよう、
        # QSettings["tagger/model_dir/{model_id}"] として保存する
        # （tagger_engine.py _resolve_model_path()/_resolve_tags_path() 参照）。
        _model_dir_key = lambda mid: f"tagger/model_dir/{mid}"
        _model_dir_pending: dict[str, str] = {}
        _model_dir_shown = {"id": _model_before_dialog}

        model_dir_row = QHBoxLayout()
        model_dir_edit = QLineEdit(settings.value(_model_dir_key(_model_before_dialog), ""))
        model_dir_edit.setPlaceholderText(
            "空欄 = 自動探索/自動ダウンロード（model.onnxのあるフォルダ、"
            "または複数モデル共通の親フォルダ(例: E:/models)のどちらでも可）"
        )
        model_dir_row.addWidget(model_dir_edit, 1)
        model_dir_browse_btn = QPushButton("...")
        model_dir_browse_btn.setFixedWidth(32)
        model_dir_browse_btn.setToolTip("フォルダを参照")
        model_dir_row.addWidget(model_dir_browse_btn)
        layout.addRow("モデルフォルダ(手動指定):", model_dir_row)

        def _on_browse_model_dir() -> None:
            start = model_dir_edit.text().strip() or str(Path(__file__).parent / "models")
            d = QFileDialog.getExistingDirectory(dlg, "モデルフォルダを選択", start)
            if d:
                model_dir_edit.setText(d)

        model_dir_browse_btn.clicked.connect(_on_browse_model_dir)

        def _sync_model_dir_field(new_model_id: str) -> None:
            # モデル切り替え時、直前のモデル分の入力内容を保持してから
            # 切り替え先モデルの設定値（またはpending中の未保存値）を表示する。
            prev_id = _model_dir_shown["id"]
            _model_dir_pending[prev_id] = model_dir_edit.text().strip()
            _model_dir_shown["id"] = new_model_id
            model_dir_edit.setText(
                _model_dir_pending.get(new_model_id, settings.value(_model_dir_key(new_model_id), ""))
            )

        model_combo.currentTextChanged.connect(_sync_model_dir_field)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addRow(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        settings.setValue("tagger/model",                model_combo.currentText())
        settings.setValue("tagger/device",               device_combo.currentText())
        if len(gpus) > 1:
            settings.setValue("tagger/gpu_device_id",     gpu_combo.currentData())
        else:
            settings.setValue("tagger/gpu_device_id",     0)
        settings.setValue("tagger/threshold",            spin_gen.value())
        settings.setValue("tagger/threshold_character",  spin_char.value())
        settings.setValue("tagger/threshold_copyright",  spin_copy.value())

        # 現在表示中のモデル分もpendingに反映してから、触れたモデル全ての
        # 手動フォルダ指定を保存する。
        _model_dir_pending[model_combo.currentText()] = model_dir_edit.text().strip()
        for _mid, _folder in _model_dir_pending.items():
            settings.setValue(_model_dir_key(_mid), _folder)

        # 極端な低閾値についての注意（v0.8で共通ヘルパー化。詳細は
        # モジュール冒頭の _warn_if_low_general_threshold() のコメント参照）。
        _warn_if_low_general_threshold(self, spin_gen.value())

        # バグ修正: 以前はここで無条件に _apply_new_tagger_settings() を
        # 呼んでいたため、直後の一貫性警告ダイアログ（モーダル）が開いている
        # 間にQTimerが発火し、ユーザーが「クリアするか」を答える前に新設定
        # でのBGタグ付けが始まってしまっていた（クリアを選ぶとその分のタグが
        # 無駄になる二重処理）。呼び出しをこの下のブロックに一本化し、
        # ダイアログのユーザー選択が確定してから1回だけ呼ぶようにする。

        # --- モデル/閾値変更時の一貫性警告（セッション9 追加、セッション12 拡張） ---
        # タグ付けモデルまたは閾値を変更すると、既存のタグ付け済み画像との
        # 一貫性が崩れる可能性があるため通知する。実際に再タグ付けするかは
        # ユーザー判断に委ねる（このダイアログは警告のみで強制はしない）。
        #
        # セッション12: 以前はモデル変更時のみ警告していたが、閾値
        # （一般/キャラクター/版権）を変えても付与されるタグの範囲が
        # 変わる＝一貫性が崩れる点はモデル変更と同じであるため、
        # 閾値変更時にも同じ警告を出すよう拡張した。
        #
        # セッション10: 「タグキャッシュをクリア」ボタンを追加。既存の全タグ
        # データ（tags テーブル全件）を削除し、BackgroundTaggerWorker が
        # 「未タグ付け画像」として拾い直せる状態にする。BackgroundTaggerWorker
        # は tags テーブルに行が無い画像だけを対象にするため、これだけで
        # 新設定による再タグ付けが自動的に始まる。
        new_model = model_combo.currentText()
        model_changed = new_model != _model_before_dialog
        threshold_changed = (
            abs(spin_gen.value()  - _thresh_gen_before)  > 1e-9 or
            abs(spin_char.value() - _thresh_char_before) > 1e-9 or
            abs(spin_copy.value() - _thresh_copy_before) > 1e-9
        )

        if model_changed or threshold_changed:
            changes: list[str] = []
            if model_changed:
                changes.append(f"モデル: 「{_model_before_dialog}」→「{new_model}」")
            if threshold_changed:
                changes.append(
                    f"一般タグ閾値: {_thresh_gen_before:.2f} → {spin_gen.value():.2f}\n"
                    f"キャラクター閾値: {_thresh_char_before:.2f} → {spin_char.value():.2f}\n"
                    f"版権タグ閾値: {_thresh_copy_before:.2f} → {spin_copy.value():.2f}"
                )
            change_text = "\n".join(changes)

            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Icon.Information)
            msg.setWindowTitle("タグ付け設定が変更されました")
            msg.setText(
                f"タグ付け設定を変更しました。\n\n{change_text}\n\n"
                f"モデルや閾値が異なると同じ画像でもタグ付け結果が変わるため、"
                f"既存のタグ付け済み画像との一貫性が崩れる可能性があります。\n\n"
                f"必要に応じて、対象画像のタグ付けをやり直してください。\n"
                f"「タグキャッシュをクリア」を押すと、既存のAI由来のタグ付け結果を"
                f"削除し、新しい設定での自動再タグ付けを開始します"
                f"（手動追加したタグは保持されます）。"
            )
            close_btn = msg.addButton("閉じる", QMessageBox.ButtonRole.RejectRole)
            clear_btn = msg.addButton("タグキャッシュをクリア", QMessageBox.ButtonRole.DestructiveRole)
            msg.setDefaultButton(close_btn)
            msg.exec()

            if msg.clickedButton() is clear_btn:
                # _clear_tag_cache_and_retag() が内部で
                # _apply_new_tagger_settings() を呼び再始動するため、
                # ここでは呼ばない（二重呼び出し防止）。
                self._clear_tag_cache_and_retag()
            else:
                # クリアしない場合でも、新設定を反映するため1回だけ再始動する。
                self._apply_new_tagger_settings()
        else:
            # モデル/閾値変更なし（デバイス変更等のみ）→ 警告ダイアログなしで
            # 即座に新設定を反映する。
            self._apply_new_tagger_settings()

    def _clear_tag_cache_and_retag(self) -> None:
        """
        既存のAI由来タグ付け結果（tags テーブルのうち category != 'manual'）を
        削除し、BackgroundTaggerWorker による新モデルでの再タグ付けを開始する。
        手動追加タグ（category = 'manual'）は保護対象として削除しない
        （指示書02 タスクA-1）。破壊的操作のため実行前に確認ダイアログを挟む。
        """
        conn = lifecycle_manager.get_connection()
        cursor = conn.cursor()
        # 件数は実際に削除される画像（manual以外のタグを持つ画像）のみを
        # 対象に数える。manualタグしか無い画像を含めてしまうと、実際には
        # 削除されないのに件数だけ膨らむ表示不整合になるため。
        cursor.execute(
            "SELECT COUNT(DISTINCT image_id) FROM tags WHERE category != 'manual'"
        )
        count = cursor.fetchone()[0]

        if count == 0:
            conn.close()
            self.status_bar.showMessage("タグ付け済み画像がないため、クリアの必要はありません。", 4000)
            return

        reply = QMessageBox.question(
            self,
            "タグキャッシュのクリア確認",
            f"{count} 件の画像に紐づく既存のAI由来タグ付け結果をすべて削除します。\n"
            f"手動追加したタグ（manualカテゴリ）は削除されません。\n"
            f"この操作は取り消せません。よろしいですか？\n\n"
            f"削除後、新しいモデルでバックグラウンドタグ付けが自動的に再実行されます。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            conn.close()
            return

        cursor.execute("DELETE FROM tags WHERE category != 'manual'")
        # 方針転換: ai_tagging_suppressed は「AI由来タグ全削除で自動的に
        # 立つフラグ」から「ロックボタンで明示的にON/OFFする状態」に変更
        # した（sdi_window_viewer.py SDIWindow のロックアイコン参照）。
        # 自動フラグだった頃は全クリア時に一括リセットしていたが、
        # 明示的にロックした画像を全クリアのたびに黙って解錠するのは
        # ユーザーの意図（この画像はAIに触らせたくない）に反するため、
        # ここでは触れない。ロック解除は引き続きロックボタンからのみ行う。
        conn.commit()
        conn.close()

        self.status_bar.showMessage(
            f"タグキャッシュをクリアしました（{count} 件）。バックグラウンドタグ付けを再開します。",
            6000,
        )
        # タグが消えたので表示を更新
        self.trigger_search()
        # クリア後、未タグ付け画像として拾われるようBGタガーを起動
        # （実行中のワーカーがあれば割り込んで停止後に再始動する）
        self._apply_new_tagger_settings()

    # ------------------------------------------------------------------
    # デバッグウィンドウ
    # ------------------------------------------------------------------
    def _show_debug_window(self) -> None:
        """デバッグウィンドウを開く（既に開いていれば前面に出す）"""
        if not hasattr(self, "_debug_win") or self._debug_win is None:
            self._debug_win = DebugWindow(parent=self)
        self._debug_win.show()
        self._debug_win.raise_()
        self._debug_win.activateWindow()

    def closeEvent(self, event) -> None:
        # タガーWorkerが動いていればキャンセル
        if self._tagger_worker is not None and self._tagger_worker.isRunning():
            self._tagger_worker.requestInterruption()
            self._tagger_worker.quit()
        # 接続ワーカーが動いていれば終了を待つ
        if hasattr(self, "_tagger_connect_worker") and self._tagger_connect_worker.isRunning():
            self._tagger_connect_worker.quit()
            self._tagger_connect_worker.wait(3000)
        # BGワーカーを停止してキャッシュ書き込み完了を待つ
        # （従来 _idle_tagger_worker がこのリストに含まれておらず、
        # 終了時にアイドル系ワーカーが動いたままになりうる抜けが
        # あったため、④も対象に追加した）
        for worker in [
            self._bg_thumb_worker,
            self._bg_tagger_worker,
            self._idle_tagger_worker,
        ]:
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.quit()
                worker.wait(3000)
        # タガーエンジンをシャットダウン
        if self._tagger_engine is not None:
            self._tagger_engine.shutdown()
        # サムネイルキャッシュ: アクセス日時UPDATE + 自動清掃 + DB閉じる
        if self._thumb_cache is not None:
            self._thumb_cache.close()
        self.close_all_sdi_windows()
        self.save_settings()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# デバッグウィンドウ
# ---------------------------------------------------------------------------

class _DebugSignalEmitter(QObject):
    """
    セッション18 追記（検証機クラッシュ対応 — 修正1 再実装）:
    _DebugStream.write() は BackgroundTaggerWorker 等の QThread から
    print() 経由で呼ばれ得るが、以前の実装ではそこから直接
    QPlainTextEdit.appendPlainText()（GUI部品）を呼んでいた。
    Qtでは非GUIスレッドからのGUI部品直接操作は未定義動作であり、
    検証機でモデル自動ダウンロード完了直後（＝ダウンロード完了ログの
    print() が BackgroundTaggerWorker 側スレッドで発火する瞬間）に
    メインウィンドウごとクラッシュする現象の直接原因と判明した。

    対策: GUI部品には一切触れず、シグナル発行のみ行う。
    Qtのシグナル/スロットは送受信のスレッドアフィニティが異なる場合、
    自動的にQueuedConnectionとして扱われるため、実際の
    appendPlainText() 呼び出しは受信側（DebugWindow、GUIスレッド）
    でのみ安全に実行される。
    """
    log_emitted = pyqtSignal(str)


class _DebugStream:
    """sys.stdout / sys.stderr を QPlainTextEdit に転送するストリームラッパー"""
    def __init__(self, emitter: "_DebugSignalEmitter", original) -> None:
        self._emitter = emitter
        self._original = original  # 元のストリームにも流す

    def write(self, text: str) -> None:
        if self._original:
            self._original.write(text)
        if text:
            # GUI部品(QPlainTextEdit)には直接触れず、シグナル発行のみ。
            # どのスレッドから呼ばれても安全（上記_DebugSignalEmitter参照）。
            self._emitter.log_emitted.emit(text)

    def flush(self) -> None:
        if self._original:
            self._original.flush()

    def fileno(self):
        """subprocess 等が fileno を要求した場合は元ストリームに委譲"""
        if self._original:
            return self._original.fileno()
        raise OSError("no fileno")


class DebugWindow(QDialog):
    """
    print() / stderr の出力をリアルタイムで表示するデバッグウィンドウ。
    開いている間だけ sys.stdout / sys.stderr をフック、閉じると元に戻す。
    """
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("D-liner — デバッグ出力")
        self.resize(800, 400)
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowType.WindowMinMaxButtonsHint
        )

        self._orig_stdout = None
        self._orig_stderr = None

        # メインスレッド（self自身と同じスレッド）で生成することで、
        # 他スレッドからのシグナル発行がQueuedConnectionとして
        # 安全にGUIスレッドへ配送されるようにする。
        self._emitter = _DebugSignalEmitter(self)
        self._emitter.log_emitted.connect(self._append_text)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._text = QPlainTextEdit(self)
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(2000)  # 行数上限（メモリ節約）
        self._text.setStyleSheet(
            "QPlainTextEdit {"
            "  background: #0d0d0d; color: #c8c8c8;"
            "  font-family: 'Consolas', 'Courier New', monospace;"
            "  font-size: 11px;"
            "}"
        )
        layout.addWidget(self._text)

        btn_layout = QHBoxLayout()
        btn_clear = QPushButton("クリア", self)
        btn_clear.clicked.connect(self._text.clear)
        btn_layout.addWidget(btn_clear)
        btn_layout.addStretch()
        btn_close = QPushButton("閉じる", self)
        btn_close.clicked.connect(self.close)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

    def _append_text(self, text: str) -> None:
        """_emitter.log_emitted のスロット。必ずGUIスレッドで実行される。"""
        self._text.appendPlainText(text.rstrip("\n"))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # stdout / stderr をフック
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        sys.stdout = _DebugStream(self._emitter, self._orig_stdout)
        sys.stderr = _DebugStream(self._emitter, self._orig_stderr)

    def closeEvent(self, event) -> None:
        # stdout / stderr を元に戻す
        if self._orig_stdout is not None:
            sys.stdout = self._orig_stdout
            self._orig_stdout = None
        if self._orig_stderr is not None:
            sys.stderr = self._orig_stderr
            self._orig_stderr = None
        super().closeEvent(event)


if __name__ == "__main__":
    import argparse as _argparse
    _ap = _argparse.ArgumentParser(add_help=False)
    _ap.add_argument("--debug", action="store_true",
                     help="コンソールウィンドウを表示したままにする（デバッグ用）")
    _args, _unknown = _ap.parse_known_args()

    # --debug なしの場合、コンソールウィンドウを非表示にする。
    # py.exe / python.exe 経由だと FreeConsole() では親ウィンドウが残るため、
    # pythonw.exe で自分自身を再起動することで確実に消す。
    if not _args.debug and sys.platform == "win32":
        import subprocess as _sp
        _exe = sys.executable  # 例: C:\...\python.exe
        _pythonw = _exe.replace("python.exe", "pythonw.exe").replace("py.exe", "pythonw.exe")
        import os as _os
        if _os.path.exists(_pythonw) and "pythonw" not in _exe.lower():
            # pythonw.exe が存在し、まだ pythonw で動いていない場合のみ再起動
            _sp.Popen(
                [_pythonw, __file__] + sys.argv[1:],
                creationflags=_sp.CREATE_NO_WINDOW,
            )
            sys.exit(0)

    # Windowsタスクバーのグループ化・アイコン表示対策:
    # AppUserModelIDを明示しないと、pythonw.exe実行時にタスクバーが
    # 実行ファイル(python.exe/pythonw.exe)単位でグループ化され、
    # D-liner独自のアイコンではなくPython標準アイコンが表示される
    # ことがある。QApplication生成前に一度だけ設定しておく。
    if sys.platform == "win32":
        import ctypes as _ctypes
        try:
            _ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "dliner.app.0.8"
            )
        except Exception:
            pass  # 失敗してもアイコン表示以外への影響は無いため無視する

    # バグ修正: PyQt6はスロット内の未処理Python例外でプロセスごと
    # クラッシュすることがある（sys.excepthook未設定が原因）。
    # ここでトレースバックをファイルに残し、ユーザーには即クラッシュ
    # ではなく通常のエラーダイアログを見せるようにする。
    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        import traceback as _traceback
        from datetime import datetime as _datetime

        tb_text = "".join(_traceback.format_exception(exc_type, exc_value, exc_tb))
        print(tb_text, flush=True)
        try:
            log_path = Path(__file__).parent / "crash_log.txt"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- {_datetime.now().isoformat()} ---\n{tb_text}\n")
        except Exception:
            pass
        try:
            QMessageBox.critical(
                None, "予期しないエラー",
                f"予期しないエラーが発生しました。crash_log.txt を確認してください。\n\n{exc_value}",
            )
        except Exception:
            pass

    sys.excepthook = _excepthook

    app = QApplication(sys.argv)

    # 標準スタイルの適用
    app.setStyle("Fusion")

    # アプリ全体のアイコン設定（MainWindow個別のsetWindowIconに加え、
    # SDIWindowやデバッグ出力ウィンドウ等、明示的にアイコンを設定して
    # いない全てのトップレベルウィンドウにも継承される）
    _icon_path = _resource_path("d_liner_icon.ico")
    if os.path.exists(_icon_path):
        app.setWindowIcon(QIcon(_icon_path))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())