# llllm_memory_device

**ローカルLLM（LLLM）の記録装置 — 公開向けの最小コア版。**

ローカルLLM（LM Studio 等）が、自分専用のストア（ThoughtsStore）に **tool calling で気づきを書き込み／検索する**、その必要十分な最小構成だけを収めたものです。人間側の ThoughtsStore とは同構造だが別物・別リポジトリとして分離しています。

## この版のスコープ（最小コア）

この公開版が収めるのは、次の4点だけです。

- `bridge.py` — 入口。ローカルLLMとの対話ループ＋tool calling の往復
- `tools.py` — `save_thought` / `search_thoughts`（＋スキーマ検証）
- `save_thought.py` — 書き込みコア（prev_hash → hash のハッシュ鎖で追記）
- `events_ndjson_line.schema.json` — レコードスキーマ

データ器として `ai_ssot/`（フォルダ構造と `.gitkeep` のみ。中身は `.gitignore` で除外）。

### 収めていないもの（意図的）

意味的な**常時想起（Recall）**と、その基盤となる仕組みは、本最小版には**含めていません**。これらは別レイヤー（本格的な実行環境側）で開発・運用しているコンポーネントで、ここでは配布向けに「LLMが自分のストアを tool calling で読み書きする」核だけを示します。

補足として、ツールの `search_thoughts` は**浅い text_head 一致**の軽い検索です。これは重力による意味的な想起（Recall）とは**別レイヤー**で、本最小版に載っているのは前者（浅い一致）だけです。混同しないでください。

## 立ち上げ手順

前提: Python 3、LM Studio（ローカル LLM サーバ）。チャットは OpenAI 互換 API 経由。

```bash
pip install openai jsonschema

# LM Studio でチャットモデルをロードしてから:
python bridge.py --model <モデル名>
```

保存先はデフォルトで `./ai_ssot/`（各スクリプトと同じディレクトリ）。`save_thought` が prev_hash → hash のハッシュ鎖でレコードを追記します。`bridge.py` は最小版では常時想起（Recall）を無効化した状態で動作し、`save_thought` / `search_thoughts` は通常どおり機能します。

## プライバシー / データについて

`ai_ssot/` 配下の生成データ（`journal_by_day` / `indexes` / `media` / `exports` / `file_backups`）は `.gitignore` で除外され、リポジトリには含まれません。追跡されるのはフォルダ構造の `.gitkeep` だけです。個人の思考ログが公開対象に入ることはありません。
