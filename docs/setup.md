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

`open-webui`・`searxng`・`agent`（調査エージェント）の 3 つが `Up` になっていれば OK（`open-webui` が `healthy` になるまで数分かかる）。

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
> 管理者パネル →「設定」→「接続」→ OpenAI API に `http://agent:8000/v1`（キーは任意の文字列）を追加し、
> 「設定」→「インターフェース」→ タスクモデル（外部）を `qwen3.8-27b` にする。

### テレメトリー画面（調査の様子を見る）

ブラウザで http://localhost:8100/ui/ を開く。依頼を送ると、エージェントが調べている様子（判断・検索・読んでいるページ・画像の確認・網羅表）が木の形で伸びていく。左の一覧から、これまでの調査を再生できる。

- 調査の記録は `runs/` に保存される（git 管理外）。CLI（`scripts/agent.py`）で実行した調査も一覧に出る
- `?run=<ID>&speed=0&at=110` のように開くと、その調査の 110 秒時点の状態をすぐに出す
- `.env` に `AGENT_API_KEY` を設定している場合は、初回に `?key=<キー>` を付けて開く（ブラウザに保存される）

## 6. よく使うコマンド

| コマンド | 内容 |
|---|---|
| `docker compose up -d` | 起動 |
| `docker compose down` | 停止（アカウント・会話履歴は残る） |
| `docker compose ps` | 状態を確認 |
| `docker compose logs -f open-webui` | ログを表示（Ctrl+C で終了） |
| `docker compose restart open-webui` | Open WebUI だけ再起動 |
| `docker compose up -d --build agent` | エージェントのコードを変えた後に作り直して再起動 |
| `docker compose logs -f agent` | エージェントのログを表示 |

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
| モデル選択に `research-agent` が出ない | `docker compose ps` で `agent` が `Up` か確認。古い設定のままなら手順 5 の注意のとおり接続先を追加する |
| `research-agent` が途中で止まる | `docker compose logs agent` でエラーを確認する。答えを待っている状態は 6 時間で消えるので、それより後に答えた場合は最初から調べ直す |
| `.env` を変えたのに反映されない | 接続先などの設定は初回起動時に保存され、以後は管理画面（管理者パネル →「設定」）の値が優先される。管理画面で変更する |

## 9. Python スクリプト（任意）

LLM サーバーを直接試すスクリプト。uv が必要。

```sh
uv sync
uv run scripts/vl_test.py   # 接続テスト（モデル一覧・テキスト・画像）
uv run scripts/chat.py      # ターミナルで会話
```

難しい問題でモデルを試すときは [hard_prompts.md](hard_prompts.md) のプロンプトを使う。
