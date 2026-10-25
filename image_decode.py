"""
image_decode.py — Qt が直接読めない形式（avif）の Pillow 経由デコードと、
アニメーション画像（GIF / アニメーションWebP）の判定ヘルパー。

セッション35で新設。

■ avif
  Qt（QImageReader / QImage）は標準では avif を読めないため、一覧には出るが
  サムネイルが「× Error」になっていた（拡張子セットには元から .avif が
  含まれている）。Pillow（12.x は AVIF に標準対応）で開き QImage へ変換する。
  Qt で読める形式の経路には一切触れない（呼び出し側は「Qt で読めなかった
  とき、かつ avif のときだけ」ここを呼ぶ）。

■ アニメーション
  SDI ウィンドウでの再生用。判定は Pillow の is_animated（ヘッダのみの
  軽い判定。QImageReader.imageCount() は GIF で常に 1 を返し判定に使えない
  ことをセッション35で確認した）。再生自体は Qt の QMovie が担う。
"""
from __future__ import annotations

import os

from PyQt6.QtGui import QImage

PILLOW_ONLY_SUFFIXES = frozenset({".avif"})
ANIMATED_SUFFIXES = frozenset({".gif", ".webp"})


def needs_pillow(path: str) -> bool:
    """Qt では読めず、Pillow で読む必要がある形式か。"""
    return os.path.splitext(path)[1].lower() in PILLOW_ONLY_SUFFIXES


def read_via_pillow(path: str, bounds: tuple[int, int] | None = None
                    ) -> tuple[QImage, int, int]:
    """
    Pillow で画像を読み QImage にして返す。

    bounds: (最大幅, 最大高さ)。指定時は縦横比を維持してこの枠に収まるよう
            縮小する（サムネイル・プレビュー用）。None なら原寸。
    Returns: (QImage, 元画像の幅, 元画像の高さ)。EXIF の向きは反映済みで、
             幅・高さは向き補正後の表示上の値。失敗時は (空のQImage, 0, 0)。
    """
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            ow, oh = im.size
            if bounds is not None:
                im = im.copy()
                im.thumbnail((max(1, bounds[0]), max(1, bounds[1])),
                             Image.Resampling.LANCZOS)
            has_alpha = "A" in im.getbands() or "transparency" in im.info
            im = im.convert("RGBA")
            data = im.tobytes("raw", "RGBA")
            fmt = (QImage.Format.Format_RGBA8888 if has_alpha
                   else QImage.Format.Format_RGBX8888)
            # QImage はバッファを参照するだけなので、必ず copy() で独立させる
            qimg = QImage(data, im.width, im.height, im.width * 4, fmt).copy()
            return qimg, ow, oh
    except Exception as e:
        print(f"[image_decode] Pillow decode failed for {path}: {e}", flush=True)
        return QImage(), 0, 0


def is_animated_file(path: str) -> bool:
    """GIF / WebP のうち、複数フレームを持つアニメーションか。"""
    if os.path.splitext(path)[1].lower() not in ANIMATED_SUFFIXES:
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:   # 遅延ロード。ヘッダ確認のみ
            return bool(getattr(im, "is_animated", False))
    except Exception:
        return False
