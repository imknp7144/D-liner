"""
thumbnail_cache.py — D-liner サムネイル永続キャッシュ
======================================================
SQLite ベースのサムネイルキャッシュ。linar.db とは別ファイル。

特徴:
  - path + mtime + fsize + thumb_size の4条件でヒット判定
    → ファイル更新・サイズ変更時に自動無効化
  - JPEG 品質 82 で圧縮保存（透過は白背景合成）
  - put() はバックグラウンドスレッドで実行（UI をブロックしない）
  - セッション中のアクセスをインメモリで記録し、close() で一括 UPDATE
  - 7 日間隔で 30 日未アクセスエントリを自動削除 + VACUUM
  - 終了時にサイズ上限（cache/max_size_mb）を超えていれば、last_accessed の
    古い順に上限の cache/trim_percent % まで削除 + VACUUM（セッション35）
  - 作成範囲の制限（cache/scope、セッション35）: 既定は「DB登録フォルダのみ」。
    範囲外フォルダのサムネイルは永続化しない（既存エントリの読み出しは可）
  - 起動時は DB を開けるか（ヘッダ・スキーマ）だけを確認する。従来の
    PRAGMA integrity_check（DB全体を走査するためサイズに比例して起動が
    遅くなる）はセッション35で廃止。内容の破損は実行中の読み書きで
    検出し、終了時に DB を削除して次回起動で再作成する（linar.db に影響なし）

スレッド設計（根治的修正）:
  従来は sqlite3.Connection を1個だけ生成し、check_same_thread=False +
  自前の threading.Lock で、メインスレッド／put()用ThreadPoolExecutor
  スレッド／BackgroundThumbWorker専用QThread など複数OSスレッドから
  共有していた。Pythonレベルのロックで実行タイミングは直列化できていた
  ものの、同一コネクションオブジェクトを複数スレッドにまたがって使い回す
  こと自体がsqlite3のドキュメントで非推奨とされておりクラッシュの原因に
  なり得るため、スレッドごとに個別のコネクションを持つ方式
  （threading.local）に変更した。WALモードなので、複数コネクションからの
  同時読み取り・書き込みはSQLite自身のファイルロック機構（+busy_timeout
  による自動リトライ）で安全に調停される。

QSettings キー（いずれも D-liner/D-liner 名前空間）:
    cache/dir                   キャッシュ DB の保存ディレクトリ
                                デフォルト: {アプリ配置フォルダ}/cache/
    cache/retention_days        未アクセスエントリの保持日数（デフォルト 30）
    cache/auto_clean_interval_days  自動清掃の実行間隔（デフォルト 7）
    cache/max_size_mb           サイズ上限 MB（デフォルト 200、下限 50）
    cache/trim_percent          上限超過時の整理目標（上限の何%まで削るか。
                                デフォルト 80、範囲 50〜90）
    cache/scope                 "registered"（DB登録フォルダのみ・既定）/
                                "registered_and_favorites"（お気に入り
                                = quick_access 配下を含む）
    ※ cache/warn_size_mb はセッション35で廃止（警告は上限の95%以上で表示）
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PyQt6.QtGui import QImage


# ---------------------------------------------------------------------------
# デバッグログ
# ---------------------------------------------------------------------------
# 環境変数 D_LINER_THUMB_CACHE_DEBUG=1 で詳細ログを有効化する
# （tagger_engine.py の D_LINER_ORT_THREADS と同様の env var トグル方式）。
# デフォルトは無効。get()は可視サムネイルの数だけ高頻度に呼ばれるため、
# 常時ONだとログが溢れて逆に追いづらくなる。
_DEBUG = os.environ.get("D_LINER_THUMB_CACHE_DEBUG", "0") == "1"


def _dbg(msg: str) -> None:
    if _DEBUG:
        print(f"[ThumbnailCache][DEBUG] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_DB_FILENAME  = "thumbnail_cache.db"
_JPEG_QUALITY = 82   # 80〜85 の中間値
_BUSY_TIMEOUT_MS = 8000  # 他スレッド/コネクションが書き込み中の場合の自動リトライ上限

# サイズ上限・整理目標の設定値の範囲（セッション35、ユーザー指定。不便があれば再設計）
DEFAULT_MAX_SIZE_MB   = 200
MIN_MAX_SIZE_MB       = 50
DEFAULT_TRIM_PERCENT  = 80
MIN_TRIM_PERCENT      = 50
MAX_TRIM_PERCENT      = 90
SCOPE_REGISTERED      = "registered"
SCOPE_REGISTERED_FAV  = "registered_and_favorites"
WARN_RATIO            = 0.95   # 上限のこの割合以上で起動時に警告


def _norm_for_scope(path: str) -> str:
    """範囲判定用の正規化（区切りを / に統一・末尾 / 除去・大文字小文字無視）。"""
    return path.replace("\\", "/").rstrip("/").lower()


def _is_corruption_error(e: Exception) -> bool:
    """DB 内容の破損を示すエラーか（ロック等の一時的エラーを誤判定しない）。"""
    msg = str(e).lower()
    return any(k in msg for k in ("not a database", "malformed", "corrupt"))


class ThumbnailCache:
    """
    SQLite ベースのサムネイル永続キャッシュ。

    ThumbnailGridWidget から set_cache() で受け取り、
    _trigger_load / _on_worker_finished にフックして使う。
    main_window.py の closeEvent で close() を呼ぶこと。

    スレッドセーフティ: 呼び出しスレッドごとに専用の sqlite3.Connection を
    遅延生成する（_get_conn()）。DB接続自体を跨スレッド共有しないため、
    呼び出し側は特別な排他制御を意識する必要はない。
    """

    def __init__(self, settings=None) -> None:
        """
        Args:
            settings: QSettings インスタンス（省略可）。
                      None の場合はデフォルト値を使用する。
        """
        self._settings = settings

        # スレッドごとの sqlite3.Connection（threading.local）
        self._local = threading.local()
        # _get_conn() で新規生成した全コネクションを close() 時に確実に
        # 閉じられるよう記録しておく（threading.local自体は他スレッドの
        # インスタンスを列挙できないため）
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()  # _all_conns 用（軽量・DB操作は含まない）

        self._accessed_paths: set[str] = set()   # セッション中のアクセス記録
        self._accessed_lock = threading.Lock()   # _accessed_paths 用

        # put() の書き込みスレッド数を最大2に制限（fire-and-forget による枯渇防止）
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ThumbCache")

        # 設定値を読み込む
        self._retention_days = int(self._cfg("cache/retention_days",            30))
        self._clean_interval = int(self._cfg("cache/auto_clean_interval_days",   7))
        self._load_limits()

        # 作成範囲の判定用（main_window が set_scope_roots() で供給する）。
        # None の間は全パスを範囲内とみなす（供給前・取得失敗時に黙って
        # キャッシュしなくなる事態を避けるフェイルオープン）。
        self._scope_registered: list[tuple[str, bool]] | None = None
        self._scope_favorites: list[str] = []
        # 実行中に DB 内容の破損を検出したら終了時に DB を削除する
        self._corrupt_suspected = False

        cache_dir_str = self._cfg("cache/dir", "")
        self._cache_dir = (
            Path(cache_dir_str) if cache_dir_str
            else Path(__file__).parent / "cache"
        )
        self._db_path = self._cache_dir / _DB_FILENAME

        self._db_ready = False
        self._bootstrap()

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def get(self, path: str, thumb_size: int) -> QImage | None:
        """
        キャッシュから QImage を取得する。

        ヒット条件（全て一致が必要）:
          - path がテーブルに存在する
          - mtime が現在のファイルと一致する（±1秒の誤差を許容）
          - fsize が一致する
          - thumb_size が一致する
          - BLOB のデコードに成功する

        Returns:
            QImage（ヒット） または None（ミス・無効・デコード失敗）
        """
        path = Path(path).as_posix()   # パス正規化（\ → /）
        if not self._db_ready:
            _dbg(f"get() miss: db not ready (path={path})")
            return None

        # ファイルの現在の mtime/fsize を取得
        try:
            mtime = os.path.getmtime(path)
            fsize = os.path.getsize(path)
        except OSError as e:
            _dbg(f"get() miss: stat failed for {path}: {e}")
            return None

        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT mtime, fsize, thumb_size, thumb FROM thumb_cache WHERE path = ?",
                (path,),
            ).fetchone()
        except Exception as e:
            _dbg(f"get() miss: SELECT raised for {path}: {e}")
            self._note_error(e)
            return None

        if row is None:
            _dbg(f"get() miss: no row for {path}")
            return None

        db_mtime, db_fsize, db_size, blob = row

        # 無効化チェック: ±1秒の誤差を許容（FAT系ファイルシステム対策）
        if abs(db_mtime - mtime) > 1.0 or db_fsize != fsize or db_size != thumb_size:
            _dbg(
                f"get() miss: stale entry for {path} "
                f"(mtime db={db_mtime} cur={mtime} diff={abs(db_mtime - mtime):.6f}, "
                f"fsize db={db_fsize} cur={fsize}, "
                f"thumb_size db={db_size} req={thumb_size})"
            )
            # 古いエントリを削除（呼び出しスレッド自身のコネクションでそのまま行う。
            # 従来は別スレッドを起こして削除していたが、コネクションをスレッド間で
            # 共有しない方式にしたため、そのまま自スレッドで削除して問題ない）
            self._delete_entry(path)
            return None

        # BLOB → QImage
        qimg = self._blob_to_qimage(blob)
        if qimg is None:
            _dbg(f"get() miss: BLOB decode failed for {path} ({len(blob) if blob else 0} bytes)")
            # 壊れたエントリを削除 → 次回 Worker で再デコード
            self._delete_entry(path)
            return None

        # ヒット記録（close() 時に last_accessed を UPDATE する）
        with self._accessed_lock:
            self._accessed_paths.add(path)
        _dbg(f"get() HIT: {path} (thumb_size={thumb_size})")
        return qimg

    def put(self, path: str, thumb_size: int, qimage: QImage) -> None:
        """
        QImage を JPEG 圧縮して DB に保存する（バックグラウンドで実行）。
        透過（ARGB）画像は白背景に合成してから保存する。
        同時実行数は最大2スレッドに制限（ThreadPoolExecutor）。
        """
        if not self._db_ready:
            _dbg(f"put() skipped: db not ready (path={path})")
            return
        path = Path(path).as_posix()   # パス正規化（\ → /）
        if not self.is_in_scope(path):
            _dbg(f"put() skipped: out of scope (path={path})")
            return
        _dbg(f"put() queued: {path} (thumb_size={thumb_size})")
        self._executor.submit(self._put_bg, path, thumb_size, qimage)

    # ------------------------------------------------------------------
    # 設定・作成範囲（セッション35）
    # ------------------------------------------------------------------

    def _load_limits(self) -> None:
        """QSettings からサイズ上限・整理目標・作成範囲を（範囲補正つきで）読む。"""
        try:
            mx = int(float(self._cfg("cache/max_size_mb", DEFAULT_MAX_SIZE_MB)))
        except (TypeError, ValueError):
            mx = DEFAULT_MAX_SIZE_MB
        try:
            pct = int(float(self._cfg("cache/trim_percent", DEFAULT_TRIM_PERCENT)))
        except (TypeError, ValueError):
            pct = DEFAULT_TRIM_PERCENT
        scope = str(self._cfg("cache/scope", SCOPE_REGISTERED))
        if scope not in (SCOPE_REGISTERED, SCOPE_REGISTERED_FAV):
            scope = SCOPE_REGISTERED
        self._max_size_mb = max(MIN_MAX_SIZE_MB, mx)
        self._trim_percent = max(MIN_TRIM_PERCENT, min(MAX_TRIM_PERCENT, pct))
        self._scope = scope

    def reload_settings(self) -> None:
        """設定ダイアログで保存した直後など、QSettings を読み直す。"""
        self._load_limits()

    @property
    def max_size_mb(self) -> int:
        return self._max_size_mb

    @property
    def trim_percent(self) -> int:
        return self._trim_percent

    @property
    def scope(self) -> str:
        return self._scope

    def scope_includes_favorites(self) -> bool:
        return self._scope == SCOPE_REGISTERED_FAV

    def set_scope_roots(self, registered: list[tuple[str, bool]],
                        favorites: list[str]) -> None:
        """
        作成範囲の判定に使うフォルダ一覧を更新する。

        registered: (フォルダパス, recursive) — watch_mode != 'none' のもの。
                    recursive=False は直下のファイルのみ範囲内。
        favorites : お気に入り（quick_access=1）。配下のサブフォルダも含む。
        """
        reg = [(_norm_for_scope(p), bool(r)) for p, r in registered if p]
        fav = [_norm_for_scope(p) for p in favorites if p]
        self._scope_registered = reg
        self._scope_favorites = fav

    def is_in_scope(self, path: str) -> bool:
        """このパスのサムネイルを永続化してよいか。"""
        if self._scope_registered is None:
            return True   # フェイルオープン（範囲情報の供給前）
        p = _norm_for_scope(path)
        for root, recursive in self._scope_registered:
            if p.startswith(root + "/"):
                if recursive or "/" not in p[len(root) + 1:]:
                    return True
        if self._scope == SCOPE_REGISTERED_FAV:
            for root in self._scope_favorites:
                if p.startswith(root + "/"):
                    return True
        return False

    def is_near_limit(self) -> bool:
        """起動時警告用: ファイルサイズが上限の WARN_RATIO 以上か。"""
        return self.get_size_mb() >= self._max_size_mb * WARN_RATIO

    def get_size_mb(self) -> float:
        """キャッシュ DB のファイルサイズを MB で返す。"""
        try:
            return self._db_path.stat().st_size / (1024 * 1024)
        except OSError:
            return 0.0

    def count_cached(self, paths: list[str], thumb_size: int) -> int:
        """
        与えられたパス群のうち、指定サイズでキャッシュ済み（と思われる）
        件数を高速に返す。

        get() と異なり、ディスクI/O（mtime/fsize確認）は一切行わず、
        SQLite への一括 IN クエリのみで済ませる。そのため、ファイルが
        更新されて実際にはキャッシュが無効化されるケースは考慮されず
        「概算」になるが、大量ファイルに対して1件ずつ os.path.getmtime()
        を呼ぶよりも桁違いに高速なため、進捗表示の母数（残り件数）を
        算出する目的にはこちらを使う。実際にスキップするかどうかの
        最終判定は引き続き get() 側で行う。
        """
        if not self._db_ready or not paths:
            return 0
        norm_paths = [Path(p).as_posix() for p in paths]
        count = 0
        CHUNK = 500  # SQLiteのIN句上限(既定999)に対する安全マージン
        try:
            conn = self._get_conn()
        except Exception:
            return 0
        for i in range(0, len(norm_paths), CHUNK):
            chunk = norm_paths[i:i + CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM thumb_cache "
                    f"WHERE thumb_size = ? AND path IN ({placeholders})",
                    (thumb_size, *chunk),
                ).fetchone()
                count += row[0] if row else 0
            except Exception:
                pass
        _dbg(f"count_cached(): {count}/{len(norm_paths)} matched (thumb_size={thumb_size})")
        return count

    def get_entry_count(self) -> int:
        """キャッシュエントリ数を返す。"""
        if not self._db_ready:
            return 0
        try:
            conn = self._get_conn()
            row = conn.execute("SELECT COUNT(*) FROM thumb_cache").fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def clean_now(self) -> int:
        """
        手動清掃: retention_days 以上アクセスのないエントリを削除する。
        削除件数を返す。
        """
        deleted = self._do_clean()
        self._set_meta("last_cleaned_at", datetime.now().isoformat(timespec="seconds"))
        return deleted

    def delete_all(self) -> None:
        """全エントリを削除する（メンテナンス UI の「全削除」用）。"""
        if not self._db_ready:
            return
        try:
            conn = self._get_conn()
            conn.execute("DELETE FROM thumb_cache")
            conn.execute("DELETE FROM cache_meta WHERE key = 'last_cleaned_at'")
            conn.commit()
            conn.execute("VACUUM")
        except Exception:
            pass

    def close(self) -> None:
        """
        アプリ終了時に呼ぶ。
        1. ThreadPoolExecutor をシャットダウン（進行中の書き込みを完了させる）
        2. セッション中のアクセスを last_accessed に一括 UPDATE
        3. 清掃間隔を確認して必要なら自動清掃
        4. 全スレッドぶんの DB コネクションを閉じる

        注意: BackgroundThumbWorker等、他のQThreadがまだ動作中の状態で
        呼ぶと、そのスレッドが独自に保持しているコネクションはここでは
        閉じられない（外部から強制終了できないため）。呼び出し側は
        closeEvent 等でバックグラウンドワーカーを止めてから呼ぶこと。
        """
        if not self._db_ready:
            return

        # 1. 進行中の put() 書き込みが完全に終わるまで待つ
        try:
            self._executor.shutdown(wait=True, cancel_futures=False)
        except Exception:
            pass

        # 1.5 実行中に DB 内容の破損を検出していた場合は、整理等をせず
        #     全コネクションを閉じて DB ファイルを削除する（次回起動で再作成）。
        #     起動時の integrity_check 廃止（セッション35）に伴う代替手段。
        if self._corrupt_suspected:
            print("[ThumbnailCache] DB corruption detected. Removing DB; "
                  "it will be recreated on next start.", flush=True)
            self._close_all_conns()
            self._remove_db_files()
            self._db_ready = False
            return

        try:
            conn = self._get_conn()
        except Exception as e:
            print(f"[ThumbnailCache] close(): failed to get connection: {e}", flush=True)
            self._db_ready = False
            return

        # 2. アクセス日時を一括 UPDATE
        now_str = datetime.now().isoformat(timespec="seconds")
        with self._accessed_lock:
            accessed = list(self._accessed_paths)
        if accessed:
            try:
                conn.executemany(
                    "UPDATE thumb_cache SET last_accessed = ? WHERE path = ?",
                    [(now_str, p) for p in accessed],
                )
                conn.commit()
            except Exception as e:
                print(f"[ThumbnailCache] last_accessed update failed: {e}", flush=True)

        # 3. 整理（セッション35）
        #    a) 期間による自動清掃（間隔チェックあり）。VACUUM は b) とまとめて
        #       1回だけ行うため、ここでは行わない。
        #    b) サイズ上限超過時の整理（毎回の終了時にチェック）。
        age_deleted = 0
        if self._should_clean():
            age_deleted = self._do_clean(vacuum=False)
            self._set_meta("last_cleaned_at", now_str)
        vacuumed = self._trim_by_size()
        if age_deleted > 0 and not vacuumed:
            self._vacuum()

        # 4. このプロセスが把握している全コネクションを閉じる
        self._close_all_conns()
        self._db_ready = False

    def _close_all_conns(self) -> None:
        """このプロセスが把握している全スレッドぶんのコネクションを閉じる。"""
        with self._conns_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception as e:
                _dbg(f"close(): foreign-thread connection could not be closed "
                     f"(owning thread already exited): {e}")
        self._local.conn = None

    def _remove_db_files(self) -> None:
        """DB 本体と WAL/SHM ファイルを削除する（破損時の再作成用）。"""
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self._db_path) + suffix).unlink(missing_ok=True)
            except Exception as e:
                print(f"[ThumbnailCache] could not remove {self._db_path.name}"
                      f"{suffix}: {e}", flush=True)

    def _note_error(self, e: Exception) -> None:
        """DB 内容の破損を示すエラーなら、終了時に DB を削除するフラグを立てる。"""
        if isinstance(e, sqlite3.DatabaseError) and _is_corruption_error(e):
            if not self._corrupt_suspected:
                print(f"[ThumbnailCache] corruption suspected: {e}", flush=True)
            self._corrupt_suspected = True

    def _vacuum(self) -> None:
        t0 = time.perf_counter()
        try:
            conn = self._get_conn()
            conn.execute("VACUUM")
            # WAL モードでは VACUUM の結果がメインファイルへ反映されるのは
            # チェックポイント時。ここで反映・切り詰めして、ファイルサイズが
            # 実際に縮んだ状態にする（ログ・次回起動時のサイズ警告の正確さのため）
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            print(f"[ThumbnailCache] VACUUM: {(time.perf_counter() - t0) * 1000:.0f} ms",
                  flush=True)
        except Exception as e:
            print(f"[ThumbnailCache] VACUUM failed: {e}", flush=True)

    def _trim_by_size(self) -> bool:
        """
        サイズ上限（max_size_mb）を超えていれば、last_accessed の古い順に
        削除して「上限 × trim_percent %」まで縮め、VACUUM する。
        終了時（close()）専用。VACUUM を実行したら True を返す。
        """
        cap_bytes = self._max_size_mb * 1024 * 1024
        target_bytes = int(cap_bytes * self._trim_percent / 100)
        try:
            conn = self._get_conn()
            # WAL 内の未反映分をメインファイルへ書き戻してから大きさを測る
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            size = self._db_path.stat().st_size
        except Exception as e:
            print(f"[ThumbnailCache] size trim: could not measure: {e}", flush=True)
            return False
        if size <= cap_bytes:
            return False

        t0 = time.perf_counter()
        try:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
            used = size - free_pages * page_size     # 実データ分
            need = used - target_bytes               # 削るべき量
            removed = 0
            if need > 0:
                to_delete: list[int] = []
                freed = 0
                cur = conn.execute(
                    "SELECT rowid, length(thumb) FROM thumb_cache "
                    "ORDER BY last_accessed ASC, rowid ASC"
                )
                try:
                    for rowid, blen in cur:
                        to_delete.append(rowid)
                        freed += (blen or 0)
                        if freed >= need:
                            break
                finally:
                    cur.close()   # 未完了の SELECT が残ると VACUUM が失敗する
                CHUNK = 500
                for i in range(0, len(to_delete), CHUNK):
                    chunk = to_delete[i:i + CHUNK]
                    conn.execute(
                        f"DELETE FROM thumb_cache WHERE rowid IN "
                        f"({','.join('?' for _ in chunk)})", chunk
                    )
                conn.commit()
                removed = len(to_delete)
        except Exception as e:
            print(f"[ThumbnailCache] size trim failed: {e}", flush=True)
            self._note_error(e)
            return False

        self._vacuum()
        try:
            new_size = self._db_path.stat().st_size
        except OSError:
            new_size = 0
        print(
            f"[ThumbnailCache] Size trim: removed {removed} entries, "
            f"{size / 1048576:.1f} MB -> {new_size / 1048576:.1f} MB "
            f"(cap {self._max_size_mb} MB, target {self._trim_percent}%, "
            f"{(time.perf_counter() - t0) * 1000:.0f} ms total)",
            flush=True,
        )
        return True

    def release_thread_connection(self) -> None:
        """
        呼び出しスレッド専用の sqlite3.Connection を閉じ、破棄する。

        BackgroundThumbWorker のように、フォルダ訪問のたびに新しい
        QThread インスタンスとして生成・破棄される一時ワーカーは、
        run() を抜ける直前に必ずこれを呼ぶこと。呼ばないと、そのスレッドが
        _get_conn() で生成したコネクションが _all_conns に残ったまま
        二度と閉じられずリークし続ける（コネクションは生成元スレッドでしか
        close() できないため、後からメインスレッドの close() では回収不能）。
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        try:
            conn.close()
            _dbg(f"release_thread_connection(): closed on thread '{threading.current_thread().name}'")
        except Exception as e:
            _dbg(f"release_thread_connection(): close failed: {e}")
        finally:
            self._local.conn = None
            with self._conns_lock:
                if conn in self._all_conns:
                    self._all_conns.remove(conn)


    # ------------------------------------------------------------------
    # 内部: DB 初期化・接続管理
    # ------------------------------------------------------------------

    def _bootstrap(self) -> None:
        """
        起動時に1回だけ実行: キャッシュディレクトリ作成と、DB を開けるかの確認。

        セッション35: 従来ここで行っていた PRAGMA integrity_check（DB全体を
        走査するためキャッシュサイズに比例して起動が遅くなる。実機でおよそ
        50MBあたり1秒）と SELECT COUNT(*)（ログ用のみ）を廃止した。
        代わりに _get_conn()（WAL 設定・スキーマ作成）が成功するかだけを確認する。
        "not a database" 等のヘッダ破損はここで検出して DB を削除・再作成する。
        ファイル内部の破損は実行中の読み書きで検出し（_note_error）、終了時に
        DB を削除して次回起動で再作成する。
        ロック・権限などの一時的エラーでは DB を削除しない（有効なDBを誤って
        消さないため、メッセージに破損を示す語がある場合のみ削除する）。
        """
        t0 = time.perf_counter()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        _dbg(f"_bootstrap(): resolved db_path={self._db_path.resolve()}")

        ready = False
        for attempt in (1, 2):
            try:
                self._get_conn()
                ready = True
                break
            except sqlite3.DatabaseError as e:
                if (attempt == 1 and self._db_path.exists()
                        and _is_corruption_error(e)):
                    print(f"[ThumbnailCache] DB corrupt ({e}). Recreating...",
                          flush=True)
                    self._close_all_conns()
                    self._remove_db_files()
                    continue
                print(f"[ThumbnailCache] Failed to open DB: {e}", flush=True)
                break
            except Exception as e:
                print(f"[ThumbnailCache] Failed to open DB: {e}", flush=True)
                break

        self._db_ready = ready
        if ready:
            print(f"[ThumbnailCache] DB ready: {self._db_path} "
                  f"(bootstrap {(time.perf_counter() - t0) * 1000:.0f} ms)",
                  flush=True)

    def _get_conn(self) -> sqlite3.Connection:
        """
        呼び出しスレッド専用の sqlite3.Connection を返す（無ければ生成する）。

        根治的修正: 従来は sqlite3.Connection を1個だけ生成し、
        check_same_thread=False + 自前のLockで複数スレッドから共有して
        いたが、同一コネクションオブジェクトをスレッドをまたいで使い回す
        こと自体がsqlite3のドキュメントで非推奨とされている。
        スレッドごとに個別のコネクションを持たせることで、この経路由来の
        不整合・クラッシュの可能性を構造的に排除する。
        WALモード + busy_timeoutにより、複数コネクションからの同時読み書き
        はSQLite自身のファイルロック機構で安全に調停され、ロック競合時も
        例外にせず自動リトライされる。
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            self._ensure_schema(conn)
        except Exception:
            # 初期化に失敗した接続を放置しない（起動時の破損DB再作成で、
            # 開いたままのファイルハンドルが削除を妨げるのを防ぐ）
            try:
                conn.close()
            except Exception:
                pass
            raise

        self._local.conn = conn
        with self._conns_lock:
            self._all_conns.append(conn)
        _dbg(f"_get_conn(): opened new connection on thread '{threading.current_thread().name}'")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS thumb_cache (
                path          TEXT    PRIMARY KEY,
                mtime         REAL    NOT NULL,
                fsize         INTEGER NOT NULL,
                thumb_size    INTEGER NOT NULL,
                thumb         BLOB    NOT NULL,
                last_accessed TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS cache_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tc_last_accessed
                ON thumb_cache(last_accessed);
        """)
        conn.commit()

    # ------------------------------------------------------------------
    # 内部: 読み書き
    # ------------------------------------------------------------------

    def _put_bg(self, path: str, thumb_size: int, qimage: QImage) -> None:
        """put() のバックグラウンド処理本体。"""
        path = Path(path).as_posix()   # パス正規化（\ → /）
        if not self._db_ready:
            return
        if not self.is_in_scope(path):
            return
        try:
            mtime = os.path.getmtime(path)
            fsize = os.path.getsize(path)
        except OSError as e:
            _dbg(f"_put_bg: os.getmtime/getsize failed for {path}: {e}")
            return

        blob = self._qimage_to_jpeg(qimage)
        if not blob:
            _dbg(f"_put_bg: _qimage_to_jpeg returned None for {path}")
            return

        now_str = datetime.now().isoformat(timespec="seconds")
        try:
            conn = self._get_conn()
            conn.execute(
                """INSERT OR REPLACE INTO thumb_cache
                   (path, mtime, fsize, thumb_size, thumb, last_accessed)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (path, mtime, fsize, thumb_size, blob, now_str),
            )
            conn.commit()
            with self._accessed_lock:
                self._accessed_paths.add(path)
            _dbg(
                f"put() OK: {path} (mtime={mtime}, fsize={fsize}, "
                f"thumb_size={thumb_size}, blob={len(blob)}B)"
            )
        except Exception as e:
            print(f"[ThumbnailCache] put failed for {path}: {e}", flush=True)
            self._note_error(e)

    def _delete_entry(self, path: str) -> None:
        if not self._db_ready:
            return
        try:
            conn = self._get_conn()
            conn.execute("DELETE FROM thumb_cache WHERE path = ?", (path,))
            conn.commit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 内部: 画像変換
    # ------------------------------------------------------------------

    @staticmethod
    def _qimage_to_jpeg(qimage: QImage) -> bytes | None:
        """
        QImage → JPEG バイト列。
        透過チャンネルがある場合は白背景に合成してから変換。
        """
        from PyQt6.QtGui import QImage as _QImage, QPainter
        from PyQt6.QtCore import QBuffer, QIODevice
        try:
            img = qimage
            # 透過合成
            if img.hasAlphaChannel():
                flat = _QImage(img.size(), _QImage.Format.Format_RGB32)
                flat.fill(0xFFFFFFFF)
                p = QPainter(flat)
                p.drawImage(0, 0, img)
                p.end()
                img = flat

            # RGB32 に正規化
            if img.format() != _QImage.Format.Format_RGB32:
                img = img.convertToFormat(_QImage.Format.Format_RGB32)

            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "JPEG", _JPEG_QUALITY)
            buf.close()
            data = bytes(buf.data())
            return data if data else None
        except Exception as e:
            _dbg(f"_qimage_to_jpeg failed: {e}")
            return None

    @staticmethod
    def _blob_to_qimage(blob: bytes) -> QImage | None:
        """JPEG バイト列 → QImage。失敗時は None。"""
        from PyQt6.QtGui import QImage as _QImage
        try:
            qimg = _QImage()
            return qimg if qimg.loadFromData(bytes(blob), "JPEG") else None
        except Exception as e:
            # 今回の QIODevice.OpenMode 誤り(修正済み)のように、ここが無音の
            # except だと同種の不具合が再発した際にまた発見が遅れる。
            # デバッグ有効時だけでも可視化しておく。
            _dbg(f"_blob_to_qimage failed: {e}")
            return None

    # ------------------------------------------------------------------
    # 内部: 清掃
    # ------------------------------------------------------------------

    def _should_clean(self) -> bool:
        """前回清掃から clean_interval 日以上経過しているか確認する。"""
        last = self._get_meta("last_cleaned_at")
        if not last:
            return True
        try:
            return (datetime.now() - datetime.fromisoformat(last)).days >= self._clean_interval
        except Exception:
            return True

    def _do_clean(self, vacuum: bool = True) -> int:
        """retention_days 以上アクセスのないエントリを削除する。削除件数を返す。"""
        if not self._db_ready:
            return 0
        cutoff = (
            datetime.now() - timedelta(days=self._retention_days)
        ).isoformat(timespec="seconds")
        try:
            conn = self._get_conn()
            cur = conn.execute(
                "DELETE FROM thumb_cache WHERE last_accessed < ?", (cutoff,)
            )
            deleted = cur.rowcount
            conn.commit()
            if deleted > 0 and vacuum:
                conn.execute("VACUUM")
            print(f"[ThumbnailCache] Auto-clean: removed {deleted} entries.", flush=True)
            return deleted
        except Exception as e:
            print(f"[ThumbnailCache] Clean failed: {e}", flush=True)
            return 0

    # ------------------------------------------------------------------
    # 内部: メタデータ
    # ------------------------------------------------------------------

    def _get_meta(self, key: str) -> str | None:
        if not self._db_ready:
            return None
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT value FROM cache_meta WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def _set_meta(self, key: str, value: str) -> None:
        if not self._db_ready:
            return
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()
        except Exception:
            pass

    def _cfg(self, key: str, default):
        """QSettings から設定値を取得する。settings が None の場合はデフォルトを返す。"""
        if self._settings is not None:
            return self._settings.value(key, default)
        return default
