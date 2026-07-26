from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QObject, QRunnable


def _escape_like_prefix(folder_path: str) -> str:
    """
    フォルダパスを SQLite の LIKE 前方一致パターン用にエスケープする。
    末尾の "/%" は呼び出し側で付与すること。
    GLOBの*?[...]による誤マッチを避けるため、フォルダ検索は全てこれを経由する。
    """
    fp = folder_path.rstrip("/")
    return (
        fp.replace("\\", "\\\\")
          .replace("%", "\\%")
          .replace("_", "\\_")
    )

# 有効なソート用キーのホワイトリスト
VALID_SORT_KEYS = {
    "path":       "i.path",
    "filesize":   "i.filesize",
    "width":      "i.width",
    "height":     "i.height",
    "resolution": "(i.width * i.height)",
    "added":      "i.added_at",
}

def _linar_db_path() -> str:
    """linar.db の絶対パスを返す（相対パス接続によるDB不一致を防ぐ）"""
    return str(Path(__file__).parent / "linar.db")


# ---------------------------------------------------------------------------
# タグ整形・共通定数（指示書03: SDIタグパネルのコピー/類似検索モード）
# ---------------------------------------------------------------------------

# tag_panel.py TagPanel.CATEGORY_ORDER と値を同期させること（表示順序の
# 一貫性のため）。TagPanel経由の呼び出し（コピーモード）はTagPanel側で既に
# この順に並べ替え済みのリストを渡してくるが、LoraExportWorkerのようにDBから
# 直接取得したタグリストを format_tags_for_copy() に渡す場合は、ここで明示的に
# 並べ替えないと DB の取得順（保証なし）のまま出力されてしまう。
CATEGORY_ORDER_FOR_TAG_EXPORT: list[str] = [
    "manual", "character", "copyright", "artist", "general", "meta", "rating",
]


def sort_tags_by_category_order(
    tags: list[tuple[str, str]],
    order: list[str] = CATEGORY_ORDER_FOR_TAG_EXPORT,
) -> list[tuple[str, str]]:
    """
    (tag, category) のリストを order の並び順に安定ソートする。
    order に無いカテゴリは末尾に回す。同一カテゴリ内の順序は元のリストの
    順序を維持する（Pythonのsortは安定ソートのため）。
    """
    order_index = {cat: i for i, cat in enumerate(order)}
    fallback = len(order)
    return sorted(tags, key=lambda tc: order_index.get(tc[1], fallback))


def format_tags_for_copy(
    tags: list[tuple[str, str]],   # (tag, category)
    exclude_categories: tuple[str, ...] = ("rating", "meta"),
    use_underscore: bool = True,
) -> str:
    """
    タグリストをカンマ区切り文字列に整形する（ComfyUI等への貼り付け用途）。

    exclude_categories に含まれるカテゴリのタグは除外する。
    use_underscore=False の場合はアンダースコアをスペースに変換する（表示用）。

    出力順は呼び出し側が渡した tags の順序をそのまま維持する
    （tag_panel.py TagPanel 側で CATEGORY_ORDER 順に並べ替え済みの
    リストを渡す想定のため、ここでは並べ替えを行わない）。
    """
    filtered = [t for t, cat in tags if cat not in exclude_categories]
    if not use_underscore:
        filtered = [t.replace("_", " ") for t in filtered]
    return ", ".join(filtered)


# サムネイル右クリック「似たタグの画像を探す」（指示書03 タスクD）で、
# 抽出対象から除外する汎用タグ。人数・視線構図など、ほぼ全てのDanbooru風
# タグ付き画像に付与されるため、除外しないと実質フィルタとして機能しない。
# 比較する側（呼び出し元）は、対象タグ・このリスト双方を
# .lower().replace(" ", "_") で正規化してから比較すること
# （手動タグは大文字・スペース区切りの表記揺れがあり得るため）。
GENERIC_TAGS_FOR_SIMILARITY_SEARCH: frozenset[str] = frozenset({
    "1girl", "1boy", "2girls", "2boys", "3girls", "3boys",
    "multiple_girls", "multiple_boys",
    "solo", "solo_focus",
    "male_focus", "female_focus",
    "looking_at_viewer",
})


class SearchWorker(QThread):
    """
    登録フォルダ用 DB 検索ワーカー。

    検索トークン（スペース区切り）は AND 結合。
    各トークンを以下の OR 条件で評価する:
      1. tags テーブルに完全一致するタグが存在する（タグ検索）
         ※ スペース↔アンダースコアの表記ゆれを正規化
      2. images.path のファイル名部分に部分一致する（ファイル名検索）

    例: "izumi 20250102 swimsuit"
      → izumi(ファイル名) AND 20250102(ファイル名) AND swimsuit(タグ) を全て満たす画像
      日付+連番のファイル命名規則と Danbooru タグを組み合わせた絞り込みを実現する。

    除外トークン: "-" で始まるワードはその条件を NOT で除外する（将来実装）。
    """
    finished = pyqtSignal(list)  # list[tuple] (id, path, width, height, filesize)
    error    = pyqtSignal(str)

    def __init__(
        self,
        folder_path: str = "",
        tag_query:   str = "",
        sort_key:    str = "path",
        sort_order:  str = "ASC",
        recursive:   bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.folder_path = folder_path.replace("\\", "/")
        self.tag_query   = tag_query.strip()
        self.sort_key    = sort_key
        self.sort_order  = "DESC" if sort_order.upper() == "DESC" else "ASC"
        self.recursive   = recursive

    @staticmethod
    def _normalize_tag(word: str) -> str:
        """スペース↔アンダースコアを統一してタグ検索用に正規化する"""
        return word.strip().lower().replace(" ", "_")

    def run(self) -> None:
        # バグ修正: 以前は conn.close() が複数箇所に散在しており、
        # cursor.execute(sql, params) 等が例外を投げると except 節に
        # 抜けて conn を閉じないままリークしていた（TagFetchWorker で
        # 採用済みの conn=None + finally close 方式に統一）。
        conn = None
        try:
            import lifecycle_manager as _lm
            conn   = _lm.get_connection()
            cursor = conn.cursor()

            sql = """
                SELECT DISTINCT i.id, i.path, i.width, i.height, i.filesize
                FROM images i
            """
            params: list = []
            conditions: list[str] = ["i.status = 'ACTIVE'"]

            # 1. フォルダ条件（選択フォルダ配下）
            # GLOBは * ? [...] をワイルドカードとして解釈するため、
            # ファイル名にこれらの文字（例: ブラウザ重複DLの "[1]"）が
            # 含まれると誤マッチ/不一致を起こす。LIKE + ESCAPE に変更し、
            # パス側の % _ \ をエスケープしてリテラル比較にする。
            if self.folder_path:
                fp_escaped = _escape_like_prefix(self.folder_path)
                conditions.append("LOWER(i.path) LIKE LOWER(?) ESCAPE '\\'")
                params.append(fp_escaped + "/%")
                if not self.recursive:
                    # バグ修正: 以前はrecursive設定を一切見ておらず常に
                    # 配下を再帰的に含めていたため、非recursive登録の
                    # フォルダでも、その中のサブフォルダが別途DB登録済み
                    # （＝DBにファイルが存在する）だと中身が漏れて表示
                    # されていた。深い階層のパスを明示的に除外する。
                    conditions.append("LOWER(i.path) NOT LIKE LOWER(?) ESCAPE '\\'")
                    params.append(fp_escaped + "/%/%")

            # 2. 検索トークン: タグ部分一致 OR ファイル名部分一致 の AND 結合
            if self.tag_query:
                tokens = [
                    w for w in self.tag_query.split()
                    if w and not w.startswith("-")
                ]
                excl_tokens = [
                    w[1:] for w in self.tag_query.split()
                    if len(w) > 1 and w.startswith("-")
                ]
                for word in tokens:
                    tag_norm = self._normalize_tag(word)
                    # タグ部分一致（LIKE）OR ファイル名部分一致
                    conditions.append("""(
                        EXISTS (
                            SELECT 1 FROM tags t
                            WHERE t.image_id = i.id
                              AND LOWER(t.tag) LIKE ?
                        )
                        OR LOWER(i.path) LIKE ?
                    )""")
                    params.append(f"%{tag_norm}%")
                    params.append(f"%/{word.lower()}%")

                # 除外トークン
                for word in excl_tokens:
                    tag_norm = self._normalize_tag(word)
                    conditions.append("""(
                        NOT EXISTS (
                            SELECT 1 FROM tags t
                            WHERE t.image_id = i.id
                              AND LOWER(t.tag) LIKE ?
                        )
                        AND LOWER(i.path) NOT LIKE ?
                    )""")
                    params.append(f"%{tag_norm}%")
                    params.append(f"%/{word.lower()}%")

            if conditions:
                sql += " WHERE " + " AND ".join(conditions)

            # 3. ソート（ホワイトリスト検証）
            # 自然順ソート（指示書08タスクA）: ファイル名(i.path)のソートのみ
            # COLLATE NATSORT を付与する。filesize/width/height/resolution/
            # added_at は数値列のため対象外（collationは文字列比較にのみ
            # 意味を持つ）。self.sort_key が未知の値でも VALID_SORT_KEYS.get()
            # のフォールバックで "i.path" になるため、キー名ではなく
            # マッピング後の列名で判定することで、フォールバック経路も
            # 取りこぼさないようにしている。
            mapped_sort_col = VALID_SORT_KEYS.get(self.sort_key, "i.path")
            if mapped_sort_col == "i.path":
                mapped_sort_col = "i.path COLLATE NATSORT"
            sql += f" ORDER BY {mapped_sort_col} {self.sort_order}"

            if self.isInterruptionRequested():
                return

            cursor.execute(sql, params)
            results = cursor.fetchall()

            if self.isInterruptionRequested():
                return

            # フォルダ指定がある場合、直下サブフォルダをOS直読みで先頭に付加する
            # （SearchWorkerはDBにあるファイルしか返さないため、フォルダ自体は別途取得）
            folder_entries: list[tuple] = []
            if self.folder_path:
                try:
                    with os.scandir(self.folder_path) as it:
                        subdirs = sorted(
                            [e for e in it if e.is_dir(follow_symlinks=False)],
                            key=lambda e: e.name.lower(),
                        )
                    folder_entries = [
                        (-2, e.path.replace("\\", "/"), 0, 0, 0)
                        for e in subdirs
                    ]
                except OSError:
                    pass

            self.finished.emit(folder_entries + results)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

class _ThumbnailTaskSignals(QObject):
    """
    QRunnable自体はQObjectではなくシグナルを持てないため、
    シグナル送出専用のヘルパーオブジェクトを分離している。
    """
    finished = pyqtSignal(str, object)  # path, QImage
    error = pyqtSignal(str, str)        # path, error_msg


class ThumbnailLoadTask(QRunnable):
    """
    サムネイル読み込みタスク（QThreadPool + QRunnable版）。

    バグ修正: 従来の ThumbnailWorker(QThread) は thumbnail_grid.py の
    _trigger_load() が呼ばれるたびに新規QThreadを生成しており、同時
    実行数の上限が無かった（ウィンドウリサイズ・大量スクロール等で
    多数のセルが一斉に可視化されると、その数だけOSネイティブスレッドが
    同時に起動しうる）。QThreadPoolで同時実行数を確定的に絞れる
    QRunnableベースに置き換えた。

    また、従来はキャンセルを isInterruptionRequested() で判定していた
    が、呼び出し側(thumbnail_grid.py)は requestInterruption() ではなく
    quit() を呼んでいた。run()を完全にオーバーライドしQThread内部の
    イベントループを使わないワーカーに対して quit() は事実上何もせず、
    キャンセルチェックが実質機能していなかった。ここでは呼び出し側と
    共有する cancel_flag（1要素リスト、[False]/[True]で共有）に置き
    換え、確実にキャンセルできるようにしている。
    """

    def __init__(self, path: str, target_size: int, cancel_flag: list[bool]) -> None:
        super().__init__()
        self.path = path
        self.target_size = target_size
        self._cancel_flag = cancel_flag
        self.signals = _ThumbnailTaskSignals()
        self.setAutoDelete(True)

    def _cancelled(self) -> bool:
        return self._cancel_flag[0]

    def run(self) -> None:
        from PyQt6.QtGui import QImage, QImageReader
        from PyQt6.QtCore import QSize as _QSize
        try:
            # フォルダ切り替え・スクロールアウト時に旧タスクを即無効化するための
            # キャンセルチェック
            if self._cancelled():
                return

            reader = QImageReader(self.path)
            reader.setAutoTransform(True)
            # ComfyUI埋め込みワークフローJSON等の巨大メタデータを持つ
            # PNG/WebPでQImageReaderがデフォルト128MB上限で失敗するのを防ぐ。
            # 0 = 上限なし（Qtドキュメント準拠）
            reader.setAllocationLimit(0)

            orig_size = reader.size()
            if orig_size.isValid() and orig_size.width() > 0 and orig_size.height() > 0:
                w, h = orig_size.width(), orig_size.height()
                scale = min(self.target_size / w, self.target_size / h)
                if scale < 1.0:
                    new_w = max(1, int(w * scale))
                    new_h = max(1, int(h * scale))
                    reader.setScaledSize(_QSize(new_w, new_h))
            # else: サイズ取得失敗 → フルデコード後リサイズにフォールバック

            if self._cancelled():
                return

            qimg = reader.read()
            if qimg.isNull():
                # QImageReader失敗時はQImage直接ロードで再試行
                qimg = QImage(self.path)
            if qimg.isNull():
                raise ValueError(f"読み込み失敗: {reader.errorString()}")

            # フルサイズでロードされた場合はリサイズ
            if qimg.width() > self.target_size or qimg.height() > self.target_size:
                qimg = qimg.scaled(
                    _QSize(self.target_size, self.target_size),
                    1,  # Qt.AspectRatioMode.KeepAspectRatio
                    1,  # Qt.TransformationMode.SmoothTransformation
                )

            if not self._cancelled():
                self.signals.finished.emit(self.path, qimg)
        except Exception as e:
            if not self._cancelled():
                self.signals.error.emit(self.path, str(e))


class PreviewWorker(QThread):
    finished = pyqtSignal(object)  # QImage
    error = pyqtSignal(str)

    def __init__(self, path: str, max_width: int, max_height: int, parent=None) -> None:
        super().__init__(parent)
        self.path = path
        self.max_width = max(max_width, 10)
        self.max_height = max(max_height, 10)

    def run(self) -> None:
        from PyQt6.QtGui import QImage, QImageReader
        try:
            reader = QImageReader(self.path)
            reader.setAutoTransform(True)
            orig_size = reader.size()
            if orig_size.isValid():
                w, h = orig_size.width(), orig_size.height()
                scale = min(self.max_width / w, self.max_height / h)
                if scale < 1.0:
                    reader.setScaledSize(orig_size * scale)
            qimg = reader.read()
            if qimg.isNull():
                raise ValueError("プレビュー画像の読み込みに失敗しました")
            self.finished.emit(qimg)
        except Exception as e:
            self.error.emit(str(e))

class TagFetchWorker(QThread):
    """
    単一画像のタグを取得するワーカー。

    finished シグナル: (image_id, list[tuple[str, str]])
      内部リストの各要素は (tag, category) のタプル。
      category は Danbooru 準拠の文字列
      ("general" / "character" / "copyright" / "artist" / "rating" / "meta")。
    """
    finished = pyqtSignal(tuple)  # (image_id, list[tuple[str, str]])
    error = pyqtSignal(str)

    # Danbooru カテゴリ番号 → 文字列マッピング
    _CAT_MAP: dict[int, str] = {
        0: "general",
        1: "artist",
        3: "copyright",
        4: "character",
        5: "meta",
    }

    def __init__(self, image_id: int, parent=None) -> None:
        super().__init__(parent)
        self.image_id = image_id

    def run(self) -> None:
        conn = None
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tag, category FROM tags WHERE image_id = ? ORDER BY category, tag",
                (self.image_id,),
            )
            tags: list[tuple[str, str]] = [
                (row[0], row[1] if row[1] else "general")
                for row in cursor.fetchall()
            ]
            self.finished.emit((self.image_id, tags))
        except Exception as e:
            self.error.emit(str(e))
        finally:
            # バグ修正: 例外発生時にconn.close()がスキップされコネクション
            # リークしていた（他のWorkerはfinallyでclose済み）。積み重なると
            # SQLiteの database is locked を誘発するため統一。
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

class LifecycleSyncWorker(QThread):
    """
    起動時チェック（startup_check）用の非同期バックグラウンド実行ワーカ。
    """
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def run(self) -> None:
        # バグ修正: ensure_schema()/sync_lifecycle()が例外を投げると
        # conn.close()に到達せずリークしていた（conn=None+finally方式に統一）。
        conn = None
        try:
            import lifecycle_manager
            conn = lifecycle_manager.get_connection()
            lifecycle_manager.ensure_schema(conn)
            res = lifecycle_manager.sync_lifecycle(conn)
            self.finished.emit(res)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

class FilesystemSearchWorker(QThread):
    """
    DB未登録フォルダ用。OSのファイルシステムを直接読んで画像一覧を返す。
    タグ検索は無視し、ファイル名のみで絞り込む。
    返す tuple は SearchWorker と同形式: (id, path, width, height, filesize)
    id は -1（DB未登録のダミー値）。
    """
    finished = pyqtSignal(list)
    error    = pyqtSignal(str)

    IMAGE_EXTENSIONS = {
        ".jpg", ".jpeg", ".png", ".webp", ".gif",
        ".bmp", ".tiff", ".tif", ".avif",
    }

    def __init__(
        self,
        folder_path: str,
        name_filter: str = "",
        sort_key: str = "path",
        sort_order: str = "ASC",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.folder_path = folder_path.replace("\\", "/").rstrip("/")
        self.name_filter = name_filter.strip().lower()
        self.sort_key    = sort_key
        self.sort_order  = "DESC" if sort_order.upper() == "DESC" else "ASC"

    def run(self) -> None:
        try:
            import lifecycle_manager as _lm

            if not os.path.isdir(self.folder_path):
                self.finished.emit([])
                return

            subdirs = []
            files   = []
            exts    = self.IMAGE_EXTENSIONS

            # os.scandir を使用してOSレベルのシステムコールと属性走査を高速化
            with os.scandir(self.folder_path) as it:
                for entry in it:
                    if self.isInterruptionRequested():
                        self.finished.emit([])
                        return
                    # entry.is_dir / is_file は追加の stat コールを発生させない（Windows/Linux）
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry)
                    elif entry.is_file(follow_symlinks=False):
                        # 拡張子チェック
                        _, ext = os.path.splitext(entry.name)
                        if ext.lower() in exts:
                            files.append(entry)

            # フォルダは名前順でソート
            subdirs.sort(key=lambda e: e.name.lower())
            folder_entries = [(-2, entry.path.replace("\\", "/"), 0, 0, 0) for entry in subdirs]

            # ファイル名フィルタ
            if self.name_filter:
                tokens = [t for t in self.name_filter.split() if t and not t.startswith("-")]
                for token in tokens:
                    files = [e for e in files if token in e.name.lower()]

            # ソート: ディレクトリ走査時のキャッシュされた stat() 情報を利用
            if self.sort_key in ("filesize", "added"):
                def stat_key(e):
                    try:
                        st = e.stat()
                        return st.st_size if self.sort_key == "filesize" else st.st_mtime
                    except OSError:
                        return 0
                files.sort(key=stat_key, reverse=(self.sort_order == "DESC"))
            else:
                # 自然順ソート（指示書08タスクA）: lifecycle_manager と同じ
                # natural_sort_key を使い、登録フォルダ側(SearchWorker)との
                # ソート挙動の一貫性を保つ。
                files.sort(key=lambda e: _lm.natural_sort_key(e.name), reverse=(self.sort_order == "DESC"))

            # パス文字列をスラッシュ統一
            # ファイルサイズはos.scandir()でキャッシュ済みのstat情報から取得
            # （Windows/Linuxとも追加のシステムコールなしで取れる）。
            # 解像度(width/height)は「仕様として据え置き」と決着済み：
            # 未登録フォルダの一覧では画像デコードを伴うためコスト増になり、
            # DB登録済みフォルダとの一貫性のためにも0のままとする。
            def _entry_filesize(e) -> int:
                try:
                    return e.stat().st_size
                except OSError:
                    return 0

            image_entries = [
                (-1, entry.path.replace("\\", "/"), 0, 0, _entry_filesize(entry))
                for entry in files
            ]

            self.finished.emit(folder_entries + image_entries)
        except Exception as e:
            self.error.emit(str(e))


class TaggerWorker(QThread):
    """
    バックグラウンドでタグ付けを実行するワーカー。

    TaggerEngine（接続済みであること）を受け取り、
    image_paths のリストを順番に処理して DB に保存する。

    シグナル:
        progress(int, int, str)  現在件数, 総件数, 処理中パス
        finished(int, int)       完了件数, スキップ件数
        error(str)               致命的エラーメッセージ
    """
    progress = pyqtSignal(int, int, str)   # current, total, path
    finished = pyqtSignal(int, int)        # tagged, skipped
    error    = pyqtSignal(str)

    # D-liner DB に保存するカテゴリ（meta / year は除外）
    SAVE_CATEGORIES = ("general", "character", "copyright", "artist", "rating")

    def __init__(
        self,
        engine,                        # TaggerEngine
        image_paths: list[tuple],      # list of (image_id, path)
        model: str = "wd14",
        device: str = "NPU",
        threshold: float = 0.35,
        threshold_character: float = 0.75,
        threshold_copyright: float = 0.50,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.engine               = engine
        self.image_paths          = image_paths
        self.model                = model
        self.device               = device
        self.threshold            = threshold
        self.threshold_character  = threshold_character
        self.threshold_copyright  = threshold_copyright

    def run(self) -> None:
        import lifecycle_manager as _lm
        import traceback

        tagged  = 0
        skipped = 0
        total   = len(self.image_paths)

        try:
            conn = _lm.get_connection()
        except Exception as e:
            self.error.emit(f"DB接続失敗: {e}")
            return

        try:
            for idx, (image_id, path) in enumerate(self.image_paths):
                if self.isInterruptionRequested():
                    break

                self.progress.emit(idx + 1, total, path)

                # Worker にタグ付け依頼
                try:
                    result = self.engine.tag(
                        image_path=path,
                        model=self.model,
                        device=self.device,
                        threshold=self.threshold,
                        threshold_character=self.threshold_character,
                        threshold_copyright=self.threshold_copyright,
                        replace_underscores=False,
                    )
                except Exception as e:
                    print(f"[TaggerWorker] tag() error for {path}: {e}", flush=True)
                    skipped += 1
                    continue

                if result is None or result.get("status") != "ok":
                    err_msg = result.get("error", "unknown") if result else "no response"
                    print(f"[TaggerWorker] tag failed for {path}: {err_msg}", flush=True)
                    skipped += 1
                    continue

                # カテゴリ別にタグを展開して DB に保存
                try:
                    cursor = conn.cursor()
                    # 手動タグ保護: 再タグ付けのたびに手動追加タグ(category='manual')
                    # まで無条件削除していたため、手動タグが毎回消える不具合があった。
                    # AI由来カテゴリのみを削除対象とする（指示書02 タスクA-3）。
                    cursor.execute(
                        "DELETE FROM tags WHERE image_id = ? AND category != 'manual'",
                        (image_id,),
                    )

                    rows = []
                    for cat in self.SAVE_CATEGORIES:
                        cat_key = f"{cat}_tags"
                        tag_str = result.get(cat_key, "").strip()
                        if not tag_str:
                            continue
                        for tag in tag_str.split(","):
                            tag = tag.strip()
                            if tag:
                                rows.append((image_id, tag, cat))

                    if rows:
                        cursor.executemany(
                            "INSERT OR IGNORE INTO tags (image_id, tag, category) VALUES (?, ?, ?)",
                            rows,
                        )

                    conn.commit()
                    tagged += 1

                except Exception as e:
                    skipped += 1
                    print(f"[TaggerWorker] DB write error for {path}: {e}", flush=True)
                    continue

        except Exception as e:
            # for ループ外の予期しない例外 → エラーシグナルを出してスレッドを安全に終了
            print(f"[TaggerWorker] Fatal error:\n{traceback.format_exc()}", flush=True)
            self.error.emit(f"タグ付け処理エラー: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        self.finished.emit(tagged, skipped)


class TagListWorker(QThread):
    """
    現在の検索結果（image_id リスト）に対してタグ集計を行う。

    一時テーブル方式で SQLite の IN 句 999 件上限を回避。
    戻り値: list[(tag, category, count)]  count 降順・同数はアルファベット順
    """
    finished = pyqtSignal(list)
    error    = pyqtSignal(str)

    def __init__(self, image_ids: list, parent=None) -> None:
        super().__init__(parent)
        self.image_ids = image_ids

    def run(self) -> None:
        # バグ修正: 一時テーブル操作やJOINクエリが例外を投げると
        # conn.close()に到達せずリークしていた（conn=None+finally方式に統一）。
        conn = None
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()

            if not self.image_ids or self.isInterruptionRequested():
                self.finished.emit([])
                return

            # 一時テーブルに image_id を流し込む（IN 句 999 件上限を回避）
            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _tl_ids (id INTEGER PRIMARY KEY)"
            )
            conn.execute("DELETE FROM _tl_ids")
            conn.executemany(
                "INSERT OR IGNORE INTO _tl_ids VALUES (?)",
                [(i,) for i in self.image_ids],
            )

            cursor = conn.cursor()
            cursor.execute("""
                SELECT t.tag, t.category, COUNT(*) AS cnt
                FROM tags t
                INNER JOIN _tl_ids tmp ON t.image_id = tmp.id
                GROUP BY t.tag, t.category
                ORDER BY cnt DESC, t.tag ASC
            """)
            results = cursor.fetchall()

            conn.execute("DROP TABLE IF EXISTS _tl_ids")

            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass



class TaggerConnectWorker(QThread):
    """
    TaggerEngine.connect_or_launch() をバックグラウンドで実行するワーカー。

    connect_or_launch() は worker.py の起動を最大60秒待つため、
    メインスレッドで呼ぶと UI がフリーズする。このワーカー経由で非同期化する。

    シグナル:
        succeeded(str)  接続成功。引数はモード文字列（"piggyback"/"standalone"）
        failed()        接続失敗
    """
    succeeded = pyqtSignal(str)   # mode
    failed    = pyqtSignal()

    def __init__(self, engine, parent=None) -> None:
        super().__init__(parent)
        self._engine = engine

    def run(self) -> None:
        try:
            ok = self._engine.connect_or_launch()
            if ok:
                self.succeeded.emit(self._engine.mode)
            else:
                self.failed.emit()
        except Exception as e:
            print(f"[TaggerConnectWorker] error: {repr(e)}", flush=True)
            self.failed.emit()


class BackgroundTaggerWorker(QThread):
    """
    未タグ付け画像をバックグラウンドで順次タグ付けするワーカー。

    v2再設計（サムネイル・タグ付けトリガー再設計）対応:
      scope='current' — 選択中フォルダ全体が対象（優先度②）。
                        folder_path必須。recursiveはwatched_foldersの
                        設定に従う（未登録フォルダはFalse固定）。
      scope='other'   — 選択中フォルダ以外の全画像が対象（優先度④・アイドル時のみ）。
                        folder_pathは「除外するフォルダ」として使う
                        （Noneならフィルタなし＝DB全体）。

    TaggerWorker（手動）との違い:
      - 優先度低（低負荷で常時稼働）
      - QProgressDialog を出さず、シグナルでステータスバーに進捗を通知
      - isInterruptionRequested() を各画像の前後でチェックして中断可能
      - 完了後も finished を emit せず、呼び出し元が _on_bg_finished で後処理

    シグナル:
      progress(current, total, path)  処理中の状況
      finished(tagged, skipped)       全件処理完了（中断時も emit）
      error(str)                      致命的エラー
      queue_empty()                   未タグ付け画像がゼロだった
    """

    progress    = pyqtSignal(int, int, str)   # current, total, path
    finished    = pyqtSignal(int, int)         # tagged, skipped
    error       = pyqtSignal(str)
    queue_empty = pyqtSignal()

    # TaggerWorker と共通のカテゴリセット
    SAVE_CATEGORIES = ("general", "character", "copyright", "artist", "rating")

    def __init__(
        self,
        engine,                         # TaggerEngine（接続済み）
        model:               str   = "wd14",
        device:              str   = "NPU",
        threshold:           float = 0.35,
        threshold_character: float = 0.75,
        threshold_copyright: float = 0.50,
        folder_path:         str | None = None,
        recursive:           bool = True,
        scope:               str  = "current",   # "current" | "other"
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.engine               = engine
        self.model                = model
        self.device               = device
        self.threshold            = threshold
        self.threshold_character  = threshold_character
        self.threshold_copyright  = threshold_copyright
        self.folder_path          = folder_path
        self.recursive            = recursive
        self.scope                = scope

    # ------------------------------------------------------------------
    # メインスレッド（QThread.run）
    # ------------------------------------------------------------------

    def run(self) -> None:
        import lifecycle_manager as _lm
        import traceback

        # 実際の接続を確認してから開始（is_available は軽量チェックのみ）
        if not self.engine.check_alive():
            print("[BGTagger] Worker に接続できません。スキップします。", flush=True)
            self.finished.emit(0, 0)
            return

        try:
            conn = _lm.get_connection()
        except Exception as e:
            self.error.emit(f"DB接続失敗: {e}")
            return

        try:
            targets = self._fetch_untagged(conn)
        except Exception as e:
            conn.close()
            self.error.emit(f"未タグ付けリスト取得失敗: {e}")
            return

        if not targets:
            conn.close()
            self.queue_empty.emit()
            return

        total           = len(targets)
        tagged          = 0
        skipped         = 0
        consecutive_err = 0   # 連続エラーカウント（接続断を検出するため）
        MAX_CONSECUTIVE = 5   # これを超えたら接続断とみなし中断

        try:
            for idx, (image_id, path) in enumerate(targets):
                if self.isInterruptionRequested():
                    break

                self.progress.emit(idx + 1, total, path)

                # --- 推論 ---
                try:
                    result = self.engine.tag(
                        image_path=path,
                        model=self.model,
                        device=self.device,
                        threshold=self.threshold,
                        threshold_character=self.threshold_character,
                        threshold_copyright=self.threshold_copyright,
                        replace_underscores=False,
                    )
                except Exception as e:
                    print(f"[BGTagger] tag() error {path}: {e}", flush=True)
                    skipped += 1
                    consecutive_err += 1
                    if consecutive_err >= MAX_CONSECUTIVE:
                        print(f"[BGTagger] 連続エラー {consecutive_err} 件。接続断とみなし中断します。", flush=True)
                        break
                    continue

                if result is None or result.get("status") != "ok":
                    err = result.get("error", "unknown") if result else "no response"
                    err_type = result.get("error_type") if result else None

                    if err_type == "image_error":
                        # バグ修正: 破損ファイル・巨大画像等、その画像固有の
                        # 恒久的なエラーは接続断とは無関係。ここで
                        # consecutive_err を増やして「5件連続で接続断」と
                        # 誤判定すると、無関係な残り画像の処理まで巻き添えで
                        # 打ち切られてしまう。tag_failed フラグをDBに立てて
                        # 二度とキューに戻らないようにし、カウントは増やさず
                        # 次の画像へ進める。
                        print(f"[BGTagger] tag failed (permanent) {path}: {err}", flush=True)
                        try:
                            conn.execute(
                                "UPDATE images SET tag_failed = 1 WHERE id = ?",
                                (image_id,),
                            )
                            conn.commit()
                        except Exception as e:
                            print(f"[BGTagger] tag_failed更新失敗 {path}: {e}", flush=True)
                        skipped += 1
                        continue

                    print(f"[BGTagger] tag failed {path}: {err}", flush=True)
                    skipped += 1
                    consecutive_err += 1
                    if consecutive_err >= MAX_CONSECUTIVE:
                        print(f"[BGTagger] 連続エラー {consecutive_err} 件。接続断とみなし中断します。", flush=True)
                        break
                    continue

                consecutive_err = 0  # 成功したらリセット

                # --- DB 保存 ---
                try:
                    cursor = conn.cursor()
                    # 手動タグ保護（指示書02 タスクA-2、TaggerWorker側と同型修正）
                    cursor.execute(
                        "DELETE FROM tags WHERE image_id = ? AND category != 'manual'",
                        (image_id,),
                    )
                    rows = []
                    for cat in self.SAVE_CATEGORIES:
                        tag_str = result.get(f"{cat}_tags", "").strip()
                        for tag in tag_str.split(","):
                            tag = tag.strip()
                            if tag:
                                rows.append((image_id, tag, cat))
                    if rows:
                        cursor.executemany(
                            "INSERT OR IGNORE INTO tags "
                            "(image_id, tag, category) VALUES (?, ?, ?)",
                            rows,
                        )
                    conn.commit()
                    tagged += 1
                except Exception as e:
                    print(f"[BGTagger] DB write error {path}: {e}", flush=True)
                    skipped += 1

                # v2指示書6章 対策2: アプリレベルのバックプレッシャー。
                # ①(表示読み込み)の要求をOS待ち行列で埋もれさせないよう、
                # 1件処理するごとに小休止を挟んでディスクI/Oに隙間を作る。
                # scope='other'(④・アイドル時のみ動く最低優先度)は、より
                # 積極的に間引く（v2指示書3-3「気配りだが不可欠ではない」
                # に準じ、他フォルダ処理は①②③より遠慮する）。
                self.msleep(80 if self.scope == "other" else 20)

        except Exception:
            print(f"[BGTagger] Fatal:\n{traceback.format_exc()}", flush=True)
            self.error.emit("バックグラウンドタグ付けで予期しないエラーが発生しました")
            # バグ修正: 以前はここで return していなかったため、finally を
            # 抜けた後に self.finished.emit(...) にも到達し、error と
            # finished の両シグナルが発火していた。_on_bg_error /
            # _on_bg_finished の両方が _bg_restart_pending を見て再始動
            # 処理を行うため、条件が揃うとワーカーが二重起動しうる実害バグ
            # だった。BackgroundThumbWorker 側の同種修正と揃えて、致命的
            # エラー時は finished を emit せず return する
            # （finally は return 時も実行されるので conn.close() は保証される）。
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

        self.finished.emit(tagged, skipped)

    def _fetch_untagged(self, conn) -> list[tuple[int, str]]:
        """
        登録済み（ACTIVE）かつタグが1件もない画像を返す。
        ファイルが実在するものだけを対象とする。

        scope='current': folder_path配下（recursiveに従う）に限定。
        scope='other'  : folder_path配下"以外"の全画像。folder_pathが
                         Noneの場合はDB全体（＝現在フォルダ未選択時の④）。
        """
        cursor = conn.cursor()
        conditions = [
            "i.status = 'ACTIVE'",
            # 指示書02 タスクA-4: 手動タグ(category='manual')だけが付いている
            # 画像は「AIタグ付け済み」ではないため、AIタグの有無だけで判定する。
            # これが無いと、手動タグのみ付与された画像が「タグ済み」と誤認され、
            # 永久に自動タグ付けキューに入らなくなる。
            "NOT EXISTS (SELECT 1 FROM tags t WHERE t.image_id = i.id AND t.category != 'manual')",
            # バグ修正: 恒久的に失敗した画像（破損ファイル等）を除外。
            # これが無いと同じ画像が毎回キューに戻ってきて無限に失敗を
            # 繰り返す。
            "i.tag_failed = 0",
            # ロックボタンで明示的に除外された画像は対象にしない
            # （sdi_window_viewer.py SDIWindow._on_lock_btn_clicked()参照）。
            "i.ai_tagging_suppressed = 0",
        ]
        params: list = []

        if self.folder_path:
            fp_escaped = _escape_like_prefix(self.folder_path)
            if self.scope == "current":
                conditions.append("i.path LIKE ? ESCAPE '\\'")
                params.append(fp_escaped + "/%")
                if not self.recursive:
                    # 直下のみ: フォルダ配下だが、さらに深い階層は除外
                    conditions.append("i.path NOT LIKE ? ESCAPE '\\'")
                    params.append(fp_escaped + "/%/%")
            else:  # "other" — 選択中フォルダ以外
                conditions.append("i.path NOT LIKE ? ESCAPE '\\'")
                params.append(fp_escaped + "/%")

        where = " AND ".join(conditions)
        cursor.execute(
            f"""
            SELECT i.id, i.path
            FROM images i
            WHERE {where}
            ORDER BY i.added_at ASC
            """,
            params,
        )
        rows = cursor.fetchall()
        # ファイル実在チェック（削除済みが MISSING になっていない場合の保険）
        return [
            (img_id, path)
            for img_id, path in rows
            if os.path.exists(path)
        ]


class BackgroundThumbWorker(QThread):
    """
    未キャッシュ画像のサムネイルをバックグラウンドで先行生成するワーカー。

    v2再設計（サムネイル・タグ付けトリガー再設計）対応:
      - 対象は常に「選択中フォルダの見えない範囲」（優先度③）に限定する。
        他フォルダのサムネイル先読みは設計上存在しない
        （気配りに過ぎないため、見ているフォルダ以外では行わない）。
      - 呼び出し元（main_window.py）は、同フォルダの
        BackgroundTaggerWorker(scope='current') が完了してから
        このワーカーを起動すること（③は②完了後にのみ開始）。
      - このワーカー自身は「タグ付け完了→サムネ開始」のような連鎖トリガーを
        持たない（v1で撤廃した設計。起動判断は呼び出し元が行う）。

    ThumbnailCache.put() に書き込むのみ。グリッドUIは触らない。

    シグナル:
        progress(current, total, path)  処理中の状況
        finished(generated, skipped)    完了（対象ゼロ件を除く正常終了）
        queue_empty()                   未キャッシュ画像がゼロだった
        interrupted()                   対象リスト構築中に中断された
                                         （finished(0,0)と違い「未処理分が
                                         残っている」ことを呼び出し元が
                                         区別できるようにするための専用シグナル）
        error(str)                      致命的エラー
    """

    progress    = pyqtSignal(int, int, str)
    finished    = pyqtSignal(int, int)        # generated, skipped
    queue_empty = pyqtSignal()
    interrupted = pyqtSignal()
    error       = pyqtSignal(str)

    def __init__(
        self,
        thumb_cache,           # ThumbnailCache インスタンス
        thumb_size:  int  = 160,
        folder_path: str | None = None,
        recursive:   bool = True,
        parent=None,
        paths:       list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._thumb_cache = thumb_cache
        self._thumb_size  = thumb_size
        self.folder_path  = folder_path
        self.recursive    = recursive
        self.paths        = paths

    def _fetch_active_paths(self, conn) -> list[str]:
        """
        folder_path が指定されていればその配下（recursiveに従う）、
        未指定ならDB全体のACTIVE画像パスを返す。
        バグ修正: 以前はDB全体を毎回Python側で走査していたため
        （SQLレベルの絞り込みが無かった）、大規模ライブラリで
        1万枚規模だと対象リスト構築だけで数分かかっていた。
        フォルダスコープをSQL側でかけることで対象件数を絞る。
        """
        cursor = conn.cursor()
        conditions = ["status = 'ACTIVE'"]
        params: list = []

        if self.folder_path:
            fp_escaped = _escape_like_prefix(self.folder_path)
            conditions.append("path LIKE ? ESCAPE '\\'")
            params.append(fp_escaped + "/%")
            if not self.recursive:
                conditions.append("path NOT LIKE ? ESCAPE '\\'")
                params.append(fp_escaped + "/%/%")

        where = " AND ".join(conditions)
        cursor.execute(
            f"SELECT path FROM images WHERE {where} ORDER BY added_at ASC",
            params,
        )
        return [row[0] for row in cursor.fetchall()]

    def run(self) -> None:
        import lifecycle_manager as _lm
        import traceback
        from PyQt6.QtGui import QImage, QImageReader
        from PyQt6.QtCore import QSize as _QSize

        try:
            # 未キャッシュ画像リストを取得
            try:
                if self.paths is not None:
                    all_paths = self.paths
                else:
                    conn = _lm.get_connection()
                    all_paths = self._fetch_active_paths(conn)
                    conn.close()
            except Exception as e:
                self.error.emit(f"対象ファイルリスト取得失敗: {e}")
                return

            if not all_paths:
                self.queue_empty.emit()
                return

            # バグ修正: 以前はここで全件について事前に os.path.exists() +
            # ThumbnailCache.get()（同期ディスクI/O + SQLite SELECT）を回し、
            # 対象リストを確定させてから処理を始めていた。大規模フォルダ
            # （1万枚規模）だとこの事前スキャンだけで処理開始まで数十秒かかり、
            # その間はディスクI/Oが連続発生して他の処理を巻き込みかねなかった。
            # all_paths をそのままターゲットにし、キャッシュ確認は1枚処理する
            # 直前（メインループ内）で遅延評価することで、事前スキャンの
            # まとめ打ちを解消する。
            #
            # ただしこれにより進捗表示の母数(total)を「フォルダの全アクティブ
            # 画像数」で固定表示していたため、フォルダを切り替えて戻って
            # きただけで母数が常に総数のまま（今回のケースでは常に295）に
            # 見えてしまい、「中断前の進捗が保持されていない」ように見える
            # 不具合になっていた。実際に生成済みのサムネイルはthumb_cache
            # にちゃんと保持されているので、count_cached()（ディスクI/O
            # 無しの一括SQLカウントのみ、高速）で概算の既キャッシュ件数を
            # 引いた「概算の残り件数」を母数として使う。
            try:
                cached_count = self._thumb_cache.count_cached(all_paths, self._thumb_size)
            except Exception:
                cached_count = 0
            total       = max(1, len(all_paths) - cached_count)
            generated   = 0
            skipped     = 0
            cache_hits  = 0

            # バグ修正(ユーザー提案): サムネイル生成時にQImageReaderで既に
            # width/heightを取得しているにもかかわらず、DBへ書き戻して
            # いなかったため、スキャン直後の画像はメニューの「メタデータ補完」
            # を手動実行するまで解像度が「-」表示のままだった。
            # ここで得られる値は追加デコード無しの副産物なので、そのまま
            # images テーブルへバッチ書き込みする（Linar同様、フォルダ
            # アクセス時の解像度取得に相当）。
            # ・書き込み対象は width=0 OR height=0 の行のみ（scan.py が
            #   新規行を width=0/height=0/filesize=0 で挿入するため、
            #   未補完かどうかの判定に使える）
            # ・ユーザー体験を邪魔しない裏作業に留めるため、1枚ごとの
            #   同期書き込みは避け、一定件数ごとにまとめてコミットする
            _META_FLUSH_INTERVAL = 25
            pending_meta: list[tuple[int, int, int, str]] = []  # (w, h, filesize, path)

            try:
                meta_conn = _lm.get_connection()
            except Exception as e:
                print(f"[BGThumb] メタデータ用DB接続に失敗（解像度補完は今回スキップ）: {e}", flush=True)
                meta_conn = None

            def _flush_pending_meta() -> None:
                if meta_conn is None or not pending_meta:
                    return
                try:
                    meta_conn.executemany(
                        "UPDATE images SET width=?, height=?, filesize=? "
                        "WHERE path=? AND (width=0 OR height=0)",
                        [(w, h, fsize, p) for (w, h, fsize, p) in pending_meta],
                    )
                    meta_conn.commit()
                except Exception as e:
                    print(f"[BGThumb] メタデータ書き込み失敗: {e}", flush=True)
                pending_meta.clear()

            try:
                for idx, path in enumerate(all_paths):
                    if self.isInterruptionRequested():
                        _flush_pending_meta()
                        self.interrupted.emit()
                        return

                    if not os.path.exists(path):
                        continue

                    # キャッシュ済みなら何もせずスキップ（遅延評価）
                    if self._thumb_cache.get(path, self._thumb_size) is not None:
                        cache_hits += 1
                        continue

                    # 分子は「概算の残り件数」母数(total)に対応させる
                    # （cache_hitsでスキップした分は数えない）
                    self.progress.emit(generated + skipped + 1, total, path)

                    try:
                        # QImageReader でスケール指定しながら読み込み（高速・省メモリ）
                        reader = QImageReader(path)
                        reader.setAutoTransform(True)
                        reader.setAllocationLimit(0)
                        orig = reader.size()
                        if orig.isValid() and orig.width() > 0 and orig.height() > 0:
                            w, h  = orig.width(), orig.height()
                            scale = min(self._thumb_size / w, self._thumb_size / h)
                            if scale < 1.0:
                                reader.setScaledSize(
                                    _QSize(max(1, int(w * scale)), max(1, int(h * scale)))
                                )
                        else:
                            w = h = 0
                        qimg = reader.read()
                        if qimg.isNull():
                            qimg = QImage(path)
                        if qimg.isNull():
                            skipped += 1
                            continue

                        # アスペクト比維持でサムネサイズに収める
                        if qimg.width() > self._thumb_size or qimg.height() > self._thumb_size:
                            qimg = qimg.scaled(
                                _QSize(self._thumb_size, self._thumb_size),
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation,
                            )

                        # すでにワーカースレッド内なので _put_bg を直接呼ぶ
                        # （put() 経由だと追加スレッドを立てるため二重になる）
                        self._thumb_cache._put_bg(path, self._thumb_size, qimg)
                        generated += 1

                        # 解像度取得に成功していれば補完キューに積む
                        if w > 0 and h > 0 and meta_conn is not None:
                            try:
                                fsize = os.path.getsize(path)
                            except OSError:
                                fsize = 0
                            pending_meta.append((w, h, fsize, path))
                            if len(pending_meta) >= _META_FLUSH_INTERVAL:
                                _flush_pending_meta()

                    except Exception as e:
                        print(f"[BGThumb] {path}: {e}", flush=True)
                        skipped += 1

                    # v2指示書6章 対策3: 先読みWorkerの意図的なthrottle。
                    # ③は「見えない範囲」が対象でも、ユーザーがスクロールして
                    # ①の表示要求と同時にディスクI/Oを奪い合うケースが最も
                    # 見落としやすいリスク（パターン②）とされているため、
                    # 1枚ごとに明示的な間引きを入れる。
                    self.msleep(120)

                # ループを最後まで走り切った場合の残り分をコミット
                _flush_pending_meta()

            except Exception:
                _flush_pending_meta()
                print(f"[BGThumb] Fatal:\n{traceback.format_exc()}", flush=True)
                self.error.emit("バックグラウンドサムネイル生成で予期しないエラーが発生しました")
                return
            finally:
                if meta_conn is not None:
                    meta_conn.close()

            # 全件キャッシュ済み（生成も失敗もゼロ）だった場合は、対象が
            # そもそも無かった扱いとして queue_empty を返す（旧・事前フィルタ時の
            # 挙動を、遅延評価に変更した後も維持するため）
            if generated == 0 and skipped == 0:
                self.queue_empty.emit()
                return

            self.finished.emit(generated, skipped)

        finally:
            # このスレッド専用に開いた thumbnail_cache.db 接続を必ず解放する。
            # これを怠ると、フォルダ再訪問のたびにゾンビ接続が1本ずつ
            # 増え続ける（詳細は該当issue参照）。
            self._thumb_cache.release_thread_connection()


# ---------------------------------------------------------------------------
# 一括タグ追加/削除ワーカー（指示書「複数画像への一括タグ操作」）
# ---------------------------------------------------------------------------

class BulkTagWorker(QThread):
    """
    複数画像に対してタグをまとめて追加/削除するワーカー。
    executemany()自体は数千件規模でも通常1秒未満で終わる処理量だが、
    UIが一瞬固まったように見えないよう、既存のTaggerWorker等と同じ
    「QThread + 簡易QProgressDialog」パターンでバックグラウンド化する
    （ロジックの複雑さを増やすためではなく、体感上のフリーズを避けるため）。

    mode:
        "add"    — category='manual' として追加（既に別カテゴリで存在する
                   同名タグは ON CONFLICT で manual へ格上げ、単体の
                   _add_manual_tag() と同じパターン）
        "delete" — image_id×tag の組をまとめて削除
    """

    progress = pyqtSignal(int, int)          # done, total
    finished = pyqtSignal(int, int, str)     # affected_image_count, tag_count, mode
    error = pyqtSignal(str)

    CHUNK_SIZE = 500

    def __init__(
        self,
        target_ids: list[int],
        tags: list[str],
        mode: str,
        parent: "QObject | None" = None,
    ) -> None:
        super().__init__(parent)
        self._target_ids = list(target_ids)
        self._tags = list(tags)
        self._mode = mode  # "add" | "delete"

    def run(self) -> None:
        conn = None
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            cursor = conn.cursor()

            total = len(self._target_ids)
            done = 0

            for i in range(0, total, self.CHUNK_SIZE):
                if self.isInterruptionRequested():
                    break
                chunk = self._target_ids[i : i + self.CHUNK_SIZE]
                pairs = [(img_id, tag) for img_id in chunk for tag in self._tags]

                if not pairs:
                    done += len(chunk)
                    self.progress.emit(done, total)
                    continue

                if self._mode == "add":
                    cursor.executemany(
                        "INSERT INTO tags (image_id, tag, category) VALUES (?, ?, 'manual') "
                        "ON CONFLICT(image_id, tag) DO UPDATE SET category = 'manual'",
                        pairs,
                    )
                else:
                    cursor.executemany(
                        "DELETE FROM tags WHERE image_id = ? AND tag = ?",
                        pairs,
                    )
                conn.commit()

                done += len(chunk)
                self.progress.emit(done, total)

            self.finished.emit(done, len(self._tags), self._mode)

        except Exception as e:
            self.error.emit(f"一括タグ操作エラー: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


class LoraExportWorker(QThread):
    """
    LoRA作成支援ツールへ渡す前の整形用エクスポート機能（セッション27）。

    選択画像（または絞り込み結果全体）を、新規作成したフォルダへ
    「画像ファイルのコピー」＋「同名.txtへのキャプション書き出し」の
    ペアとして出力する。DB側のタグは一切変更しない非破壊的な操作。

    出力先フォルダはデフォルトで watched_folders に登録しない
    （呼び出し側/main_window.py でも登録処理は行わない）。これにより、
    既存の「未登録フォルダは自動タグ付け対象外」という原則をそのまま
    利用でき、AI再タグ付けを気にせずLoRA向けの整形ができる
    （ユーザー判断: 明示的なロック運用に頼らずこの方式で解決する）。

    caption_mode:
        "all"         — AI由来タグ + マニュアルタグ（rating/meta除外）
        "manual_only" — マニュアルタグのみ

    ファイル名衝突（複数の元フォルダから同名ファイルが集まるケースを含む）
    は、衝突時のみ "元名_連番.拡張子" 形式のサフィックスを付与して回避する。
    画像側とtxt側は同じ連番で揃える。事前に出力先フォルダの既存ファイルも
    衝突判定対象に含める（同じフォルダへの再エクスポート保護）。

    致命的な失敗（出力先フォルダ自体の作成失敗）以外は、画像1件単位で
    例外を捕捉して処理を継続し、完了時にまとめて報告する
    （他のワーカーと同じ「個別except、全体は止めない」方針）。
    """

    progress = pyqtSignal(int, int)   # done, total
    # summary dict: {"copied": int, "renamed": list[tuple[str, str]],
    #                "errors": list[str]}
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)           # 致命的エラー（出力先フォルダ作成失敗等）

    CHUNK_SIZE = 500

    def __init__(
        self,
        targets: list[tuple[int, str]],   # (image_id, path)
        dest_dir: str,
        caption_mode: str,                # "all" | "manual_only"
        parent: "QObject | None" = None,
    ) -> None:
        super().__init__(parent)
        self._targets = list(targets)
        self._dest_dir = dest_dir
        self._caption_mode = caption_mode

    def _fetch_tags_by_image(self, conn: sqlite3.Connection) -> dict[int, list[tuple[str, str]]]:
        """
        対象画像群のタグを (tag, category) のリストとして image_id 単位で
        まとめて取得する。SQLiteのIN()プレースホルダ上限（既定999）を
        避けるため、BulkTagWorkerと同じ500件チャンクで問い合わせる。
        """
        result: dict[int, list[tuple[str, str]]] = {}
        ids = [img_id for img_id, _path in self._targets]
        cursor = conn.cursor()
        for i in range(0, len(ids), self.CHUNK_SIZE):
            chunk = ids[i : i + self.CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(
                f"SELECT image_id, tag, category FROM tags WHERE image_id IN ({placeholders})",
                chunk,
            )
            for image_id, tag, category in cursor.fetchall():
                result.setdefault(image_id, []).append((tag, category if category else "general"))

        # バグ修正（指示書08監査時に発覚）: DBからのSELECTにはORDER BYが無く
        # 取得順の保証が無いため、format_tags_for_copy()が前提とする
        # 「呼び出し側がCATEGORY_ORDER順に並べ替え済み」の状態を満たしていな
        # かった。manualタグ（LoRAトリガーワード等）を先頭に揃えるため、
        # ここで明示的に並べ替える。
        for image_id in result:
            result[image_id] = sort_tags_by_category_order(result[image_id])
        return result

    def _resolve_unique_stem(self, orig_stem: str, image_ext: str, used_lower: set[str]) -> str:
        """
        画像側(stem+image_ext)・txt側(stem+.txt)のどちらか一方でも
        used_lower（小文字化済み集合）と衝突する場合のみ、両方が衝突しない
        stemを探して返す（"元名_連番"形式）。

        画像側の名前だけで衝突判定すると、出力先フォルダに画像を伴わない
        同名の.txtが偶然existingしていた場合、そのtxtだけが無警告で
        上書きされてしまう（画像側は衝突しないため素通りしてしまう）。
        これを避けるため、image_ext/.txt の両方を必ずセットで判定する。
        Windowsのファイル名は大文字小文字を区別しないため、比較は小文字で行う。
        """
        def _conflicts(stem: str) -> bool:
            return (
                (stem + image_ext).lower() in used_lower
                or (stem + ".txt").lower() in used_lower
            )

        if not _conflicts(orig_stem):
            return orig_stem
        n = 2
        while True:
            candidate = f"{orig_stem}_{n}"
            if not _conflicts(candidate):
                return candidate
            n += 1

    def run(self) -> None:
        import shutil

        # --- 出力先フォルダの作成（致命的エラーはここで打ち切る） ---
        try:
            os.makedirs(self._dest_dir, exist_ok=True)
        except Exception as e:
            self.error.emit(f"出力先フォルダの作成に失敗しました: {e}")
            return

        # 出力先フォルダの既存ファイル名も衝突判定に含める
        # （同じフォルダへの再エクスポートを想定した保護）。
        used_lower: set[str] = set()
        try:
            for name in os.listdir(self._dest_dir):
                used_lower.add(name.lower())
        except Exception:
            pass

        conn = None
        tags_by_image: dict[int, list[tuple[str, str]]] = {}
        try:
            import lifecycle_manager as _lm
            conn = _lm.get_connection()
            tags_by_image = self._fetch_tags_by_image(conn)
        except Exception as e:
            self.error.emit(f"タグ情報の取得に失敗しました: {e}")
            return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        total = len(self._targets)
        copied = 0
        renamed: list[tuple[str, str]] = []
        errors: list[str] = []
        empty_captions = 0

        for idx, (image_id, src_path) in enumerate(self._targets):
            if self.isInterruptionRequested():
                break

            self.progress.emit(idx + 1, total)

            try:
                if not os.path.isfile(src_path):
                    errors.append(f"元ファイルが見つかりません: {src_path}")
                    continue

                orig_name = os.path.basename(src_path)
                orig_stem, orig_ext = os.path.splitext(orig_name)

                final_stem = self._resolve_unique_stem(orig_stem, orig_ext, used_lower)
                final_name = final_stem + orig_ext
                used_lower.add(final_name.lower())
                used_lower.add((final_stem + ".txt").lower())
                if final_stem != orig_stem:
                    renamed.append((orig_name, final_name))

                dest_image_path = os.path.normpath(os.path.join(self._dest_dir, final_name))
                dest_txt_path = os.path.normpath(os.path.join(self._dest_dir, final_stem + ".txt"))

                shutil.copy2(src_path, dest_image_path)

                tags = tags_by_image.get(image_id, [])
                if self._caption_mode == "manual_only":
                    tags = [(t, c) for t, c in tags if c == "manual"]
                caption_text = format_tags_for_copy(tags, exclude_categories=("rating", "meta"))
                if not caption_text:
                    empty_captions += 1

                with open(dest_txt_path, "w", encoding="utf-8") as f:
                    f.write(caption_text)

                copied += 1
            except Exception as e:
                errors.append(f"{src_path}: {e}")

        self.finished.emit({
            "copied": copied,
            "renamed": renamed,
            "errors": errors,
            "empty_captions": empty_captions,
        })
