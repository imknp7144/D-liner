#!/usr/bin/env python
from __future__ import annotations

import os
import sys
import argparse
from pathlib import Path
from PIL import Image

import lifecycle_manager

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Asset Viewer - Scanner Tool")
    parser.add_argument(
        "folder",
        nargs="?",
        default=None,
        help="スキャン対象のフォルダパス（指定しない場合はメタデータ補完のみ実行可能）",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="スキャンしたフォルダを監視対象（watched_folders）に登録する",
    )
    parser.add_argument(
        "--mode",
        choices=["startup_check", "manual"],
        default="startup_check",
        help="監視モード（--watch 指定時のみ有効、デフォルト: startup_check）",
    )
    parser.add_argument(
        "--with-meta",
        action="store_true",
        help="登録済み、または新規登録画像のうち width/height が未設定(0)のもののメタデータを補完する",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DBへの登録や変更を行わず、スキャン結果のログ表示のみ行う",
    )
    return parser.parse_args()

def main() -> None:
    args = parse_args()
    
    conn = lifecycle_manager.get_connection()
    lifecycle_manager.ensure_schema(conn)

    # 1. フォルダスキャン & 登録処理
    if args.folder:
        target_path = Path(args.folder).resolve()
        if not target_path.exists() or not target_path.is_dir():
            print(f"Error: 指定されたフォルダが存在しないか、ディレクトリではありません: {args.folder}")
            sys.exit(1)

        print(f"フォルダスキャンを開始します: {target_path}")
        if args.dry_run:
            print("[DRY-RUN] 以下の検出・走査のみ行い、データベースは変更しません。")
            
            # ドライラン時の簡易カウント
            physical_files = []
            for p in target_path.glob("**/*"):
                if p.is_file() and p.suffix.lower() in lifecycle_manager.IMAGE_EXTENSIONS:
                    physical_files.append(p)
            print(f"[DRY-RUN] 検出された画像ファイル数: {len(physical_files)}")
            for pf in physical_files[:10]:
                print(f"  - {pf}")
            if len(physical_files) > 10:
                print(f"  ...他 {len(physical_files) - 10} 件")
        else:
            # 本番スキャン
            res = lifecycle_manager.scan_folder(conn, str(target_path), recursive=True)
            print("スキャン完了:")
            print(f"  新規追加: {res['added']} 件")
            print(f"  移動検出: {res['recovered']} 件")
            print(f"  行方不明: {res['missing']} 件")
            print(f"  変化なし: {res['skipped']} 件")

            # 監視設定への登録
            if args.watch:
                lifecycle_manager.add_watched_folder(
                    conn,
                    str(target_path),
                    recursive=True,
                    watch_mode=args.mode
                )
                print(f"監視対象として登録しました: {target_path} (モード: {args.mode})")

    # 2. メタデータ補完 (--with-meta)
    if args.with_meta:
        print("メタデータ未設定画像の補完処理を実行中...")
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, path FROM images 
            WHERE status = 'ACTIVE' AND (width = 0 OR height = 0)
        """)
        target_images = cursor.fetchall()
        
        if not target_images:
            print("メタデータが未取得の ACTIVE な画像はありません。")
            return

        print(f"補完対象: {len(target_images)} 件")
        updates = []
        for img_id, img_path in target_images:
            if not os.path.exists(img_path):
                continue
            
            try:
                # Pillowでヘッダーのみロードして寸法取得
                with Image.open(img_path) as im:
                    w, h = im.size
                filesize = os.path.getsize(img_path)
                
                if args.dry_run:
                    print(f"[DRY-RUN-META] ID {img_id}: {img_path} -> {w}x{h} ({filesize} bytes)")
                else:
                    updates.append((w, h, filesize, img_id))
            except Exception as e:
                print(f"エラー (ID {img_id}: {img_path}): {e}")

        if updates and not args.dry_run:
            cursor.executemany("""
                UPDATE images 
                SET width = ?, height = ?, filesize = ? 
                WHERE id = ?
            """, updates)
            conn.commit()
            print(f"メタデータ補完を適用しました: {len(updates)} 件完了")

if __name__ == "__main__":
    main()