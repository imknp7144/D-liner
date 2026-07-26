"""
sdi_image_label.py — SDIウィンドウ内の画像描画コンポーネント
======================================================================
セッション27〜29の高速化・保守性検討（候補2・第2段階）により、
sdi_window_viewer.py から機械的に分離。

【重要】このファイルは sdi_window_viewer.py からのクラス定義の「移動」のみを
目的としており、ロジックの変更は一切行っていない（メソッド本文は1文字も
変えていない）。

含まれるクラス:
    SDIImageLabel  SDIウインドウ内での画像描画コンポーネント
                   （指定された表示モード・補間モードで拡大縮小や回転・反転等の
                   変形をリアルタイム反映する）
"""

from __future__ import annotations

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


class SDIImageLabel(QLabel):
    """
    SDIウインドウ内での画像描画コンポーネント。
    指定された表示モード・補間モードで拡大縮小や回転・反転等の変形をリアルタイム反映する。
    """
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(100, 100)
        
        self.raw_image: QImage | None = None
        self.display_image: QImage | None = None
        
        # 内部変形ステート
        self.scale_factor: float = 1.0
        self.rotation_angle: int = 0  # 0, 90, 180, 270
        self.flip_horizontal: bool = False
        self.flip_vertical: bool = False
        
        # 表示設定
        self.fit_mode: str = "smart"  # "raw", "window", "window_aspect", "width", "smart"
        self.interpolation_mode: str = "smooth"  # "fast", "smooth"

        # ビューポートサイズ取得用: SDIWindow.init_ui() で設定する
        self._scroll_area: QScrollArea | None = None
        self._sdi_window = None  # SDIWindow への参照（ウィンドウの内側サイズ取得用）
        
        self.setStyleSheet("background-color: #1a1a1a;")

    def set_raw_image(self, qimg: QImage) -> None:
        self.raw_image = qimg
        self.scale_factor = 1.0
        self.rotation_angle = 0
        self.flip_horizontal = False
        self.flip_vertical = False
        self.apply_transforms()

    def apply_transforms(self) -> None:
        if self.raw_image is None or self.raw_image.isNull():
            self.clear()
            return

        # 1. 回転・反転のアフィン変換の適用
        transform = QTransform()
        if self.flip_horizontal:
            transform.scale(-1, 1)
        if self.flip_vertical:
            transform.scale(1, -1)
        if self.rotation_angle != 0:
            transform.rotate(self.rotation_angle)
            
        transformed_img = self.raw_image.transformed(transform, Qt.TransformationMode.FastTransformation)
        self.display_image = transformed_img
        self.update_view()

    def _scaled_pixmap_letterboxed(
        self, content_w: int, content_h: int, canvas_w: int, canvas_h: int, trans_mode
    ) -> QPixmap:
        """
        画像を content_w x content_h にスケーリングし、それが
        canvas_w x canvas_h より小さい場合は黒背景キャンバスの中央に
        配置する（レターボックス）。極端に小さい画像を無理に
        canvas サイズまで拡大しないための共通処理。
        """
        scaled = QPixmap.fromImage(self.display_image).scaled(
            content_w, content_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            trans_mode,
        )
        if scaled.width() >= canvas_w and scaled.height() >= canvas_h:
            return scaled
        canvas = QPixmap(canvas_w, canvas_h)
        canvas.fill(Qt.GlobalColor.black)
        painter = QPainter(canvas)
        x = (canvas_w - scaled.width()) // 2
        y = (canvas_h - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
        painter.end()
        return canvas

    def update_view(self) -> None:
        if self.display_image is None:
            return

        w, h = self.display_image.width(), self.display_image.height()

        # 画像表示エリアの論理サイズを取得する。
        # centralWidget はコンテナ（scroll_area + tag_panel の親）になったため、
        # 画像エリアのサイズは scroll_area から直接取得する。
        # viewport() は setWidgetResizable(False) 時にラベルサイズに引き伸ばされる場合があるため
        # scroll_area 自体のサイズを使う。
        if self._sdi_window is not None:
            sa = getattr(self._sdi_window, "scroll_area", None)
            if sa is not None:
                parent_w = sa.width()
                parent_h = sa.height()
            else:
                cw = self._sdi_window.centralWidget()
                parent_w = cw.width()
                parent_h = cw.height()
        elif self._scroll_area is not None:
            parent_w = self._scroll_area.width()
            parent_h = self._scroll_area.height()
        else:
            parent_w, parent_h = 800, 600

        # QImage.width()/height() は物理ピクセル、centralWidget().width()/height() は論理ピクセル。
        # スケール計算を合わせるため、画像サイズを論理ピクセルに変換する。
        dpr = self.devicePixelRatio()
        if dpr <= 0:
            dpr = 1.0
        logical_w = w / dpr
        logical_h = h / dpr

        if self.fit_mode == "raw":
            # 原寸 or 手動ズーム: ラベルを画像サイズに広げてスクロールに任せる
            #
            # バグ修正: target_w/target_h は物理ピクセル基準で計算しているが、
            # 以前は QPixmap の devicePixelRatio を設定せず（デフォルト1.0の
            # まま）、self.resize() にもこの物理ピクセル値をそのまま渡して
            # いた。self.resize() は論理ピクセル単位で解釈されるため、
            # DPR=1.5環境では実際の表示サイズが画像サイズの1.5倍になり、
            # 「そのまま(100%)」のはずが原寸と一致しなくなっていた
            # （linar本家は非DPI対応で物理ピクセル=論理ピクセルとして
            # 動くため、この差が顕在化する）。
            # pixmap 自体は物理ピクセル解像度のまま保持しつつ、
            # devicePixelRatio を明示的に dpr に設定して「このpixmapの
            # 論理サイズは target_w/dpr である」とQtに伝え、ウィンドウ/
            # ラベルのリサイズは論理ピクセル値で行うことで、物理ピクセル
            # 1個=画面の物理ピクセル1個という真の等倍表示に一致させる。
            target_w = max(1, int(w * self.scale_factor))
            target_h = max(1, int(h * self.scale_factor))
            trans_mode = (
                Qt.TransformationMode.SmoothTransformation
                if self.interpolation_mode == "smooth"
                else Qt.TransformationMode.FastTransformation
            )
            scaled_pixmap = QPixmap.fromImage(self.display_image).scaled(
                target_w, target_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                trans_mode,
            )
            scaled_pixmap.setDevicePixelRatio(dpr)
            self.setPixmap(scaled_pixmap)
            logical_target_w = max(1, int(target_w / dpr))
            logical_target_h = max(1, int(target_h / dpr))
            self.resize(logical_target_w, logical_target_h)
            return

        if self.fit_mode == "smart":
            # ウィンドウを画像サイズ（論理px）に合わせてリサイズし、余白なし原寸表示。
            # 画像が画面をはみ出す場合はアスペクト比維持で縮小したサイズにウィンドウをリサイズ。
            # ウィンドウリサイズは _sdi_window 経由で行い、update_view は次のリサイズイベントで再呼出される。
            #
            # バグ修正: 以前はここで常に target_w/h = logical_w/h（画像の
            # フル論理サイズ）を使ってラベル/pixmapをリサイズしていた。
            # しかし _auto_resize_window_if_raw() 側では画面に収まらない
            # 場合アスペクト比維持で縮小した「表示サイズ」でウィンドウを
            # リサイズしていたため、ウィンドウは縮小されているのに
            # ラベル側は常にフルサイズを要求する不整合が生じ、
            # 「大きい画像のみ縮小」モードでスクロールバーが出てしまう
            # 原因になっていた。_auto_resize_window_if_raw() が実際に
            # 採用した表示サイズを返すようにし、ここではそれを使う。
            target_w = max(1, int(logical_w))
            target_h = max(1, int(logical_h))
            content_w, content_h = target_w, target_h
            if self._sdi_window is not None:
                # ウィンドウリサイズ後は resizeEvent → update_view が再度呼ばれるため
                # ここでは pixmap のセットだけ行う（サイズは次の update_view で決まる）
                result = self._sdi_window._auto_resize_window_if_raw(w, h)
                if result is None:
                    # バグ修正: result が None になるのは、再入防止ガード
                    # （resize()→resizeEvent()→update_view() の再帰呼び出し
                    # 中）でブロックされたケース（fit_mode は既にこの分岐
                    # 内なので "smart" 以外を理由に None になることはない）。
                    # ここでフォールバック値（画像の生サイズそのまま、
                    # 最小表示サイズの床を考慮していない）で pixmap を
                    # 更新してしまうと、外側の呼び出しが完了する前に
                    # 誤ったサイズ・レターボックス無しの状態で上書きして
                    # しまう不具合があった（極小画像でのレターボックスが
                    # ナビゲーション時に効かなくなる）。何もせず抜け、
                    # 外側の呼び出し（既に実行中）が正しい最終状態に
                    # 確定させるのに任せる。
                    return
                target_w, target_h, content_w, content_h = result
            trans_mode = (
                Qt.TransformationMode.SmoothTransformation
                if self.interpolation_mode == "smooth"
                else Qt.TransformationMode.FastTransformation
            )
            # バグ修正: 極端に小さい画像（例: 32x32アイコン）は
            # target_w/h（ウィンドウの表示エリア全体＝最小表示サイズの
            # 床で下限が保証されたサイズ）まで無理に引き伸ばさず、
            # content_w/h（画像本来の表示サイズ）のまま黒背景キャンバスの
            # 中央に配置する（レターボックス）。content_w/h == target_w/h
            # の通常サイズの画像では、これまで通り単純なスケーリングと
            # 完全に同じ結果になる。
            self.setPixmap(
                self._scaled_pixmap_letterboxed(content_w, content_h, target_w, target_h, trans_mode)
            )
            self.resize(target_w, target_h)

            # 安全策（最終防御）: ここまでの計算（_auto_resize_window_if_raw
            # のフレーム/タグパネル高さ推定、非同期タグ取得完了タイミング等）
            # に何らかのズレが残っていても、smart モードは本来
            # 「常にウィンドウ内に収まりスクロールしない」ことが設計意図
            # そのものである。実機（実OSのウィンドウマネージャ）では、
            # ウィンドウ枠の適用とタグ取得完了の非同期処理がまれに競合し、
            # 上記の計算だけでは吸収しきれない数px単位のズレが残ることが
            # あるため、実際に確定している scroll_area 自体のサイズ
            # （親レイアウトにより同期的に確定済み。QScrollArea側の
            # スクロールバー要否判定・viewport()サイズはレイアウト更新が
            # 遅延しうるため参照しない）と直接比較し、収まっていなければ
            # 追加で縮小する。原因推定に依存しない、症状（スクロールバー）
            # そのものへの対処。
            sa = self._sdi_window.scroll_area if self._sdi_window is not None else self._scroll_area
            if sa is not None:
                avail_w = sa.width()
                avail_h = sa.height()
                if (avail_w > 0 and avail_h > 0
                        and (target_w > avail_w or target_h > avail_h)):
                    scale = min(avail_w / target_w, avail_h / target_h)
                    scale = max(0.01, min(1.0, scale))
                    fallback_w = max(1, int(target_w * scale))
                    fallback_h = max(1, int(target_h * scale))
                    if (fallback_w, fallback_h) != (target_w, target_h):
                        fallback_content_w = max(1, int(content_w * scale))
                        fallback_content_h = max(1, int(content_h * scale))
                        self.setPixmap(
                            self._scaled_pixmap_letterboxed(
                                fallback_content_w, fallback_content_h,
                                fallback_w, fallback_h, trans_mode,
                            )
                        )
                        self.resize(fallback_w, fallback_h)
            return

        # --- ウィンドウに収めるモード共通 ---
        # ラベルはビューポートいっぱいに広げ、pixmap だけをスケーリングする。
        # self.resize() を呼ばないことで setWidgetResizable(True) との競合を防ぐ。

        if self.fit_mode == "window":
            target_w = parent_w
            target_h = parent_h
            aspect = Qt.AspectRatioMode.IgnoreAspectRatio
        elif self.fit_mode == "window_aspect":
            scale = min(parent_w / logical_w, parent_h / logical_h)
            target_w = int(logical_w * scale)
            target_h = int(logical_h * scale)
            aspect = Qt.AspectRatioMode.KeepAspectRatio
        elif self.fit_mode == "width":
            scale = parent_w / logical_w
            target_w = int(logical_w * scale)
            target_h = int(logical_h * scale)
            aspect = Qt.AspectRatioMode.KeepAspectRatio
        else:  # "smart": 画像が論理ビューポートより大きければ縮小、小さければ原寸
            if logical_w > parent_w or logical_h > parent_h:
                scale = min(parent_w / logical_w, parent_h / logical_h)
                target_w = int(logical_w * scale)
                target_h = int(logical_h * scale)
            else:
                target_w = int(logical_w)
                target_h = int(logical_h)
            aspect = Qt.AspectRatioMode.KeepAspectRatio

        target_w = max(1, target_w)
        target_h = max(1, target_h)

        trans_mode = (
            Qt.TransformationMode.SmoothTransformation
            if self.interpolation_mode == "smooth"
            else Qt.TransformationMode.FastTransformation
        )
        scaled_pixmap = QPixmap.fromImage(self.display_image).scaled(
            target_w, target_h, aspect, trans_mode
        )
        self.setPixmap(scaled_pixmap)
        # ラベルサイズはビューポートに任せる（resize しない）
