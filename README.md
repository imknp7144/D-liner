# D-liner

AI画像生成物（イラスト・生成画像等）を対象とした、Windows向けのローカル完結型
画像管理・タグ付けアプリケーションです。

フォルダ単位での画像閲覧に加え、WD14 / Camie / JoyTag といったタガーモデルに
よる自動タグ付けをローカル環境（NPU / GPU / CPU）で実行し、タグによる検索・
整理を行えます。外部サーバーへ画像や生成物をアップロードすることはありません
（初回のみ、タグ付けモデル取得のために Hugging Face へアクセスします）。

> **プレリリース版です。** 動作環境や既知の制約について、導入前に
> [ユーザーマニュアル](docs/MANUAL.md) に必ず目を通してください。

---

## 主な特徴

- **SDI方式のプレビュー**: サムネイルをダブルクリックすると、画像ごとに独立
  したウィンドウが開きます（Linar最大の特徴を継承）
- **ローカルAIタグ付け**: WD14 / Camie / JoyTag から選択し、NPU（Intel）/
  GPU（DirectML）/ CPU いずれでも実行可能
- **タグ検索**: SQLiteベースの高速タグ検索（AND検索・INTERSECT戦略）
- **手動タグ・ロック機能**: AIの誤タグ修正や、LoRA学習用トリガーワードの
  手動追加に対応。画像単位でAI自動タグ付けの対象から明示的に除外可能
- **一括タグ操作 / LoRA用エクスポート**: 選択画像へのタグ一括追加・削除、
  画像＋キャプションのセットでのエクスポートに対応
- **コピー/類似検索モード**: タグパネルをコピーモードに切り替え、選択した
  タグをクリップボードへコピー、または組み合わせ検索が可能
- **バックグラウンド処理を妨げないUI**: フォルダスキャン・タグ付け・
  サムネイル生成はすべて非同期実行

## 動作環境

| 項目 | 要件 |
|---|---|
| OS | Windows 10 / 11（64bit） |
| Python | 3.10 以上（システムPythonが別途必要。D-liner自体は専用venv内で動作） |
| Visual C++ 再頒布可能パッケージ | 2015-2022 (x64) が必要（onnxruntime系パッケージの依存関係） |
| NPU / GPU | 必須ではない。Intel NPUはOpenVINO経由、NVIDIA/AMD/Intel GPUはDirectML経由で高速化 |
| ネットワーク | タグ付けモデルの初回自動ダウンロード時のみ必要（Hugging Face） |

詳細は [ユーザーマニュアル 2章](docs/MANUAL.md) を参照してください。

## インストール

```
setup_runtime_env.bat
```

を実行すると、Pythonバージョン確認・NPU検出・専用venv構築・起動用ランチャー
生成までを自動で行います。NPU非搭載機やNVIDIA/AMD GPU搭載機など、自動検出を
上書きしたい場合は以下のように実行してください。

```
setup_runtime_env.bat --runtime directml
```

（`--runtime` の指定値: `auto`（既定） / `npu` / `directml` / `cpu`）

セットアップ完了後は `launch_d_liner.bat` から起動します。詳細な手順は
[ユーザーマニュアル 3〜4章](docs/MANUAL.md) を参照してください。

## 依存パッケージ

依存関係のインストールは `setup_runtime_env.py` が自動で行うため、手動での
`pip install` は不要です。以下は内容の参考情報です（実際のインストール対象は
`setup_runtime_env.py` 内の `COMMON_PACKAGES` を正としています）。

```
PyQt6>=6.7
numpy>=1.26
Pillow>=10.0
psutil
huggingface_hub
send2trash
```

上記に加え、NPU / GPU / CPU いずれの構成かに応じて、以下のいずれか1つが
選択的にインストールされます（同一環境に共存できないため排他選択）。

| 系統 | 追加パッケージ |
|---|---|
| NPU（Intel OpenVINO） | `onnxruntime-openvino==1.24.1`, `openvino==2025.4.1` |
| GPU（DirectML） | `onnxruntime-directml==1.24.4` |
| CPU | `onnxruntime`（バージョン指定なし） |

なお `onnx` パッケージは、セットアップ時の動作確認（probe）専用の一時的な
依存であり、D-liner本体の実行には不要です。

## 既知の制約事項

- Intel NPU と NVIDIA/AMD GPU は同一環境で併用できません（onnxruntime-openvino
  と onnxruntime-directml が同一venv内で共存不可のため）
- タグ付け中にフォルダの監視登録を解除しても、進行中のタグ付け自体は止まりません
- 手動で削除したAI由来タグは、再タグ付けを行うと復活することがあります
  （ロック機能で除外可能）

その他の制約は [ユーザーマニュアル 9章](docs/MANUAL.md) にまとめています。

## ドキュメント

- [ユーザーマニュアル](docs/MANUAL.md) — 導入・設定・操作方法の詳細
- [CHANGELOG](CHANGELOG.md) — バージョンごとの変更履歴

## ライセンス

[MIT License](LICENSE)

## 開発について

本リポジトリのコード実装はAIコーディングエージェントによって行われています。
"# D-liner" 
"# D-liner" 
