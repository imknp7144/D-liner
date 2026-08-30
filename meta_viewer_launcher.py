"""
meta_viewer_launcher.py
metahub_lite（ComfyUIメタデータビューア）をD-linerから起動するための薄いヘルパー。

設計方針（引き継ぎ資料 2026-08-18版 §7-2 に基づく）:
- D-liner本体には統合しない（疎結合、ファイルパス渡しのみ）。
  DB・タグ機構・main_window の状態には一切触れない。
- 単体exe化は棚上げ中のため、D-liner自身が実行されているPython
  （venv内の python.exe / pythonw.exe = sys.executable）をそのまま使い、
  `python -m metahub_lite.viewer <path>` の形でサブプロセス起動する。
- metahub_lite/ フォルダは本ファイルと同階層（D-linerのインストール
  ルート、main_window.py と同じ場所）に配置されている前提。
  `-m` 起動のため cwd をこのルートに設定する（metahub_lite が
  パッケージとして解決できるようにするため）。
- metahub_lite が要求する追加パッケージ（piexif）は D-liner の venv
  （setup_runtime_env.py の COMMON_PACKAGES）にも追加してある前提。
  未導入環境向けに、起動失敗時はダイアログで案内する
  （metahub_lite側の詳細なエラー内容までは補足しない＝疎結合を維持）。

同期リンク（MetaViewerLink / open_in_meta_viewer_singleton）:
- SDIウィンドウ用の MetaViewerLink と、サムネイルグリッド用の
  open_in_meta_viewer_singleton の2つの接続方式が併存している。
  どちらも「固定チャンネル名へまずプローブ接続を試み、繋がれば既存
  プロセスを使い回し、繋がらなければ新規起動してその固定チャンネル名
  を名乗らせる」という同じシングルトン戦略を採る（セッション33: 元々
  MetaViewerLinkはopen()の度に新規サブプロセス・新規チャンネル名を
  使う設計だったが、SDIウィンドウが複数あっても meta-only ビューアは
  常に1つだけにしたい、かつVキー連打で孤児プロセスが増えるのを防ぎたい
  という要望を受けて、フル表示側と同じシングルトン方式に統一した）。
  フル表示用（dliner_metaviewer_singleton）と meta-only用
  （dliner_metaviewer_metaonly_singleton）はモードが異なるため別チャン
  ネル名で完全に独立しており、互いに干渉しない。
    - MetaViewerLink: 接続を維持し続け、画像切替の度に何度もpush()
      される。新規起動時のみ、サーバのlisten()が間に合わない可能性が
      あるためQTimer.singleShotによる非同期リトライで接続を確立する。
    - open_in_meta_viewer_singleton: ボタン/右クリックの単発クリック
      応答としてその場で結果が欲しいだけなので、送信後は接続を維持
      しない（都度プローブ→送信→切断）。
  どちらも「改行区切りのUTF-8パス文字列を1行流す」プロトコルは共通
  （_write_path_line参照）。
  既存プロセスへの再接続（＝相手側から見た新規コネクション）が起きる
  度に、metahub_lite側は自動的に自分自身を前面に出す（viewer.py の
  SyncListener.on_new_connection 参照）。そのため前面表示のための
  追加のプロトコル拡張は不要。
"""
from __future__ import annotations

import os
import subprocess
import sys

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtNetwork import QLocalSocket
from PyQt6.QtWidgets import QMessageBox, QWidget


def _dliner_root() -> str:
    """D-liner本体の実行ルート（このファイルと同階層）を返す"""
    return os.path.dirname(os.path.abspath(__file__))


def _write_path_line(sock: QLocalSocket, path: str) -> None:
    """接続済みソケットへ、パスを1行（改行区切りUTF-8）書き込んでflushする。
    MetaViewerLink.push() と open_in_meta_viewer_singleton() の両方から
    呼ばれる、プロトコルの実体（重複実装を避けるための共通化）。"""
    sock.write((path + "\n").encode("utf-8"))
    sock.flush()


# サムネイルグリッドからの「メタデータビューアで開く」（フル表示）用の
# 固定チャンネル名。誰から呼ばれても常にこの名前で待受/接続するため、
# 既に開いている1つのフル表示ウィンドウだけを使い回すシングルトンとして
# 機能する。
_SINGLETON_CHANNEL = "dliner_metaviewer_singleton"
_SINGLETON_CONNECT_TIMEOUT_MS = 200

# SDIウィンドウの「V」キー用（meta-onlyモード）の固定チャンネル名。
# 上記フル表示用シングルトンとはモードが異なるため別名にしてある
# （画像パネル省略のmeta-onlyウィンドウと、画像込みのフル表示ウィンドウ
# を混同して使い回すことはない）。SDIウィンドウが複数あっても、
# meta-onlyビューアは常にこの1プロセスだけを共有する
# （セッション33: MetaViewerLink.open()を参照）。
_METAONLY_SINGLETON_CHANNEL = "dliner_metaviewer_metaonly_singleton"
_METAONLY_PROBE_TIMEOUT_MS = 200


def open_in_meta_viewer_singleton(parent: QWidget | None, path: str) -> None:
    """
    フル表示のMeta Viewerを、既に1つ開いていればそれを使い回して開く。
    接続できれば新しいパスを送るだけ（新規プロセスは起動しない）、
    繋がらなければ新規起動して以後のシングルトン先になる。

    サムネイルグリッドの右クリックのように、同じ操作が何度も繰り返され
    得る呼び出し元向け。UI操作の応答として使うため、接続試行はごく短い
    タイムアウトの同期待ちにしている（サーバーが実在すれば数msで返る）。
    """
    probe = QLocalSocket(parent)
    probe.connectToServer(_SINGLETON_CHANNEL)
    if probe.waitForConnected(_SINGLETON_CONNECT_TIMEOUT_MS):
        _write_path_line(probe, path)
        probe.waitForBytesWritten(_SINGLETON_CONNECT_TIMEOUT_MS)
        probe.disconnectFromServer()
        return

    # 既存プロセスなし。プローブ自体は使い終わったので後始末する
    # （parent持ちなのでparent破棄時にも回収されるが、明示的に切る）。
    probe.abort()
    probe.deleteLater()

    # 新規起動して、以後の呼び出しがこのプロセスを見つけられるよう
    # --sync-channel に固定名を渡す。
    open_in_meta_viewer(parent, path, meta_only=False, sync_channel=_SINGLETON_CHANNEL)


def open_in_meta_viewer(
    parent: QWidget | None,
    path: str,
    meta_only: bool = False,
    sync_channel: str | None = None,
) -> bool:
    """
    metahub_lite をサブプロセスとして起動し、指定画像を開く。
    D-liner本体の応答性に影響しないよう、Popen() のみで完了を待たない
    （sdi_window_viewer.py の関連付け起動と同様の非ブロッキング方針）。

    Args:
        parent: エラーダイアログの親ウィジェット（None可）
        path: 開く画像の絶対パス
        meta_only: True の場合、metahub_lite側で画像パネルを生成せず
            セクション情報のみを表示する（--meta-only）。呼び出し元の
            ウィンドウに既に同じ画像が表示されている場合（SDIウィンドウ
            からの呼び出し等）に、二重描画を避けるために使う。
        sync_channel: 指定した場合、metahub_lite側にその名前で
            QLocalServer を立てさせる（--sync-channel）。呼び出し元は
            MetaViewerLink 経由でこのチャンネルに接続し、以後の画像
            切り替えを追従させることができる。

    Returns:
        サブプロセスの起動に成功したら True。失敗時はダイアログを出して
        False を返す（呼び出し元はこれを見て同期リンクの確立を諦める）。
    """
    root = _dliner_root()
    pkg_dir = os.path.join(root, "metahub_lite")

    if not os.path.isdir(pkg_dir):
        QMessageBox.warning(
            parent, "メタデータビューア未導入",
            "metahub_lite フォルダが見つかりません。\n"
            f"想定パス: {pkg_dir}\n\n"
            "D-liner と同じフォルダに metahub_lite フォルダを配置してください。"
        )
        return False

    cmd = [sys.executable, "-m", "metahub_lite.viewer"]
    if meta_only:
        cmd.append("--meta-only")
    if sync_channel:
        cmd += ["--sync-channel", sync_channel]
    cmd.append(path)
    popen_kwargs: dict = {"cwd": root}
    if sys.platform == "win32":
        # main_window.py の再起動処理と同様、余計なコンソールウィンドウを
        # 出さない（D-liner自体がpythonw.exeで動いている場合は元々出ないが、
        # --debug付きのpython.exe実行時にも二重コンソールが出ないようにする）
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        subprocess.Popen(cmd, **popen_kwargs)
        return True
    except Exception as e:
        QMessageBox.critical(
            parent, "エラー",
            f"メタデータビューアを起動できませんでした:\n{e}\n\n"
            "D-liner の venv に metahub_lite の依存パッケージ（piexif等）が"
            "インストールされているかご確認ください。"
        )
        return False


class MetaViewerLink(QObject):
    """
    SDIウィンドウ等に1つ持たせておく、Meta Viewer（meta-onlyモード）との
    同期リンク。

    使い方:
        self._meta_viewer_link = MetaViewerLink(self)
        ...
        self._meta_viewer_link.open(self.file_path)   # メニュー操作時
        ...
        self._meta_viewer_link.push(self.file_path)   # 画像切替の度に呼ぶ
        ...
        self._meta_viewer_link.close()                # ウィンドウ破棄時

    open() は固定チャンネル（_METAONLY_SINGLETON_CHANNEL）へまずプローブ
    接続を試み、既に他のSDIウィンドウ等が開いたmeta-onlyビューアが
    動いていればそれを再利用する（新規プロセスは起動しない）。無ければ
    新規起動してその固定チャンネル名を名乗らせる（セッション33:
    以前は呼ぶ度に必ず新規サブプロセスを起動しており、Vキー連打や
    複数SDIウィンドウでの利用時に孤児プロセスが蓄積する問題があった
    ため、フル表示側と同じシングルトン方式に統一）。
    複数のSDIウィンドウが同じmeta-onlyビューアを共有するため、後から
    open()したウィンドウが接続を「奪う」形になり、先に追従していた
    ウィンドウのpush()は以後無視されるだけになる（エラーにはならない）。
    push() は現在接続中のリンクにのみパスを流し、未接続なら何もしない
    （＝繋がっていなければ黙って何もしない、壊れても実害のない設計）。
    """

    _MAX_CONNECT_ATTEMPTS = 15
    _RETRY_INTERVAL_MS = 150

    def __init__(self, parent: QWidget | None):
        super().__init__(parent)
        self._parent = parent
        self._channel: str | None = None
        self._socket: QLocalSocket | None = None
        self._pending_socket: QLocalSocket | None = None

    def is_active(self) -> bool:
        return (
            self._socket is not None
            and self._socket.state() == QLocalSocket.LocalSocketState.ConnectedState
        )

    def open(self, path: str) -> None:
        """meta-only の Meta Viewerを開く。既存の共有ビューアがあれば
        再利用し（新規プロセスは起動しない）、無ければ新規起動する。"""
        self.close()  # このリンク自身の古い接続が残っていれば切る
        # （プロセスの生死には影響しない。単に古いソケットを手放すだけ）

        probe = QLocalSocket(self._parent)
        probe.connectToServer(_METAONLY_SINGLETON_CHANNEL)
        if probe.waitForConnected(_METAONLY_PROBE_TIMEOUT_MS):
            # 既存の共有meta-onlyビューアに接続できた。このソケットを
            # そのまま以後の push() 用リンクとして使い続ける
            # （open_in_meta_viewer_singleton と違い、ここでは即切断しない）。
            self._channel = _METAONLY_SINGLETON_CHANNEL
            self._socket = probe
            self._socket.disconnected.connect(self._on_disconnected)
            _write_path_line(self._socket, path)
            return

        # 既存プロセスなし。プローブ自体は使い終わったので後始末する。
        probe.abort()
        probe.deleteLater()

        # 新規起動して、固定チャンネル名を名乗らせる（以後は他のSDI
        # ウィンドウからの再利用先にもなる）。
        if not open_in_meta_viewer(
            self._parent, path, meta_only=True, sync_channel=_METAONLY_SINGLETON_CHANNEL
        ):
            return
        self._channel = _METAONLY_SINGLETON_CHANNEL
        self._try_connect(self._MAX_CONNECT_ATTEMPTS)

    def push(self, path: str) -> None:
        """接続済みのMeta Viewerへ新しい画像パスを1行送る（未接続なら無視）。"""
        if not self.is_active():
            return
        _write_path_line(self._socket, path)

    def close(self) -> None:
        for sock in (self._socket, self._pending_socket):
            if sock is not None:
                sock.disconnectFromServer()
        self._socket = None
        self._pending_socket = None
        self._channel = None

    # -- 内部: 起動直後はサーバのlisten()が間に合わないことがあるため、
    #    接続失敗を許容してリトライする --------------------------------

    def _try_connect(self, attempts_left: int) -> None:
        if self._channel is None or attempts_left <= 0:
            return
        sock = QLocalSocket(self._parent)
        channel = self._channel
        sock.connected.connect(lambda: self._on_connected(sock))
        sock.errorOccurred.connect(
            lambda _err, a=attempts_left, c=channel: self._on_connect_error(a, c)
        )
        self._pending_socket = sock
        sock.connectToServer(channel)

    def _on_connected(self, sock: QLocalSocket) -> None:
        if sock is not self._pending_socket:
            return  # 既にcloseされた後などの古いソケットは無視
        self._socket = sock
        self._pending_socket = None
        self._socket.disconnected.connect(self._on_disconnected)

    def _on_connect_error(self, attempts_left: int, channel: str) -> None:
        if channel != self._channel:
            return  # 既に別のopen()/close()が呼ばれた後の古い試行
        QTimer.singleShot(self._RETRY_INTERVAL_MS, lambda: self._try_connect(attempts_left - 1))

    def _on_disconnected(self) -> None:
        # ユーザーがMeta Viewerウィンドウを閉じた等。以後pushは無視される
        # だけなので、明示的な後始末は不要（＝これまで通りの動作に戻る）。
        self._socket = None
        self._channel = None
