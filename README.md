# research_agent

研究室サーバーの LLM（llama.cpp / qwen3.8-27b）と Web 検索を組み合わせて、ブラウザから会話できる環境。

- **Open WebUI**: チャット画面
- **SearXNG**: Web 検索
- **調査エージェント**（LangGraph）: Open WebUI のモデル `research-agent` として使える。出典を確かめながら調べて報告する
- **テレメトリー画面**（ゲートウェイ, http://localhost:8100/ui/）: エージェントの作業の流れをノードと線で見ながら依頼する。
  画面とエージェントは契約（Agent Protocol v1, `/agent/v1/*`）だけで話すので、エージェントを差し替え・追加できる
  （`config/agents.toml`。今は調査エージェントとチャット）
- **モデルの窓口**（model-gateway）: エージェントはモデルを直接呼ばず、ここに頼む。どのモデルに送るかは `config/models.toml`。研究室のモデルのほか、Web API のモデル（Claude・OpenAI）も選べ、費用を数えて上限で止める
  で決める（画面・エージェント・モデルの三層）
- すべて Docker で動かし、`docker compose up -d` で起動する

## はじめかた

```sh
cp .env.example .env      # 研究室サーバーの接続先などを書く
docker compose up -d      # 起動
```

http://localhost:3000 を開く。詳しくは [docs/setup.md](docs/setup.md) を参照。

## ドキュメント

- [docs/setup.md](docs/setup.md) — セットアップ手順・使い方・トラブル対処
- [docs/requirements_v0.1.md](docs/requirements_v0.1.md) — 要件定義書 v0.1
- [docs/hard_prompts.md](docs/hard_prompts.md) — モデルを試すための難しい問題集

## 注意

研究室サーバーの IP などは `.env`（git 管理外）に書き、リポジトリには書かない（このリポジトリは public）。
