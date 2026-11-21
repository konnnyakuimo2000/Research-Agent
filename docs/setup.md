# セットアップ手順

Open WebUI（チャット画面）と SearXNG（Web 検索）を Docker で起動し、研究室サーバーの LLM（llama.cpp）と
Web 検索を使って会話できるようにする手順。構成の詳細は [requirements_v0.1.md](requirements_v0.1.md) を参照。

## 必要なもの

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)（起動しておく）
- git
- 研究室サーバーの LLM の接続先（IP・ポート）。リポジトリには書いていないので、メンバーに聞く
- 研究室ネットワークへの接続（学外からは研究室サーバーにつながらない）

Python のスクリプト（`scripts/`）も使う場合は [uv](https://docs.astral.sh/uv/) も入れる。

## 1. リポジトリを取得する

```sh
git clone https://github.com/konnnyakuimo2000/my_llm_app.git
cd my_llm_app
```

## 2. .env を作る

`.env.example` をコピーして `.env` を作り、値を書き換える。`.env` は git 管理外なので、コミットされない。

```sh
cp .env.example .env
```

| 変数 | 書く値 |
|---|---|
| `VL_BASE_URL` | 研究室サーバーの LLM の接続先（例: `http://<server-ip>:<port>/v1`） |
| `VL_MODEL` | `qwen3.8-27b`（そのままでよい） |
| `VL_API_KEY` | `dummy`（そのままでよい） |
| `SEARXNG_SECRET` | ランダムな文字列。下のコマンドで作る |
| `CINII_APPID` | 任意。CiNii Research の API の appid（CiNii の API 利用登録で取得）。空でも動く（その場合 CiNii は site: 検索で探す） |

```sh
# どちらか一方で、ランダムな文字列を作る
python -c "import secrets; print(secrets.token_hex(32))"
openssl rand -hex 32
```

## 3. 起動する

```sh
docker compose up -d
```

初回はイメージのダウンロード、エージェントのイメージ作成、初期化に数分かかる。起動の確認:

```sh
docker compose ps
```

`open-webui`・`searxng`・`webui`（ゲートウェイ）・`agent-research`（調査エージェント）・`agent-chat`（チャット）・`model-gateway`（モデルの窓口）の 6 つが `Up` になっていれば OK（`open-webui` が `healthy` になるまで数分かかる）。

画面・エージェント・モデルの窓口の三層に分かれている（2026-11-21 から）。

| サービス | 内容 |
|---|---|
| `webui`（ゲートウェイ） | テレメトリー画面（http://localhost:8100/ui/）、依頼の記録（`runs/`）、Open WebUI 向けの OpenAI 互換の入口。エージェントとは契約（`/agent/v1/*`）だけで話す |
| `agent-research` | 調査エージェント。外には出さず、ゲートウェイからだけ呼ぶ |
| `agent-chat` | モデルとそのまま話すだけのエージェント。Open WebUI の裏処理も受け持つ |
| `model-gateway` | モデルの窓口。エージェントはモデルを直接呼ばず、ここに頼む。どのモデルに送るか（`config/models.toml`）、やり直し、モデルごとの同時実行の上限（研究室のモデルは全エージェント合わせて 2 件まで）、使用量を受け持つ。LLM の接続先と鍵を持つのはここだけ |

つなぐエージェントは `config/agents.toml`、使うモデルは `config/models.toml` に書く。
`models.toml` の `[roles]` で、役割（判断 `decide`・読み取り `extract`・画像の確認 `vision`・報告 `report`・チャット `chat`）ごとに使うモデルを決める。
依頼ごとに選び直すこともできる。画面では入力欄のモデルのボタン（「既定のモデル」）から選ぶ（全部まとめて 1 つ、または「役割ごとに選ぶ」）。
同じ窓に、モデルごとの接続の状態・使用量・今日の費用と上限が出る。API から選ぶときは:

```sh
curl -X POST http://localhost:8100/api/runs -H "Content-Type: application/json"   -d '{"request": "...", "agent": "research", "models": {"report": "<モデルの id>"}}'
```

選べるモデルは `GET /api/models` で見られる。役割に要る能力（画像の確認なら `vision`）がないモデルは、始める前に断る。
どの依頼でどのモデルを使ったかは、一覧（`GET /api/runs` の `models`）に残る。エージェントを足すときはサービスを足して `agents.toml` に 1 項目、モデルを足すときは `models.toml` に 1 項目足す。

### Web API のモデル（Claude・OpenAI）

`models.toml` には Claude（`claude-opus`・`claude-haiku`）が入っている。`.env` に `ANTHROPIC_API_KEY` を書いて、
`docker compose up -d model-gateway` で窓口を立ち上げ直すと使えるようになる（鍵がなければ、一覧に「使えない」と出る）。
OpenAI は `models.toml` の例のコメントを外し、`OPENAI_API_KEY`・`OPENAI_MODEL` と値段を書く。

- **外部に送られる**: Web API のモデルを選ぶと、依頼文と読んだページの本文がその会社に送られる（一覧の `external: true`）
- **費用**: 値段（`price`）を書いたモデルは、呼び出しごとに費用を数える。上限は `models.toml` の `[limits]`
  （既定は 1 日 $5・1 つの調査 $1）。上限に達すると、値段のあるモデルへは送らずに断る（研究室のモデルは止めない）。
  送る前に確かめるので、同時に送っている分だけ少し超えることがある
- 今日の費用とモデルごとの使用量は窓口の `GET /v1/usage`、1 つの調査の費用はゲートウェイの `GET /api/runs/{id}/usage`
- 記録は `runs/model_spend.json`（立ち上げ直しても今日の分を覚えている）

研究室のモデルと Claude で同じ依頼を比べる例:

```sh
curl -X POST http://localhost:8100/api/runs -H "Content-Type: application/json" \
  -d '{"request": "...", "agent": "research", "models": {"decide": "claude-haiku", "extract": "claude-haiku", "vision": "claude-haiku", "report": "claude-haiku"}}'
```

## 4. 初期設定

1. ブラウザで **http://localhost:3000** を開く
2. アカウントを作る（最初に作ったアカウントが管理者になる）
3. 左上のモデル選択で `qwen3.8-27b` を選ぶ

研究室サーバーへの接続と Web 検索の設定は、初回起動時に `.env` と `docker-compose.yml` から自動で入る。

## 5. 使い方

- 普通に入力して送ると、LLM が答える
- **Web 検索を使う**: 入力欄の左下、「＋」ボタンの右にある「統合」ボタン（ブロックを組み合わせたアイコン）を押し、
  「ウェブ検索」をオンにしてから送る。回答に出典（URL）が付く
- **画像を使う**: 「＋」から画像を添付して質問する

### 調査エージェント（research-agent）

左上のモデル選択で **`research-agent`** を選ぶと、調査エージェントに依頼できる（例:「マーキュリーについて調べて」）。

- 調べている途中の様子は、回答の上の開閉できる欄（「考え中」の表示）に出る。開くと、何を検索し、何ページ読み、どの観点が確認できたかが見える
- 依頼があいまいなときは、エージェントが選択肢付きで質問してくる。**同じ会話の中で**番号か内容を答えると、続きから調べる
- 1 回の調査に 5〜10 分ほどかかる（研究室サーバーの生成速度のため）。その間は画面を閉じずに待つ
- 報告には、出典（信頼度の Tier 付き）、出典どうしの食い違い、調べきれなかった観点、画像（求めた場合）が付く
- タイトル生成などの裏処理は、エージェントではなく `qwen3.8-27b` が直接行う

> 2026-11 より前に Open WebUI を初期設定した場合、エージェントは接続先に入っていない。
> 管理者パネル →「設定」→「接続」→ OpenAI API に `http://webui:8000/v1`（キーは任意の文字列）を追加し、
> 「設定」→「インターフェース」→ タスクモデル（外部）を `qwen3.8-27b` にする。
> 前に追加した `http://agent:8000/v1` は、そのままでも届く（ゲートウェイに `agent` という別名を付けている）。

### テレメトリー画面（調査の様子を見る）

ブラウザで http://localhost:8100/ui/ を開く。依頼を送ると、エージェントが調べている様子（判断・検索・読んでいるページ・画像の確認・網羅表）が木の形で伸びていく。左の一覧から、これまでの調査を再生できる。

- 入力欄の下で、依頼を渡すエージェントを選べる（調査エージェント / チャット）。チャットは作業の段階を持たないので、流れの図を出さずにチャットだけになる
- Open WebUI から `research-agent` に依頼した調査も、この一覧に出る（同じゲートウェイを通るため）

- 依頼の記録は `runs/` に保存される（一覧は `runs/gateway.sqlite`、イベントは `runs/<ID>.events.jsonl`、結果は `runs/<ID>.result.json`。git 管理外）。分ける前の記録と、CLI（`scripts/agent.py`）で実行した調査も一覧に出る
- `?run=<ID>&speed=0&at=110` のように開くと、その調査の 110 秒時点の状態をすぐに出す
- `.env` に `AGENT_API_KEY` を設定している場合は、初回に `?key=<キー>` を付けて開く（ブラウザに保存される）

### 安全のための制限（2026-11-21 から）

- **公開ページ・画像の取得**: エージェントは、検索結果やページから得た URL を、インターネットの公開アドレスにあるものだけ取りに行く。研究室の内部のアドレス（10.x、192.168.x、127.0.0.1、169.254.x など）や、HTTP(S) 以外の URL は取りに行かない。転送（リダイレクト）の先も、つなぐ瞬間の IP アドレスも確かめる。設定は要らない。設定した LLM サーバーと SearXNG には、これまでどおりつながる
- **大きさの上限**: ページは 3 MB、画像は 8 MB を超えた時点で読むのをやめる
- **会話の再開**: Open WebUI は `ENABLE_FORWARD_USER_INFO_HEADERS=true`（`docker-compose.yml` に設定済み）で、利用者 ID とチャット ID をエージェントに渡す。エージェントはこの組で、質問への答えをどの調査に戻すかを決める（同じ文面で始めた別の会話を取り違えない）。この設定を変えたときは `docker compose up -d open-webui` で作り直す。ID が届かない使い方（API を直接呼ぶなど）では、質問の末尾に表示されない会話トークンを入れて見分ける
- ゲートウェイのポート（127.0.0.1:8100）はこの PC からだけ使える。エージェントのサービスはポートを出さない（Docker の中からだけ呼べる）。研究室の外に出す場合は、`AGENT_API_KEY`（ゲートウェイ）と `AGENT_SERVICE_KEY`（ゲートウェイ → エージェント）を設定し、Open WebUI 以外から利用者 ID のヘッダーを送れないようにする

## 6. よく使うコマンド

| コマンド | 内容 |
|---|---|
| `docker compose up -d` | 起動 |
| `docker compose down` | 停止（アカウント・会話履歴は残る） |
| `docker compose ps` | 状態を確認 |
| `docker compose logs -f open-webui` | ログを表示（Ctrl+C で終了） |
| `docker compose restart open-webui` | Open WebUI だけ再起動 |
| `docker build -t my_llm_app-app:local .` → `docker compose up -d webui agent-research agent-chat model-gateway` | コードを変えた後に作り直して再起動（4 つのサービスは同じイメージを使う） |
| `docker compose logs -f agent-research` | 調査エージェントのログを表示（ゲートウェイは `webui`） |

**注意**: `docker compose down -v` は、アカウントと会話履歴もすべて削除する。最初からやり直したいとき以外は使わない。

## 7. 更新するとき

`docker-compose.yml` のイメージのバージョンを書き換えてから:

```sh
docker compose pull
docker compose up -d
```

バージョンは固定している（`latest` は使わない）。更新後は、会話と Web 検索ができることを確認する。

## 8. うまくいかないとき

| 症状 | 原因と対処 |
|---|---|
| `docker compose up` で `VL_BASE_URL` や `SEARXNG_SECRET` のエラーが出る | `.env` がない、または値が空。手順 2 をやり直す |
| モデル選択に `qwen3.8-27b` が出ない | 研究室サーバーにつながっていない。研究室ネットワークにいるか確認し、`docker compose restart open-webui` 後にページを再読み込みする |
| 返事が返ってこない・途中で止まる | 研究室サーバーが混んでいるか、一時的に止まっている（共有サーバーのため）。時間をおいて再度試す |
| 「ウェブ検索」が見つからない | 「＋」の中ではなく、その右の「統合」ボタンの中にある。それでも出なければ `Ctrl+F5` で再読み込み |
| http://localhost:3000 が開けない | `docker compose ps` で起動しているか確認。Docker Desktop が起動しているかも確認する |
| モデル選択に `research-agent` が出ない | `docker compose ps` で `webui` と `agent-research` が `Up` か確認。古い設定のままなら手順 5 の注意のとおり接続先を追加する |
| `research-agent` が途中で止まる | `docker compose logs agent-research`（と `webui`）でエラーを確認する。答えを待っている状態は 6 時間で消えるので、それより後に答えた場合は最初から調べ直す |
| `.env` を変えたのに反映されない | 接続先などの設定は初回起動時に保存され、以後は管理画面（管理者パネル →「設定」）の値が優先される。管理画面で変更する |

## 9. Python スクリプト（任意）

LLM サーバーを直接試すスクリプト。uv が必要。

```sh
uv sync
uv run scripts/vl_test.py   # 接続テスト（モデル一覧・テキスト・画像）
uv run scripts/chat.py      # ターミナルで会話
```

難しい問題でモデルを試すときは [hard_prompts.md](hard_prompts.md) のプロンプトを使う。

Docker を使わずにこの PC で画面とエージェントを動かすとき（開発用。SearXNG は Docker のものを使う）:

```sh
uv run uvicorn --factory my_llm_app.modelgw.app:create_app --port 8103       # モデルの窓口
uv run uvicorn --factory my_llm_app.agents.research:create_app --port 8101   # 調査エージェント
uv run uvicorn --factory my_llm_app.agents.chat:create_app --port 8102       # チャット
uv run uvicorn --factory my_llm_app.gateway.app:create_app --port 8110       # ゲートウェイ → http://localhost:8110/ui/
```

接続先は `config/agents.toml` の `url`（環境変数 `AGENT_RESEARCH_URL`・`AGENT_CHAT_URL` があればそちら）。
エージェントは、環境変数 `MODEL_GATEWAY_URL=http://localhost:8103/v1` を付けて起動すると窓口を通してモデルを呼ぶ。付けなければ、これまでどおり `VL_BASE_URL` の LLM を直接呼ぶ。
