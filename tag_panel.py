"""
tag_panel.py — SDIウィンドウ下部のタグパネル関連クラス群
======================================================================
セッション27〜29の高速化・保守性検討（候補2・第2段階）により、
sdi_window_viewer.py から機械的に分離。

【重要】このファイルは sdi_window_viewer.py からのクラス定義の「移動」のみを
目的としており、ロジックの変更は一切行っていない（メソッド本文は1文字も
変えていない）。タグパネル関連（_reflow・高さ計算・コピーモード等）は
セッション20〜22で繰り返し実害バグを出した既知のフラグル領域のため、
今後この領域を触る際は d_liner_設計指針メモ.md の方針に従い、
変更前に必ずリスク評価・熟議を行うこと。

含まれるクラス:
    _FlowLayout               フロー（折り返し）レイアウト
    _ManualTagInputDialog     手動タグ追加ダイアログ（自由入力＋オートコンプリート）
    _ExistingTagPickerDialog  既存タグ選択ダイアログ（DB全体の人気タグから選択）
    TagPanel                  SDIウィンドウ下部のタグパネル本体
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime

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


# ---------------------------------------------------------------------------
# 【一時デバッグログ】コピーモード背景色バグ（handoff30 C）調査用（セッション31）
# ---------------------------------------------------------------------------
# 「コピーモードを維持する」設定でSDIウィンドウを閉じ→別画像を新規に開くと、
# モード自体は維持されるが背景色だけ検索モードの色に戻る不具合の原因調査。
# 静的解析・headless再現テストでは原因を特定できなかったため、実機
# （Windows・4K・150%DPI）でのタイミング・状態を記録し、ログを基に判断する。
# 原因特定・対応完了後は撤去予定（恒久的なログ出力ではない）。
_COPY_MODE_BUG_LOG_PATH = Path(__file__).parent / "tag_panel_debug_log.txt"


def _debug_log_copy_mode_bug(tag: str, msg: str) -> None:
    try:
        with open(_COPY_MODE_BUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='milliseconds')} [{tag}] {msg}\n")
    except Exception:
        pass  # デバッグログ自体の失敗でアプリ本体に影響を出さないようにする


class _FlowLayout(QHBoxLayout):
    """
    タグボタンを横に並べて自動折り返しするレイアウト。
    QLayout を継承せず QWidget.resizeEvent で再配置する軽量実装。
    親 QWidget の resizeEvent から reflow() を呼ぶ。
    """
    pass  # 実装は TagPanel._reflow() で行う


# ---------------------------------------------------------------------------
# 手動タグ追加ダイアログ群（指示書02 タスクB）
# ---------------------------------------------------------------------------

class _ManualTagInputDialog(QDialog):
    """
    タグ自由入力ダイアログ。既存タグからのオートコンプリート付き
    （入力に200〜300msのデバウンスを挟んでDB問い合わせを行う簡易版。
    本格的なサジェスト機能が実装され次第、差し替える前提）。
    Enterで確定、Escでキャンセル。
    """

    def __init__(self, existing_tags_on_image: set[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("タグを追加")
        self.setMinimumWidth(320)
        self._existing_tags_on_image = existing_tags_on_image
        self._result_tag: str | None = None
        # オートコンプリートの候補選択・一覧選択経由での確定かどうか。
        # True の場合、既存タグの選択なので「新規タグ作成の確認」を省略する。
        self._is_from_completion = False

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("追加するタグ名:"))

        self.input = QLineEdit(self)
        self.input.setPlaceholderText("例: original_character")
        layout.addWidget(self.input)

        # デバウンス: 直前のタイマーをキャンセルしてから再設定する形にすることで、
        # タイピング中にDB問い合わせが連続発火しないようにする。
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(250)
        self._debounce_timer.timeout.connect(self._refresh_completions)

        self.completer = QCompleter([], self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.activated.connect(self._on_completion_activated)
        self.input.setCompleter(self.completer)
        self.input.textEdited.connect(self._on_text_edited)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.input.setFocus()

    def _on_text_edited(self, _text: str) -> None:
        self._is_from_completion = False
        self._debounce_timer.stop()
        self._debounce_timer.start()

    def _on_completion_activated(self, _text: str) -> None:
        self._is_from_completion = True

    def _refresh_completions(self) -> None:
        text = self.input.text().strip()
        if not text:
            self.completer.setModel(QStringListModel([], self))
            return
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            cursor = conn.cursor()
            # '%' ワイルドカードを付与しないと完全一致検索になり候補が出ない
            cursor.execute(
                "SELECT DISTINCT tag FROM tags WHERE tag LIKE ? ORDER BY tag LIMIT 20",
                (f"%{text}%",),
            )
            candidates = [row[0] for row in cursor.fetchall()]
            conn.close()
        except Exception:
            candidates = []
        self.completer.setModel(QStringListModel(candidates, self))

    def _tag_exists_in_db(self, tag: str) -> bool:
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM tags WHERE tag = ? LIMIT 1", (tag,))
            found = cursor.fetchone() is not None
            conn.close()
            return found
        except Exception:
            return False

    def _on_accept(self) -> None:
        tag = self.input.text().strip()
        if not tag:
            return
        # 誤字防止: 既存タグに存在しない新規文字列の場合のみ確認を挟む。
        # オートコンプリート/一覧選択経由での確定は確認不要。
        if not self._is_from_completion and not self._tag_exists_in_db(tag):
            reply = QMessageBox.question(
                self,
                "新しいタグの追加",
                f"新しいタグ「{tag}」を追加しますか？\n"
                f"（データベースにまだ存在しないタグです）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._result_tag = tag
        self.accept()

    def result_tag(self) -> str | None:
        return self._result_tag


class _ExistingTagPickerDialog(QDialog):
    """
    DB全体でよく使われているタグ上位N件から、この画像に未付与のものを
    選んで manual タグとして追加するための一覧ダイアログ。
    """

    def __init__(
        self,
        existing_tags_on_image: set[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("既存タグから選択")
        self.setMinimumSize(280, 400)
        self._result_tag: str | None = None
        self._existing_tags_on_image = existing_tags_on_image

        layout = QVBoxLayout(self)

        # 実機フィードバック対応: 元々は「よく使われている上位200件のうち
        # 未付与の先頭50件」しか表示されず、それ以外のタグを選ぶ手段が
        # 無かった。検索ボックスを追加し、入力があればDB全体からLIKE検索
        # する（_ManualTagInputDialogのオートコンプリートと同じ250ms
        # デバウンスパターンを流用）。
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("タグを検索...")
        layout.addWidget(self.search_edit)

        layout.addWidget(QLabel("よく使われているタグ（この画像に未付与のもの）:"))

        self.list_widget = QListWidget(self)
        layout.addWidget(self.list_widget)

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(250)
        self._debounce_timer.timeout.connect(self._reload_list)
        self.search_edit.textEdited.connect(self._on_search_text_edited)
        self.search_edit.returnPressed.connect(self._on_search_return_pressed)

        self._reload_list()

        self.list_widget.itemDoubleClicked.connect(self._on_double_clicked)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.search_edit.setFocus()

    def _on_search_text_edited(self, _text: str) -> None:
        self._debounce_timer.stop()
        self._debounce_timer.start()

    def _on_search_return_pressed(self) -> None:
        """
        実機フィードバック対応: 検索してもDBに完全一致するタグが無い場合、
        Enterキーで直接「新規タグとして追加」の確認に進めるようにする。
        完全一致するタグが存在する場合（＝一覧から選ぶべき状況）は、誤操作
        防止のため何もしない（一覧からのダブルクリック/OKボタンで選ぶ）。
        """
        text = self.search_edit.text().strip()
        if not text or self._exact_tag_exists(text):
            return
        self._confirm_and_add_new(text)

    def _normalize_tag(self, s: str) -> str:
        return s.strip().lower().replace(" ", "_")

    def _exact_tag_exists(self, text: str) -> bool:
        """
        text がDB上のタグ（表記ゆれ含む: 大文字小文字・スペース/アンダー
        スコア）と完全一致するか、既にこの画像に付与済みかを判定する。
        一致する場合は「新規タグとして追加」を出さない（重複作成防止）。
        """
        norm = self._normalize_tag(text)
        if norm in {self._normalize_tag(t) for t in self._existing_tags_on_image}:
            return True
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM tags WHERE tag = ? LIMIT 1", (norm,))
            found = cursor.fetchone() is not None
            conn.close()
            return found
        except Exception:
            return False

    def _confirm_and_add_new(self, text: str) -> None:
        """
        _ManualTagInputDialog と同じ文言・確認方針（誤字防止のための確認）
        に揃える。承諾されたら結果として確定しダイアログを閉じる。
        """
        reply = QMessageBox.question(
            self,
            "新しいタグの追加",
            f"新しいタグ「{text}」を追加しますか？\n"
            f"（データベースにまだ存在しないタグです）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._result_tag = text
        self.accept()

    def _reload_list(self) -> None:
        """
        検索欄が空なら従来通り「DB全体の頻出タグ上位」を、入力があれば
        DB全体をLIKE検索した結果を、それぞれ表示する（いずれもこの画像に
        未付与のもの・上位50件まで）。

        検索文字列がDB上のどのタグとも完全一致しない場合（表記ゆれ込みで
        判定）、他の候補の有無に関わらず末尾に「新規タグとして追加」の
        選択肢を追加する（実機フィードバック対応: 既存タグ検索と新規タグ
        追加を同じ入口にまとめてほしいという要望より）。
        """
        search_text = self.search_edit.text().strip()
        self.list_widget.clear()

        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            cursor = conn.cursor()
            # 実機フィードバック対応: 従来は category != 'manual' で除外して
            # いたが、このダイアログ経由で選んだタグは出所に関わらず必ず
            # manual として保存される（_add_manual_tag() 参照）ため、
            # manual タグ自体を候補から除外する理由が無かった。LoRA
            # トリガーワード等の使い回したいmanualタグも候補に出るように、
            # カテゴリによる除外条件を撤去し全カテゴリ横断で集計する。
            if search_text:
                cursor.execute(
                    "SELECT tag, COUNT(*) as cnt FROM tags "
                    "WHERE tag LIKE ? "
                    "GROUP BY tag ORDER BY cnt DESC LIMIT 200",
                    (f"%{search_text}%",),
                )
            else:
                cursor.execute(
                    "SELECT tag, COUNT(*) as cnt FROM tags "
                    "GROUP BY tag ORDER BY cnt DESC LIMIT 200"
                )
            rows = cursor.fetchall()
            conn.close()
        except Exception:
            rows = []

        shown = 0
        for tag, cnt in rows:
            if tag in self._existing_tags_on_image:
                continue
            item = QListWidgetItem(f"{tag.replace('_', ' ')}  ({cnt})")
            item.setData(Qt.ItemDataRole.UserRole, tag)
            self.list_widget.addItem(item)
            shown += 1
            if shown >= 50:
                break

        if shown == 0:
            placeholder = QListWidgetItem(
                "(該当タグなし)" if search_text else "(タグがまだありません)"
            )
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(placeholder)

        if search_text and not self._exact_tag_exists(search_text):
            new_item = QListWidgetItem(f"＋「{search_text}」を新規タグとして追加")
            new_item.setData(Qt.ItemDataRole.UserRole, search_text)
            new_item.setData(Qt.ItemDataRole.UserRole + 1, True)  # is_new フラグ
            new_item.setForeground(QColor("#7ec8e3"))
            self.list_widget.addItem(new_item)

    def _on_double_clicked(self, item: QListWidgetItem) -> None:
        tag = item.data(Qt.ItemDataRole.UserRole)
        if not tag:
            return
        if item.data(Qt.ItemDataRole.UserRole + 1):
            self._confirm_and_add_new(tag)
            return
        self._result_tag = tag
        self.accept()

    def _on_accept(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            return
        tag = item.data(Qt.ItemDataRole.UserRole)
        if not tag:
            return
        if item.data(Qt.ItemDataRole.UserRole + 1):
            self._confirm_and_add_new(tag)
            return
        self._result_tag = tag
        self.accept()

    def result_tag(self) -> str | None:
        return self._result_tag


# ---------------------------------------------------------------------------
# タグパネル（SDIウィンドウ下部に表示）
# ---------------------------------------------------------------------------

class TagPanel(QWidget):
    """
    SDIウィンドウ下部に表示するタグパネル。

    Grabber スタイルでタグをカテゴリ別に色分けして横並び・自動折り返し表示。
    タグをクリックするとメインウィンドウの検索バーにタグを追記して絞り込む。
    行数は最大 MAX_ROWS 行まで自動拡張する。

    カテゴリ配色（Danbooru / Grabber 準拠）:
      general   #a0c4e8  青白
      character #82d982  緑
      copyright #c797ff  紫
      artist    #f28383  赤/橙
      meta      #f0c040  黄
      rating    #a8d8a8  黄緑
    """

    CATEGORY_COLORS: dict[str, str] = {
        "manual":    "#ffd54f",
        "general":   "#a0c4e8",
        "character": "#82d982",
        "copyright": "#c797ff",
        "artist":    "#f28383",
        "meta":      "#f0c040",
        "rating":    "#a8d8a8",
    }
    DEFAULT_COLOR = "#cccccc"

    # カテゴリ表示順（重要度順）。manual は指示書02タスクBに基づき先頭固定。
    CATEGORY_ORDER = ["manual", "character", "copyright", "artist", "general", "meta", "rating"]

    # 最大行数（これを超える場合は末尾を省略する）
    MAX_ROWS = 3
    # ボタンの行高さ（フォント12px + padding + margin）
    ROW_H = 26
    # 行間の余白
    H_MARGIN = 6
    V_MARGIN = 4
    # コピーモードのアクション行の固定高さ（指示書03 C-5）。
    # 選択件数に関わらずコピーモード中は常にこの高さを確保する
    # （選択のたびに高さが変わりpanel_resizedが連鎖するのを避けるため。
    # セッション21-22でタグパネル高さ確定→SDIウィンドウリサイズ計算の
    # 連携を修正した繊細な領域への負荷再燃リスクを避ける判断）。
    ACTION_ROW_H = 28

    # バグ修正: タグ取得は TagFetchWorker による非同期処理のため、
    # load_image() 内の _auto_resize_window_if_raw() 呼び出し時点では
    # まだこのパネルの最終的な高さ（行数）が確定していない
    # （_on_tags_fetched() が完了するまでは前の画像時点の高さ、
    # 初回表示時は非表示＝高さ0のまま）。そのため、ウィンドウの
    # リサイズ計算がタグエリアの占有分を考慮できず、初回表示や
    # 小解像度画像で不要なスクロールバーが出る原因になっていた。
    # 実際に高さが変化したタイミングを外部（SDIWindow）に通知し、
    # 現在の画像サイズを使ってリサイズ計算をやり直せるようにする。
    panel_resized = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._main_window = None
        self._active_worker = None
        self._current_image_id: int | None = None
        self._buttons: list[QWidget] = []  # 実体は _make_tag_chip() が返すコンテナ
        # 現在表示中の画像に付与されているタグ名集合（指示書02 タスクB）。
        # オートコンプリート除外・既存タグ選択ダイアログでの重複除外に使う。
        self._current_tag_names: set[str] = set()
        # タグ表示あふれ対策（3行を超えた分の「+N件」展開表示、指示書4）
        self._expanded: bool = False
        self._more_indicator: QPushButton | None = None

        # --- 指示書03: 検索モード/コピーモード ---
        self._mode: str = "search"  # "search" | "copy"
        # CATEGORY_ORDER順に並べ替え済みの (tag, category) リスト。
        # 「全タグをコピー」「選択タグをコピー/検索」双方の情報源はこれを使う
        # （_buttons から集めると、折りたたみ中の非表示チップが漏れる事故に
        # なるため使わないこと。詳細は指示書03タスクB参照）。
        self._current_tags: list[tuple[str, str]] = []
        self._selected_tags: set[str] = set()  # コピーモードでのトグル選択
        self._chip_body_by_tag: dict[str, QPushButton] = {}  # ハイライト操作用
        # 指示書06 機能追加1: コピーモード中は×削除ボタンを無効化して誤消去を
        # 防ぐ。ハイライト用の _chip_body_by_tag とは別に、×ボタン自体への
        # 参照をここに保持する。
        self._close_btn_by_tag: dict[str, QPushButton] = {}
        self._action_row: QWidget | None = None
        self._copy_btn: QPushButton | None = None
        self._search_combo_btn: QPushButton | None = None
        self._last_display_rows: int = 0

        self._init_ui()
        self.setVisible(False)

    def _init_ui(self) -> None:
        # バグ修正（指示書06 バグ1）: プレーンなQWidgetはこの属性が無いと
        # スタイルシートのborder系プロパティを正しく描画しないことがある
        # （background-colorは比較的描画されるが、borderだけ無視される
        # Qtの既知の挙動）。コピーモード切替時に上枠線の色が変化して
        # 見えなかった実機不具合はこれが原因と判断。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._apply_panel_style()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        # 右クリックでタグ追加メニューを出す（指示書02 タスクB）。
        # タグ個別の削除は「×」チップボタン方式に変更済み（右クリックは
        # SDIWindowのページ送りと衝突するため使わない、指示書「タグ削除UIの
        # 右クリック問題の修正」参照）。
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_panel_context_menu)
        self._init_action_row()
        # ボタンは絶対位置配置（resizeEvent で再配置）
        self._update_height(0)

    def _apply_panel_style(self) -> None:
        """
        パネル背景色＋モードに応じた上枠線を、毎回まとめて1回で設定する。

        注意（指示書03実装前のソース照合で判明）: QWidget.setStyleSheet() は
        呼ぶたびに前回の指定を丸ごと置き換える。border-top の差分だけを
        setStyleSheet() すると、ここで設定する background-color が消えて
        パネルが透明になる事故につながるため、必ず背景色込みで組み立てる。
        セレクタもクラス名で自身に限定し、チップ個別の色付けに影響しない
        ようにする。

        指示書07: 枠線の色変化（青系2px）だけではコピーモードへの切替に
        気づきにくいという実機フィードバックを受け、背景色自体も
        暖色・低彩度の焦茶〜赤系（#33201f）に変える。選択中タグの
        ハイライト色（青系、_chip_body_stylesheet 側）はここでは
        変更しない（「コピーモードである」ことと「タグが選択済みである」
        ことは別レイヤーの意味を持つため、指示書07で明示的に維持を指定）。
        """
        if self._mode == "copy":
            bg = "#33201f"
            border = "border-top: 2px solid #c0776b;"
        else:
            bg = "#1e1e1e"
            border = "border-top: 1px solid #333333;"
        self.setStyleSheet(f"TagPanel {{ background-color: {bg}; {border} }}")
        # 【一時デバッグログ】バグC調査用（セッション31）: 設定直後の状態を記録
        _debug_log_copy_mode_bug(
            "apply_style",
            f"id={id(self)} mode={self._mode} bg={bg} "
            f"visible={self.isVisible()} "
            f"WA_StyledBackground={self.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)} "
            f"styleSheet_after={self.styleSheet()!r}",
        )

    def _update_height(self, rows: int) -> None:
        """行数（＋コピーモード中はアクション行分）に合わせてパネル高さを更新する。"""
        old_h = self.height()
        if rows == 0:
            h = 0
        else:
            h = self.V_MARGIN + rows * self.ROW_H + (rows - 1) * 2 + self.V_MARGIN
            if self._mode == "copy":
                # 選択件数に関わらず、コピーモード中は常にアクション行の
                # 高さを予約する（C-5参照。選択のたびに高さが変わる方式は
                # 採用しない）。
                h += self.ACTION_ROW_H
        self.setFixedHeight(h)
        # 高さが実際に変わった場合のみ通知（no-opリサイズの連鎖を避ける）
        if self.height() != old_h:
            self.panel_resized.emit()

    # ------------------------------------------------------------------
    # 外部インターフェース
    # ------------------------------------------------------------------

    def set_main_window(self, main_window) -> None:
        self._main_window = main_window

    def set_mode(self, mode: str) -> None:
        """
        検索モード/コピーモードを切り替える（指示書03）。
        呼び出し元（SDIWindowのコーナーウィジェット）側で QSettings への
        永続化・不正値のフォールバックは完了済みという前提で、ここでは
        "search"/"copy" 以外が来た場合のみ念のため "search" に丸める。
        """
        if mode not in ("search", "copy"):
            mode = "search"
        # 【一時デバッグログ】バグC調査用（セッション31）: 呼び出し自体の記録
        _debug_log_copy_mode_bug(
            "set_mode",
            f"id={id(self)} old_mode={self._mode} new_mode={mode} visible={self.isVisible()}",
        )
        self._mode = mode
        # モードを切り替えるたび（方向を問わず）、選択状態と見た目のハイライトを
        # 必ずクリアする（指示書03 C-3・C-4）。チップは使い回されるため、
        # ここでの解除漏れは実害に直結する（load_tags_for側と違い、
        # チップがすぐ作り直されるわけではないため）。
        self._selected_tags.clear()
        self._clear_chip_highlights()
        self._update_close_buttons_enabled()
        self._apply_panel_style()
        self._reflow()  # アクション行の表示/非表示・高さ再計算を反映

    def load_tags_for(self, image_id: int) -> None:
        if image_id == self._current_image_id:
            return
        self._current_image_id = image_id
        # 別の画像に切り替わるので、展開状態は毎回3行表示にリセットする
        self._expanded = False
        # バグ修正防止（指示書03 C-3・最重要）: _current_tags の更新は
        # 非同期（_on_tags_fetched() 完了後）だが、_selected_tags の
        # クリアは同期的に即座に行う必要がある。両者を _on_tags_fetched()
        # 内でまとめて行うと、画像切り替え直後〜Worker完了までの間、
        # 古い画像の選択状態が残ったまま「コピー」「この組み合わせで検索」
        # が実行可能になってしまう窓ができる。ここで即座にクリアし、
        # 実装上完全に独立した処理として扱う。
        self._selected_tags.clear()
        self._clear_chip_highlights()

        # バグ修正: 以前は実行中の古いワーカーに対し quit() + wait(200)
        # で完了を同期的に待っていたが、TagFetchWorker.run() は QThread
        # のイベントループ（exec()）を使わないため quit() は実質何もせず、
        # 古いワーカーがまだDBクエリ実行中であればほぼ毎回 200ms 近く
        # GUIスレッドをブロックしていた。マウスホイールでの高速な
        # 連続画像送り時にこれが積み重なり、ウィンドウが応答しなくなる
        # （フリーズ・ちらつき）不具合の原因になっていた。
        # _on_tags_fetched() 側は image_id の不一致で古い結果を安全に
        # 破棄する設計になっているため、同期的に完了を待つ必要は無い。
        # 古いワーカーはシグナルだけ切断し、バックグラウンドで自然に
        # 終わらせる（GUIスレッドはブロックしない）。
        if self._active_worker is not None:
            try:
                self._active_worker.finished.disconnect()
            except TypeError:
                pass
            try:
                self._active_worker.error.disconnect()
            except TypeError:
                pass
        self._active_worker = None

        if image_id < 0:
            self._clear()
            return

        from workers import TagFetchWorker
        self._active_worker = TagFetchWorker(image_id, parent=self)
        self._active_worker.finished.connect(self._on_tags_fetched)
        self._active_worker.error.connect(lambda _: self._clear())
        self._active_worker.start()

    def clear(self) -> None:
        self._current_image_id = None
        self._clear()

    # ------------------------------------------------------------------
    # 内部処理
    # ------------------------------------------------------------------

    def _clear(self) -> None:
        self._remove_all_buttons()
        self._expanded = False
        self._last_display_rows = 0
        self._update_height(0)
        self._update_action_row(0)
        self.setVisible(False)

    def _remove_all_buttons(self) -> None:
        for btn in self._buttons:
            btn.deleteLater()
        self._buttons.clear()
        if self._more_indicator is not None:
            self._more_indicator.deleteLater()
            self._more_indicator = None
        # 指示書03: チップが無くなるので、コピー用の情報源・ハイライト
        # マッピングも一貫してリセットする（_clear()経由でも漏れないように
        # ここに置く）。
        self._current_tags = []
        self._chip_body_by_tag = {}
        self._close_btn_by_tag = {}

    def _on_tags_fetched(self, result: tuple) -> None:
        image_id, tags = result
        if image_id != self._current_image_id:
            return

        self._remove_all_buttons()
        self._current_tag_names = {tag for tag, _cat in tags}

        if not tags:
            # バグ修正: 以前はここで _update_height(0) を呼んでいなかった
            # ため、前の画像でタグパネルが表示されていた状態から
            # タグ無し画像へ切り替えたときに panel_resized が発火せず、
            # ウィンドウ側がタグパネル分の余白を確保したままになる
            # （不足ではなく過剰確保だが、_clear() 側の挙動と非対称だった）。
            self._last_display_rows = 0
            self._update_height(0)
            self._update_action_row(0)
            self.setVisible(False)
            return

        # カテゴリ別に分類してから順序通りにボタン生成
        by_cat: dict[str, list[str]] = {c: [] for c in self.CATEGORY_ORDER}
        for tag, category in tags:
            bucket = category if category in by_cat else "general"
            by_cat[bucket].append(tag)

        # 指示書03 C-2: _current_tags は表示順（CATEGORY_ORDER順）で保持する。
        # 「全タグをコピー」「選択タグの抽出」双方がこれを情報源にする。
        for cat in self.CATEGORY_ORDER:
            for tag in by_cat[cat]:
                self._current_tags.append((tag, cat))
                chip = self._make_tag_chip(tag, cat)
                chip.setParent(self)
                self._buttons.append(chip)

        self.setVisible(True)
        self._reflow()

    def _chip_body_stylesheet(self, color: str, selected: bool) -> str:
        """
        タグチップ本体ボタンのスタイルを返す。通常時とコピーモードでの
        選択ハイライト時の両方をここで一元管理し、_make_tag_chip()（生成時）
        と _highlight_chip()（選択トグル時）の双方から共用する。
        選択時は hover/pressed の上書きを持たせず、ハイライトが
        マウスオーバーで消えて見えることが無いようにする。
        """
        if selected:
            return f"""
                QPushButton {{
                    color: {color};
                    background: rgba(91, 155, 213, 70);
                    border: 1px solid #5b9bd5;
                    border-radius: 3px;
                    padding: 2px 4px;
                    font-size: 12px;
                }}
            """
        return f"""
            QPushButton {{
                color: {color};
                background: transparent;
                border: none;
                padding: 2px 4px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background: rgba(255,255,255,18);
                border-radius: 3px;
            }}
            QPushButton:pressed {{
                background: rgba(255,255,255,35);
            }}
        """

    def _tag_category(self, tag: str) -> str:
        """self._current_tags からタグのカテゴリを引く（無ければ general 扱い）。"""
        for t, c in self._current_tags:
            if t == tag:
                return c
        return "general"

    def _highlight_chip(self, tag: str, selected: bool) -> None:
        body = self._chip_body_by_tag.get(tag)
        if body is None:
            return
        color = self.CATEGORY_COLORS.get(self._tag_category(tag), self.DEFAULT_COLOR)
        body.setStyleSheet(self._chip_body_stylesheet(color, selected))

    def _update_close_buttons_enabled(self) -> None:
        """
        指示書06 機能追加1: モード切替のたび、既存の全チップの×削除ボタンの
        有効/無効を更新する。_chip_body_by_tag（ハイライト用）とは別に
        _close_btn_by_tag を走査する。
        """
        disabled = self._mode == "copy"
        for tag, btn in self._close_btn_by_tag.items():
            btn.setEnabled(not disabled)
            btn.setToolTip("コピーモード中は削除できません" if disabled else "このタグを削除")

    def _clear_chip_highlights(self) -> None:
        """
        表示中の全チップのハイライトを解除する（指示書03 C-4）。
        load_tags_for() 冒頭・set_mode() の双方から呼ばれる。set_mode()側は
        画像が変わらずチップを使い回すため、ここでの解除漏れが実害に
        直結する（load_tags_for()側はチップがまもなく作り直されるため
        「次の描画までの一瞬」の保険的な意味合い）。
        """
        for tag in list(self._chip_body_by_tag.keys()):
            self._highlight_chip(tag, selected=False)

    def _toggle_tag_selection(self, tag: str) -> None:
        """コピーモード中、タグクリックで選択をトグルする（指示書03 C-4）。"""
        if tag in self._selected_tags:
            self._selected_tags.discard(tag)
            self._highlight_chip(tag, selected=False)
        else:
            self._selected_tags.add(tag)
            self._highlight_chip(tag, selected=True)
        # チップの再配置は不要（選択状態は高さに影響しない、C-5参照）。
        # アクション行の表示/非表示だけを更新する。
        self._update_action_row(self._last_display_rows)

    def _get_selected_tags_ordered(self) -> list[tuple[str, str]]:
        """
        self._selected_tags（集合）を直接使わず、self._current_tags を
        先頭から走査して選択中のものだけを抽出する。集合は順序を保持
        しないため、こうすることで (a) format_tags_for_copy() が要求する
        (tag, category) 形式が得られ、(b) 出力順がクリック順ではなく
        タグパネルの表示順（CATEGORY_ORDER順）に揃う（指示書03 C-5）。
        """
        return [(t, c) for t, c in self._current_tags if t in self._selected_tags]

    def _init_action_row(self) -> None:
        """
        コピーモード専用のアクション行（「コピー」「この組み合わせで検索」）。
        既存の絶対位置配置の流儀を維持し、QVBoxLayout等でパネル全体を
        作り直すことはしない（指示書03 C-5）。
        """
        self._action_row = QWidget(self)
        self._action_row.setVisible(False)
        row_layout = QHBoxLayout(self._action_row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(6)

        btn_style = (
            "QPushButton { color: #ffffff; background: #3d6fa8; border: none; "
            "border-radius: 3px; padding: 3px 10px; font-size: 12px; }"
            "QPushButton:hover { background: #4a82c2; }"
            "QPushButton:pressed { background: #2f5786; }"
        )

        self._copy_btn = QPushButton("コピー", self._action_row)
        self._copy_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._copy_btn.setStyleSheet(btn_style)
        self._copy_btn.clicked.connect(self._on_copy_selected)
        row_layout.addWidget(self._copy_btn)

        self._search_combo_btn = QPushButton("この組み合わせで検索", self._action_row)
        self._search_combo_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._search_combo_btn.setStyleSheet(btn_style)
        self._search_combo_btn.clicked.connect(self._on_search_selected)
        row_layout.addWidget(self._search_combo_btn)
        row_layout.addStretch(1)

    def _update_action_row(self, display_rows: int) -> None:
        """
        アクション行の配置・表示/非表示を更新する。
        高さの予約は _update_height() 側が担う（モード中は常に確保）ため、
        ここでは行の実際の表示（選択1件以上の時だけ）と位置決めのみ行う。
        """
        if self._action_row is None:
            return
        if self._mode != "copy" or display_rows == 0:
            self._action_row.setVisible(False)
            return
        base_h = self.V_MARGIN + display_rows * self.ROW_H + (display_rows - 1) * 2 + self.V_MARGIN
        panel_w = max(self.width() - 2 * self.H_MARGIN, 0)
        self._action_row.setGeometry(self.H_MARGIN, base_h, panel_w, self.ACTION_ROW_H - 4)
        self._action_row.setVisible(len(self._selected_tags) > 0)

    def _on_copy_selected(self) -> None:
        """選択中タグを format_tags_for_copy() で整形してクリップボードへ。"""
        selected = self._get_selected_tags_ordered()
        if not selected:
            return
        from workers import format_tags_for_copy
        text = format_tags_for_copy(selected)
        QGuiApplication.clipboard().setText(text)

    def _on_search_selected(self) -> None:
        """
        「この組み合わせで検索」: 検索欄を選択タグのみで全置換してAND検索する。
        既存の main_window._on_tag_list_clicked() は単一タグの追記専用
        （既存入力を残す）実装のため、ここでは流用せず直接実装する
        （指示書03改訂・重要）。
        """
        mw = self._main_window
        if mw is None:
            return
        selected = self._get_selected_tags_ordered()
        if not selected:
            return
        try:
            mw.search_input.setText(" ".join(tag for tag, _cat in selected))
            # setText()のtextChangedで動く1000msデバウンス経由の
            # 二重検索実行を防ぐ（指示書03改訂・重要）。
            if hasattr(mw, "search_timer"):
                mw.search_timer.stop()
            mw.trigger_search()
        except RuntimeError:
            # mainwindowが理論上のみ既に破棄されているケース。
            # 静かに諦める（指示書03改訂・注意喚起）。
            pass

    def _make_tag_chip(self, tag: str, category: str) -> QWidget:
        """
        タグ1件を「本体ボタン（左クリックで検索絞り込み）」+
        「×削除ボタン」の横並びで構成する。

        バグ修正: 従来は右クリックでの削除を想定していたが、QPushButton
        は右クリックのpressイベントを消費しないため親のSDIWindow.
        mousePressEvent（右クリック＝次ページ送り、Linar本家準拠）まで
        伝播してしまい、コンテキストメニューが出る前にページが切り替わって
        しまい機能していなかった（実機確認済み）。右クリックに依存しない
        「×」ボタン方式に変更する。
        """
        color = self.CATEGORY_COLORS.get(category, self.DEFAULT_COLOR)
        display = tag.replace("_", " ")

        container = QWidget(self)
        container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        body = QPushButton(display, container)
        body.setFlat(True)
        body.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        body.setToolTip(f"{tag}\n[{category}]  クリックで検索絞り込み")
        body.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        body.setStyleSheet(self._chip_body_stylesheet(color, selected=False))
        body.adjustSize()
        body.clicked.connect(lambda _checked=False, t=tag: self._on_tag_clicked(t))
        layout.addWidget(body)
        self._chip_body_by_tag[tag] = body

        close_btn = QPushButton("×", container)
        close_btn.setFixedSize(16, 16)
        close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        close_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; "
            "color: #999999; font-weight: bold; }"
            "QPushButton:hover { color: #ff5555; }"
            "QPushButton:disabled { color: #555555; }"
        )
        close_btn.clicked.connect(
            lambda _checked=False, t=tag, c=category: self._delete_tag(t, c)
        )
        # 指示書06 機能追加1: コピーモード中はタグクリックの意味が
        # 検索絞り込み→選択に変わるため、誤操作防止のため×削除も
        # 無効化しておく（生成時点のモードを反映）。
        if self._mode == "copy":
            close_btn.setEnabled(False)
            close_btn.setToolTip("コピーモード中は削除できません")
        else:
            close_btn.setToolTip("このタグを削除")
        layout.addWidget(close_btn)
        self._close_btn_by_tag[tag] = close_btn

        container.adjustSize()
        return container

    def _reflow(self) -> None:
        """
        タグチップを横に並べて折り返す。3行(MAX_ROWS)を超える分は、
        末尾に「+N件」インジケータを表示して隠す（クリックで全展開）。
        展開中（self._expanded）は行数上限を外して全件表示する。
        """
        if self._more_indicator is not None:
            self._more_indicator.deleteLater()
            self._more_indicator = None

        if not self._buttons:
            self._last_display_rows = 0
            self._update_height(0)
            self._update_action_row(0)
            return

        panel_w = self.width()
        if panel_w < 10:
            return  # まだサイズが確定していない

        max_rows = 10**9 if self._expanded else self.MAX_ROWS  # 展開時は無制限

        x = self.H_MARGIN
        y = self.V_MARGIN
        row = 1
        GAP = 4  # チップ間の水平ギャップ
        shown: list[QWidget] = []

        for chip in self._buttons:
            bw = chip.sizeHint().width()
            bh = self.ROW_H

            # 折り返し判定
            if x + bw > panel_w - self.H_MARGIN and x > self.H_MARGIN:
                row += 1
                if row > max_rows:
                    break  # ここから先は全て「+N件」側へ回す
                x = self.H_MARGIN
                y += bh + 2

            chip.setGeometry(x, y, bw, bh - 2)
            chip.show()
            shown.append(chip)
            x += bw + GAP

        hidden_chips = [c for c in self._buttons if c not in shown]
        for c in hidden_chips:
            c.hide()

        if hidden_chips and not self._expanded:
            # 3行目の末尾に「+N件」インジケータを追加（クリックで全展開）
            more_btn = QPushButton(f"+{len(hidden_chips)}件", self)
            more_btn.setFlat(True)
            more_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            more_btn.setToolTip("クリックで残りのタグをすべて表示")
            more_btn.setStyleSheet(
                "QPushButton { color: #6699ff; background: transparent; "
                "border: none; font-weight: bold; font-size: 12px; padding: 2px 4px; }"
                "QPushButton:hover { text-decoration: underline; }"
            )
            more_btn.adjustSize()
            more_btn.clicked.connect(self._expand_tags)
            # 3行目に収まりきらない場合は折り返して新しい行に置く
            mw = more_btn.sizeHint().width()
            if x + mw > panel_w - self.H_MARGIN and x > self.H_MARGIN:
                x = self.H_MARGIN
                y += self.ROW_H + 2
            more_btn.setGeometry(x, y, mw, self.ROW_H - 2)
            more_btn.show()
            self._more_indicator = more_btn

        display_rows = row if self._expanded else min(row, max_rows)
        self._last_display_rows = display_rows
        self._update_height(display_rows)
        self._update_action_row(display_rows)

    def _expand_tags(self) -> None:
        """「+N件」クリックで全タグを展開表示する。"""
        self._expanded = True
        self._reflow()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def _on_tag_clicked(self, tag: str) -> None:
        if self._mode == "copy":
            self._toggle_tag_selection(tag)
            return
        mw = self._main_window
        if mw is None:
            return
        if hasattr(mw, "_on_tag_list_clicked"):
            mw._on_tag_list_clicked(tag)
        elif hasattr(mw, "search_input"):
            current = mw.search_input.text().strip()
            tokens = current.split()
            if tag not in tokens:
                mw.search_input.setText((current + " " + tag).strip())

    # ------------------------------------------------------------------
    # 手動タグ追加・削除（指示書02 タスクB）
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        """
        バグ修正: 親のSDIWindowが右クリックをページ送りとして処理するため
        （Linar本家準拠のページ送り仕様）、ここで消費せず素通りさせると
        「パネル空白部分を右クリック→ページが送られてからメニューが開く」
        という不具合になっていた（実機確認済み）。TagPanel内での右クリックは
        常にこのパネル自身の操作として扱うため、ここでacceptして親への
        伝播を止める。contextMenuEvent（customContextMenuRequested）は
        別イベントのため、ここで止めても「タグを追加」メニュー自体は
        正常に開く。
        """
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def _on_panel_context_menu(self, pos: QPoint) -> None:
        """パネル空白部分の右クリック → タグ追加系メニュー。"""
        if self._current_image_id is None or self._current_image_id < 0:
            return

        menu = QMenu(self)

        add_action = QAction("タグを追加...", self)
        add_action.triggered.connect(self.open_add_tag_dialog)
        menu.addAction(add_action)

        select_action = QAction("既存タグ一覧から選択...", self)
        select_action.triggered.connect(self._open_select_existing_tag_dialog)
        menu.addAction(select_action)

        menu.exec(self.mapToGlobal(pos))

    def open_add_tag_dialog(self) -> None:
        """
        「タグを追加」ダイアログを開く。パネルの右クリックメニューに加え、
        SDIWindow 側の T キーショートカットからも呼ばれる（公開メソッド）。
        """
        if self._current_image_id is None or self._current_image_id < 0:
            return
        dlg = _ManualTagInputDialog(self._current_tag_names, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            tag = dlg.result_tag()
            if tag:
                self._add_manual_tag(tag)

    def _open_select_existing_tag_dialog(self) -> None:
        if self._current_image_id is None or self._current_image_id < 0:
            return
        dlg = _ExistingTagPickerDialog(self._current_tag_names, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            tag = dlg.result_tag()
            if tag:
                self._add_manual_tag(tag)

    def _add_manual_tag(self, tag: str) -> None:
        """
        タグを manual カテゴリとして追加する。既に別カテゴリで存在する
        同名タグがあれば manual へ上書き（格上げ）する（指示書02 タスクB）。
        """
        if self._current_image_id is None or self._current_image_id < 0:
            return
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            conn.execute(
                "INSERT INTO tags (image_id, tag, category) VALUES (?, ?, 'manual') "
                "ON CONFLICT(image_id, tag) DO UPDATE SET category = 'manual'",
                (self._current_image_id, tag),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"タグの追加に失敗しました: {e}")
            return
        self._refresh_after_tag_change()

    def _delete_tag(self, tag: str, category: str) -> None:
        """
        タグを削除する。AI由来・手動由来を問わず対象にできる
        （AIの誤タグ修正が用途の一つのため）。

        バグ修正: manual タグは AI 由来タグと異なり、削除しても
        再タグ付けでは絶対に復元されない（category != 'manual' 保護の
        対象外になった＝AIが二度と生成し直さない領域だからこそ手動追加
        している）。誤操作による恒久的なデータ消失を防ぐため、manual
        タグの削除時のみ確認ダイアログを挟む。AI由来タグはしきい値調整
        等で作り直せるため、従来通り無確認即削除のままでよい。
        """
        if self._current_image_id is None or self._current_image_id < 0:
            return

        if category == "manual":
            reply = QMessageBox.question(
                self,
                "手動タグの削除",
                f"手動で追加したタグ「{tag.replace('_', ' ')}」を削除します。\n"
                "AI由来のタグと異なり、この操作は再タグ付けでは復元できません。\n"
                "よろしいですか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,  # 既定はNo（誤操作防止）
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            conn.execute(
                "DELETE FROM tags WHERE image_id = ? AND tag = ?",
                (self._current_image_id, tag),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"タグの削除に失敗しました: {e}")
            return
        self._refresh_after_tag_change()

    def _refresh_after_tag_change(self) -> None:
        """
        タグの追加・削除後、既存の取得経路（load_tags_for/_on_tags_fetched）
        を再利用して即座に再取得・再描画する。無理に新しい取得経路は作らない。
        あわせてメインウィンドウ側（サムネイル一覧・タグ集計ペイン）にも
        再検索を依頼し、追加/削除したタグが即座に反映されるようにする。
        """
        image_id = self._current_image_id
        # load_tags_for() は「同じimage_idなら何もしない」ガードを持つため、
        # 一度 None に戻してから同じIDで呼び直すことで強制的に再取得させる。
        self._current_image_id = None
        self.load_tags_for(image_id)

        mw = self._main_window
        if mw is not None and hasattr(mw, "trigger_search"):
            try:
                mw.trigger_search()
            except Exception:
                pass


