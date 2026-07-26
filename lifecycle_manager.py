"""
lifecycle_manager.py — AI Asset Viewer DB管理モジュール
=======================================================
DB接続・スキーマ管理・ファイルライフサイクル同期の中核モジュール。

定数:
    IMAGE_EXTENSIONS    対応拡張子セット
    DB_FILENAME         DBファイル名
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path
from typing import TypedDict


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

DB_FILENAME = "linar.db"

IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".bmp", ".tiff", ".tif", ".avif",
})

# watch_mode の有効値
WATCH_MODES: frozenset[str] = frozenset({
    "realtime",       # 起動中常時監視
    "startup_check",  # 起動時に差分スキャン、以降は手動のみ
    "manual",         # 起動時も含め完全手動
})


# ---------------------------------------------------------------------------
# 自然順ソート（指示書08 タスクA）
# ---------------------------------------------------------------------------
#
# SQLiteの ORDER BY は既定でBINARY照合（単純なバイト列比較）のため、
# 桁数の異なるゼロ埋め連番（例: "00002" と "000021"）が人間の直感と
# 異なる順序になる。ここで定義するキー関数・collation関数はDB経由
# （SearchWorker、get_connection()でcollation登録）・ファイルシステム
# 経由（FilesystemSearchWorker、Python側でkey=に直接使用）の両方から
# 共有して使う（重複実装によるロジックの食い違いを避けるため、
# ここに一本化する）。

def natural_sort_key(s: str) -> tuple:
    """
    自然順ソート用キー関数。文字列を数字/非数字の交互リストに分割し、
    数字部分は int に変換して比較することで「000021」と「00003」を
    正しく 21 > 3 として扱える。非数字部分は .lower() で大文字小文字も
    同時に吸収する。
    """
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", s)
    )


def _natural_collate(a: str, b: str) -> int:
    """sqlite3.Connection.create_collation() に登録するcollation関数。"""
    ka, kb = natural_sort_key(a), natural_sort_key(b)
    return -1 if ka < kb else (1 if ka > kb else 0)


NATURAL_SORT_COLLATION_NAME = "NATSORT"


# ---------------------------------------------------------------------------
# 型定義
# ---------------------------------------------------------------------------

class ScanResult(TypedDict):
    added:     int   # 新規登録
    recovered: int   # MISSING → ACTIVE 復活
    missing:   int   # 実在しないファイルを MISSING にマーク
    skipped:   int   # 変化なし（既存ACTIVE）


class WatchedFolder(TypedDict):
    id:         int
    path:       str
    recursive:  bool
    watch_mode: str
    added_at:   str


# ---------------------------------------------------------------------------
# DB接続
# ---------------------------------------------------------------------------

def _resolve_db_path() -> Path:
    return Path(__file__).parent / DB_FILENAME


def get_connection() -> sqlite3.Connection:
    db_path = _resolve_db_path()
    # timeout=30: TaggerWorker等の高頻度書き込みと競合した場合に
    # 最大30秒ビジーリトライする（デフォルト5秒だと database is locked エラーが頻発）
    # check_same_thread=False: WALモード下でスレッド間共有を安全に許可
    conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # 自然順ソート（指示書08タスクA）: collationはコネクション単位の登録が
    # 必要なため、DB接続を一手に引き受けるこの関数で必ず登録する
    # （get_connection()経由の呼び出し元はSearchWorker含め全て網羅済み、
    # と確認済み。thumbnail_cache.pyは別DBのため対象外）。
    conn.create_collation(NATURAL_SORT_COLLATION_NAME, _natural_collate)
    return conn


# ---------------------------------------------------------------------------
# スキーマ管理
# ---------------------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS images (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            path       TEXT    NOT NULL UNIQUE,
            hash       TEXT,
            width      INTEGER NOT NULL DEFAULT 0,
            height     INTEGER NOT NULL DEFAULT 0,
            filesize   INTEGER NOT NULL DEFAULT 0,
            status     TEXT    NOT NULL DEFAULT 'ACTIVE',
            added_at   TEXT    DEFAULT (datetime('now')),
            updated_at TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tags (
            image_id  INTEGER NOT NULL,
            tag       TEXT    NOT NULL,
            category  TEXT    NOT NULL DEFAULT 'general',
            PRIMARY KEY (image_id, tag),
            FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS watched_folders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            path       TEXT    NOT NULL UNIQUE,
            recursive  INTEGER NOT NULL DEFAULT 1,
            watch_mode TEXT    NOT NULL DEFAULT 'startup_check',
            added_at   TEXT    DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_images_status   ON images(status);
        CREATE INDEX IF NOT EXISTS idx_images_path     ON images(path);
        CREATE INDEX IF NOT EXISTS idx_tags_tag        ON tags(tag);
        CREATE INDEX IF NOT EXISTS idx_tags_image_id   ON tags(image_id);
    """)
    conn.commit()

    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(images)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    if "added_at" not in existing_cols:
        conn.execute("ALTER TABLE images ADD COLUMN added_at TEXT")
        conn.execute("UPDATE images SET added_at = datetime('now') WHERE added_at IS NULL")
        conn.commit()

    if "updated_at" not in existing_cols:
        conn.execute("ALTER TABLE images ADD COLUMN updated_at TEXT")
        conn.execute("UPDATE images SET updated_at = datetime('now') WHERE updated_at IS NULL")
        conn.commit()

    if "hash" not in existing_cols:
        conn.execute("ALTER TABLE images ADD COLUMN hash TEXT")
        conn.commit()

    # バグ修正: 破損ファイル・巨大画像等、恒久的にタグ付け不能な画像を
    # 区別して記録するためのカラム。これが無いと BackgroundTaggerWorker
    # の「未タグ付け画像」抽出条件（tagsテーブルに1件も無い）に永久に
    # 該当し続け、アイドル処理のたびに同じ失敗を繰り返すだけの無限ループ
    # になっていた。
    if "tag_failed" not in existing_cols:
        conn.execute("ALTER TABLE images ADD COLUMN tag_failed INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    # 画像単位でバックグラウンド自動タグ付け対象から除外するためのフラグ。
    # SDIウィンドウのロックボタン（sdi_window_viewer.py SDIWindow参照）で
    # ユーザーが明示的にON/OFFする。tag_failedと異なり、AIの成否とは無関係
    # に「この画像はAIに触らせたくない」という意思表示専用。
    if "ai_tagging_suppressed" not in existing_cols:
        conn.execute("ALTER TABLE images ADD COLUMN ai_tagging_suppressed INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_images_added_at ON images(added_at)"
    )
    conn.commit()

    # tags.category カラム追加（既存 DB への後方互換マイグレーション）
    cursor.execute("PRAGMA table_info(tags)")
    tag_cols = {row[1] for row in cursor.fetchall()}
    if "category" not in tag_cols:
        conn.execute("ALTER TABLE tags ADD COLUMN category TEXT NOT NULL DEFAULT 'general'")
        conn.commit()

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tags_category ON tags(category)"
    )
    conn.commit()

    # バグ修正（レビュワー指摘・v0.8）: watched_folders.quick_access は
    # 以前 folder_tree.py（2箇所）・file_operation_dialog.py（1箇所）が
    # それぞれ個別に PRAGMA table_info → ALTER TABLE していた（tag_failed/
    # ai_tagging_suppressed は ensure_schema() に集約済みなのに、これだけ
    # 外れていた設計の不統一）。ここに一本化する。呼び出し元の個別チェックは
    # 後方互換の安全策としてそのまま残しても無害（ensure_schema()が既に
    # 追加済みなら no-op になるだけ）。
    cursor.execute("PRAGMA table_info(watched_folders)")
    wf_cols = {row[1] for row in cursor.fetchall()}
    if "quick_access" not in wf_cols:
        conn.execute(
            "ALTER TABLE watched_folders ADD COLUMN quick_access INTEGER DEFAULT 0"
        )
        conn.commit()


# ---------------------------------------------------------------------------
# ファイルスキャン
# ---------------------------------------------------------------------------

def _collect_image_paths(root: str, recursive: bool) -> list[str]:
    results: list[str] = []
    root_path = Path(root)

    if recursive:
        for p in root_path.rglob("*"):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                results.append(str(p).replace("\\", "/"))
    else:
        for p in root_path.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                results.append(str(p).replace("\\", "/"))

    return results


def _get_filesize(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _compute_hash(path: str) -> str | None:
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            h.update(f.read(65536))
        return h.hexdigest()
    except OSError:
        return None


def scan_folder(
    conn: sqlite3.Connection,
    path: str,
    recursive: bool = True,
) -> ScanResult:
    result: ScanResult = {"added": 0, "recovered": 0, "missing": 0, "skipped": 0}

    physical_paths = set(_collect_image_paths(path, recursive))
    cursor = conn.cursor()

    norm_root = path.replace("\\", "/").rstrip("/")
    if recursive:
        cursor.execute(
            "SELECT id, path, status FROM images WHERE path LIKE ?",
            (norm_root + "/%",),
        )
    else:
        cursor.execute(
            "SELECT id, path, status FROM images WHERE path LIKE ? AND path NOT LIKE ?",
            (norm_root + "/%", norm_root + "/%/%"),
        )
    db_rows = {row[1]: (row[0], row[2]) for row in cursor.fetchall()}

    for fpath in physical_paths:
        if fpath in db_rows:
            img_id, status = db_rows[fpath]
            if status == "ACTIVE":
                result["skipped"] += 1
            else:
                cursor.execute(
                    "UPDATE images SET status='ACTIVE', updated_at=datetime('now') WHERE id=?",
                    (img_id,),
                )
                result["recovered"] += 1
        else:
            filesize = _get_filesize(fpath)
            file_hash = _compute_hash(fpath)
            cursor.execute(
                """INSERT INTO images (path, hash, filesize, status)
                   VALUES (?, ?, ?, 'ACTIVE')""",
                (fpath, file_hash, filesize),
            )
            result["added"] += 1

    for db_path, (img_id, status) in db_rows.items():
        if db_path not in physical_paths and status == "ACTIVE":
            cursor.execute(
                "UPDATE images SET status='MISSING', updated_at=datetime('now') WHERE id=?",
                (img_id,),
            )
            result["missing"] += 1

    conn.commit()
    return result


# ---------------------------------------------------------------------------
# 監視フォルダ管理・判定
# ---------------------------------------------------------------------------

def _normalize_folder_path(path: str) -> str:
    """
    監視フォルダパスの正規化を1箇所に集約する（v0.8・レビュワー指摘対応）。

    以前は add_watched_folder()/remove_watched_folder() だけが
    Path(path).resolve() を使い、is_watched_path()・scan_folder()・
    UI側（folder_tree.py・file_operation_dialog.py 等）の大半は
    単純な replace("\\\\", "/") のみを使っていた。resolve() はシンボリック
    リンク・ジャンクションを実体パスへ解決するため、登録時（resolve()あり）
    と参照時（resolve()なし）でパス文字列が食い違い、「クイックアクセスに
    出ない」「is_watched_path が False になる」といった再現しづらい不具合の
    温床になっていた。大多数派である「slash統一のみ」（resolve()しない）に
    統一する。

    注意: 既存DBに既に resolve() 済みのパスで登録済みのフォルダがある場合、
    そのレコード自体は今回のマイグレーション対象外（動いている既存データに
    は触れない方針）。今後の新規登録・参照から本関数に統一される。
    """
    return path.replace("\\", "/").rstrip("/")


def is_watched_path(conn: sqlite3.Connection, path: str) -> bool:
    """
    指定されたフォルダパスが監視対象（直接登録されている、
    あるいは再帰設定された上位フォルダの傘下にある）かを判定する。
    """
    norm_path = _normalize_folder_path(path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT path, recursive FROM watched_folders")
    rows = cursor.fetchall()
    
    for w_path, recursive in rows:
        w_norm = _normalize_folder_path(w_path)
        if norm_path == w_norm:
            return True
        if recursive and norm_path.startswith(w_norm + "/"):
            return True
            
    return False


def add_watched_folder(
    conn: sqlite3.Connection,
    path: str,
    recursive: bool = True,
    watch_mode: str = "startup_check",
) -> None:
    if watch_mode not in WATCH_MODES:
        raise ValueError(
            f"無効な watch_mode: {watch_mode!r}。有効な値: {sorted(WATCH_MODES)}"
        )

    normalized = _normalize_folder_path(path)

    conn.execute(
        """INSERT INTO watched_folders (path, recursive, watch_mode)
           VALUES (?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
               recursive  = excluded.recursive,
               watch_mode = excluded.watch_mode""",
        (normalized, int(recursive), watch_mode),
    )
    conn.commit()


def remove_watched_folder(conn: sqlite3.Connection, path: str) -> None:
    normalized = _normalize_folder_path(path)
    conn.execute(
        "DELETE FROM watched_folders WHERE path = ?",
        (normalized,),
    )
    conn.commit()


def get_watched_folders(conn: sqlite3.Connection) -> list[WatchedFolder]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, path, recursive, watch_mode, added_at FROM watched_folders ORDER BY added_at"
    )
    return [
        WatchedFolder(
            id=row[0],
            path=row[1],
            recursive=bool(row[2]),
            watch_mode=row[3],
            added_at=row[4],
        )
        for row in cursor.fetchall()
    ]


# ---------------------------------------------------------------------------
# ライフサイクル同期
# ---------------------------------------------------------------------------

def sync_lifecycle(conn: sqlite3.Connection) -> ScanResult:
    total: ScanResult = {"added": 0, "recovered": 0, "missing": 0, "skipped": 0}

    folders = get_watched_folders(conn)
    startup_folders = [f for f in folders if f["watch_mode"] == "startup_check"]

    for folder in startup_folders:
        if not os.path.isdir(folder["path"]):
            continue
        result = scan_folder(conn, folder["path"], recursive=folder["recursive"])
        for key in total:
            total[key] += result[key]  # type: ignore[literal-required]

    return total