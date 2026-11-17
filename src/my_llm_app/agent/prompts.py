"""各ノードで LLM に渡す指示（プロンプト）とスキーマ。"""

from __future__ import annotations

import json

from my_llm_app.agent.state import AgentState

FEASIBILITY_SYSTEM = """あなたは、調査の依頼が「公開されている情報（Web・百科事典・公的機関・論文など）」で調べられるかを、
調査を始める前に判定する役です。

verdict:
- no: 依頼の中心が、公開情報では原理的に調べられないもの。例:
  - 公開されていない私的・内部の情報（特定の個人の私生活、家族の情報、社内や研究室の非公開の資料・会議の記録や写真、個人のメールやアカウントの中身）
  - まだ起きていない事実（将来の株価の終値、試合の結果、くじの当選番号）。ただし予報・予定・予測の記事が公開されているもの（天気予報、発売予定日など）は調べられる
  - 依頼者本人にしかわからないこと（依頼者の持ち物、依頼者の過去の行動）
- partial: 依頼の一部は調べられるが、一部は上の理由で調べられない
- unsure: 判断がつかない（調べてみないとわからない、マイナーなだけで公開されている可能性がある）
- yes: 公開情報で調べられる

ルール:
- マイナー・情報が少なそう、というだけでは no にしない（unsure か yes）。迷ったら unsure にする
- 実在する人物でも、公人や研究者の公開された経歴・業績は調べられる（yes）。私生活や非公開の情報は no
- infeasible_parts には、調べられない部分を短く書く（yes / unsure なら空）
- reason は 60 文字以内で書く
- suggestions には、利用者が代わりにできることを 1〜3 個書く（例: 「出席者に写真が残っていないか確認する」「手元の資料を渡してもらえれば内容を整理できる」）。yes なら空"""

FEASIBILITY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["yes", "partial", "no", "unsure"]},
        "reason": {"type": "string"},
        "infeasible_parts": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["verdict", "reason", "infeasible_parts", "suggestions"],
}

DECIDE_SYSTEM = """あなたは調査エージェントの「次の行動」を決める役です。
利用者の依頼と、これまでにわかったこと・わからないことを見て、次の行動を 1 つ選びます。

行動:
- search: Web と百科事典を調べる。query に Web 検索の言葉を入れる
- ask_user: 利用者に聞き返す。question に質問を入れる
- respond: 調査結果を利用者に伝えて終わる

ルール:
- まだ何も調べていなければ、まず search する
- names には、どの行動を選んだときも必ず、依頼の対象を表す名前（百科事典で引く短い名前）を 1〜3 個入れる。カタカナ語や外国由来の言葉は原語（英語など）も入れる（例: ["マーキュリー", "Mercury"]）。対象が決まったら、その対象の正式な名前を入れる（例: ["水星"]）
- 依頼の対象に複数の意味があり、利用者がどれを指しているかわからないときは ask_user する。推測で 1 つに決めない
- 対象が決まったら、その対象について search して内容を集め、十分なら respond する
- 同じ検索の言葉を繰り返さない
- reason は 30 文字以内で書く"""

DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["search", "ask_user", "respond"]},
        "reason": {"type": "string"},
        "query": {"type": "string"},
        "names": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "question": {"type": "string"},
    },
    "required": ["action", "reason", "names"],
}

UPDATE_SYSTEM = """あなたは調査エージェントの「状態を更新する」役です。
依頼・利用者の回答・現在の状態・新しい検索結果を読み、状態を更新して返します。

- known: わかったこと。text に短い文（40 文字以内）、source にその根拠の参照（Web 検索の結果の ref「W1」など、
  または Wikidata の項目の id「Q308」など）を 1 つ入れる。URL は書かない。
  検索結果に書かれていないことは入れない。依頼や利用者の回答の言い換えは入れない
- unknown: まだわからないこと（例:「利用者がどの意味のマーキュリーを指しているか」）
- candidate_ids: 依頼の言葉が指しうる対象の候補を、Wikidata の項目の id（Q 番号）で返す。
  入力にある id だけを使い、書き写すときに番号を変えないこと。一般によく知られているもの（天体・元素・神話など）を先に、最大 8 件
- target_id: 利用者の回答などから対象が 1 つに決まった場合だけ、その候補の id。決まらなければ空文字
- target_evidence: target_id を決めた根拠となる、依頼文または利用者の回答の語句をそのまま写す
  （例: 依頼が「太陽系の惑星の水星について」なら「太陽系の惑星」）。対象の名前そのものは根拠にならない。
  「一般にこの意味が多い」は根拠にならない（その場合は target_id を空文字にする）

推測で target_id を決めないこと。利用者が答えていない段階で、複数の意味があるなら target_id は空文字にする。
「特定できた対象」がすでにある場合は、その id を target_id に入れる。
target_id が決まったら、known には target についての事実だけを残す（他の意味の説明は消す）。known は重要なものから最大 5 件。"""

UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "known": {"type": "array", "maxItems": 5, "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "source": {"type": "string"}},
            "required": ["text", "source"],
        }},
        "unknown": {"type": "array", "items": {"type": "string"}},
        "candidate_ids": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
        "target_id": {"type": "string"},
        "target_evidence": {"type": "string"},
    },
    "required": ["known", "unknown", "candidate_ids", "target_id", "target_evidence"],
}

MATCH_SYSTEM = """あなたは、利用者の回答が選択肢のどれを指しているかを判定する役です。
- 回答が明らかに指している選択肢の id を matched_ids に入れる
- 回答に当てはまる選択肢が複数あれば、すべて入れる（例: 回答が「山です」で、山の選択肢が 3 つあれば 3 つとも）
- 回答が選択肢にないものを指している場合（例: 選択肢に川がないのに「川のほうです」）は空にする。近いものを無理に選ばない"""

MATCH_SCHEMA = {
    "type": "object",
    "properties": {"matched_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["matched_ids"],
}


def match_user(question: str, answer: str, candidates: list) -> str:
    return json.dumps({
        "質問": question.splitlines()[0],
        "選択肢": [{"id": c["id"], "名前": c["label"], "説明": c["description"]} for c in candidates],
        "利用者の回答": answer,
    }, ensure_ascii=False, indent=1)


CLASSIFY_SYSTEM = """調査対象の種類を、選択肢から 1 つ選ぶ役です。
対象の名前・説明・概要と、各種類の説明を見て判断する。どれにも当てはまらなければ「その他」を選ぶ。"""


def classify_schema(type_names: list[str]) -> dict:
    return {"type": "object", "properties": {"subject_type": {"type": "string", "enum": type_names}},
            "required": ["subject_type"]}


def classify_user(state: AgentState, type_descriptions: dict[str, str]) -> str:
    target = state.get("target")
    return json.dumps({
        "種類の選択肢": type_descriptions,
        "依頼": state.get("request", ""),
        "調査対象": f"{target['label']}（{target['description']}）" if target else state.get("request", ""),
        "概要": state.get("target_summary", "")[:300],
    }, ensure_ascii=False, indent=1)


EXTRACT_SYSTEM = """あなたは、1 つのページの本文を読んで、調査に関係する事実を抜き出す役です。

- 依頼と調査対象に関係する事実だけを、重要なものから最大 6 件抜き出す
- text には事実を短い文（40 文字以内）で書く
- quote には、その事実の根拠となる本文の一節を、**一字一句そのまま**写す（20〜80 文字程度。長い文は、事実を含む部分だけを連続したまま切り出す）。
  要約・言い換え・語順の変更・省略をしない。quote が本文にない事実はプログラムが捨てる
- 本文に書かれていないことは書かない。数値・日付・時刻は、本文の表記（時差の注記など）に注意して正確に写す
- 調査対象と別のもの（同名の別の対象など）についての記述は抜き出さない
- aspect には、その事実が当てはまる観点を「調べる観点」から 1 つ選ぶ。どれにも当てはまらなければ「その他」
- 「調べる観点」のうち、まだ事実がない観点についての記述を優先して抜き出す
- other_entity: このページが、調査対象とは別の、同じ名前のもの（同名の別人・別の場所など）について書いている場合に、
  その別のものを 30 文字以内で書く。調査対象について書いているページなら空にする
- 関係する事実がなければ facts は空にする"""

def extract_schema(aspect_names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "facts": {"type": "array", "maxItems": 6, "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "quote": {"type": "string"},
                               "aspect": {"type": "string", "enum": aspect_names + ["その他"]}},
                "required": ["text", "quote", "aspect"],
            }},
            "other_entity": {"type": "string"},
        },
        "required": ["facts", "other_entity"],
    }


def extract_user(state: AgentState, title: str, text: str) -> str:
    target = state.get("target")
    return json.dumps({
        "依頼": state.get("request", ""),
        "調査対象": f"{target['label']}（{target['description']}）" if target else state.get("request", ""),
        "調べる観点": [a["name"] for a in state.get("aspects", [])],
        "まだ事実がない観点": [r["aspect"] for r in state.get("coverage", []) if r["facts"] == 0],
        "ページのタイトル": title,
        "ページの本文": text,
    }, ensure_ascii=False, indent=1)


CONFLICT_SYSTEM = """あなたは、同じ観点について別々のページから集めた事実を読み、内容が食い違っている組を見つける役です。

- 同じこと（同じ数量・日時・場所・人数・名前など）について、値や内容が違う事実の組を conflicts に入れる
  例:「死者・行方不明者は30万人以上」と「死者・行方不明者は約23万人」
- fact_ids には食い違っている事実の番号を 2 つ以上入れる。topic には何について食い違っているかを短く書く（20 文字以内）
- 別のことについての事実（例: 死者数と負傷者数）は食い違いではない
- 表現の違いだけで同じ内容（例:「M9.1」と「マグニチュード9.1」）は食い違いではない
- 数値の範囲が重なる場合（例:「M9.0」と「M9.1-9.3」）や、時点・定義が明らかに違う場合も、念のため食い違いとして挙げてよい
- 食い違いがなければ conflicts は空にする"""

CONFLICT_SCHEMA = {
    "type": "object",
    "properties": {
        "conflicts": {"type": "array", "maxItems": 5, "items": {
            "type": "object",
            "properties": {"topic": {"type": "string"},
                           "fact_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 2}},
            "required": ["topic", "fact_ids"],
        }},
    },
    "required": ["conflicts"],
}


def conflict_user(aspect: str, facts: list[dict]) -> str:
    return json.dumps({
        "観点": aspect,
        "事実": [{"番号": i, "内容": f["text"], "根拠の引用": f["quote"]} for i, f in enumerate(facts, 1)],
    }, ensure_ascii=False, indent=1)


IMAGE_SYSTEM = """あなたは、1 枚の画像が調査対象を写したものかを確かめる役です。
画像と、その画像が載っているページの情報（タイトル・キャプション・alt）、対象についてわかっている事実を見て判定する。

- context: 掲載ページの情報が、この画像が対象そのものを写したものだと示しているか
  - high: タイトル・キャプション・alt に対象の名前があり、対象そのものの画像だと明確に読める
  - mid: 対象に関係するページだが、この画像が対象そのものかは文脈だけでは確実でない
  - low: 文脈が対象と関係ない、または別のもの（同名の別の対象・関連する別の物）を示している
- visual: 画像の内容が、対象の説明・事実と合うか
  - consistent: 合う（矛盾がない）  - contradict: 明らかに合わない（別の種類の物が写っている等）  - unknown: 判断できない
- 画像だけで対象を特定しようとしない。マイナーな対象は見た目だけでは区別できないので、文脈（context）を重視する
- 地図・図表・関係のない人物写真などは、対象そのものの画像ではない（context は low か mid）
- ロゴは、対象が組織・会社・ブランド・製品のときは対象を表す画像として認めてよい。それ以外の対象では対象そのものの画像ではない
- description には画像に写っているものを 40 文字以内で書く。reason は 40 文字以内"""

IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "context": {"type": "string", "enum": ["high", "mid", "low"]},
        "visual": {"type": "string", "enum": ["consistent", "contradict", "unknown"]},
        "description": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["context", "visual", "description", "reason"],
}


def image_user(state: AgentState, title: str, caption: str, page_url: str) -> str:
    target = state.get("target")
    facts = [f["text"] for f in state.get("verified", [])][:6]
    return json.dumps({
        "調査対象": f"{target['label']}（{target['description']}）" if target else state.get("request", ""),
        "対象の種類": state.get("subject_type", ""),
        "対象についての事実": facts,
        "掲載ページ": page_url,
        "タイトル": title,
        "キャプション・alt・説明": caption,
    }, ensure_ascii=False, indent=1)


RESPOND_SYSTEM = """あなたは調査結果を利用者に伝える役です。
与えられた材料（対象の概要・確認できた事実）だけを使って、日本語で答えます。

- 材料にないことは書かない。推測で補わない
- 材料の各項目の末尾にある出典番号（[1] など）を、その事実を書いた文の後ろにそのまま付ける。番号を変えたり、別の事実に付け替えたりしない
- 「（出典の信頼度: 低）」が付いた事実は断定せず、「〜とされる」「〜という情報もある」のように書く。
  同じ内容について信頼度の低くない出典と食い違う場合は、信頼度の低くない出典の内容を優先する
- [出典なし] の項目は使わない
- 「置いた前提」がある場合は、冒頭にその文をそのまま書く
- 対象が特定できていない場合は、冒頭でそのことと、置いた前提を正直に書く
- 対象が特定できている場合は、その対象についてだけ書く。同名の別の意味の説明は書かない
- 「公開情報では調べられない部分」がある場合は、最後に「調べられなかったこと」としてその部分と理由を書く
- 確認できた事実は観点ごとに整理し、観点の名前を小見出しにする（事実のない観点は書かない）
- 「出典どうしの食い違い」にある内容は、どちらか一方だけを書かず、両方の値を出典番号付きで併記し、
  「出典によって〜と〜で食い違っている」と書く（食い違いの一覧はプログラムも付ける）
- 確認できなかった観点の一覧はプログラムが付けるので、「わからなかったこと」は書かない
- 出典一覧そのものは書かない（プログラムが付ける）
- 画像についてはプログラムが別に書くので、本文では触れない"""


def _state_view(state: AgentState) -> dict:
    return {
        "依頼": state.get("request", ""),
        "利用者の回答": state.get("clarifications", []),
        "わかったこと": [k["text"] for k in state.get("known", [])],
        "わからないこと": state.get("unknown", []),
        "候補": state.get("candidates", []),
        "特定できた対象": state.get("target"),
        "実行した検索": state.get("searched", []),
        "質問した回数": state.get("questions_asked", 0),
        "公開情報では調べられない部分（調べない）": (state.get("feasibility") or {}).get("infeasible_parts", []),
    }


def decide_user(state: AgentState) -> str:
    return json.dumps(_state_view(state), ensure_ascii=False, indent=1)


def update_user(state: AgentState, query: str, entities: list, web: list) -> str:
    return json.dumps({
        "現在の状態": _state_view(state),
        "今回の検索の言葉": query,
        "Wikidata の項目": [{"id": e.id, "label": e.label, "description": e.description} for e in entities],
        "Web 検索の結果": [{"ref": f"W{i}", "title": w.title, "snippet": w.snippet[:200]}
                         for i, w in enumerate(web, 1)],
    }, ensure_ascii=False, indent=1)


def respond_user(state: AgentState, sources: list[dict]) -> str:
    """sources は出典番号の順。わかったことには、プログラムが出典番号を付けて渡す。"""
    number = {s["url"]: i for i, s in enumerate(sources, 1)}
    summary = state.get("target_summary", "")
    summary_url = state.get("summary_url", "")
    # 回答には、ページ本文で引用を照合できた事実だけを使う（検索結果の要約は使わない。設計書 G-1）
    tier = {s["url"]: s.get("tier", 4) for s in sources}

    def fact(k: dict) -> str:
        # Tier 3〜4（個人・一般のサイト）が根拠の事実は、断定しないよう印を付ける
        low = "（出典の信頼度: 低）" if tier.get(k["source"], 4) >= 3 else ""
        return f"{k['text']} [{number[k['source']]}]{low}"

    # 観点ごとにまとめて渡す（観点チェックリストの順。どれにも当たらない事実は「その他」）
    order = [a["name"] for a in state.get("aspects", [])] + ["その他"]
    facts: dict[str, list[str]] = {}
    for k in sorted((k for k in state.get("verified", []) if k["source"] in number),
                    key=lambda k: order.index(k.get("aspect")) if k.get("aspect") in order else len(order)):
        facts.setdefault(k.get("aspect") or "その他", []).append(fact(k))
    return json.dumps({
        "依頼": state.get("request", ""),
        "利用者の回答": state.get("clarifications", []),
        "特定できた対象": state.get("target"),
        "置いた前提": state.get("assumptions", []),
        "公開情報では調べられない部分": (state.get("feasibility") or {}).get("infeasible_parts", []),
        "対象の概要": f"{summary} [{number[summary_url]}]" if summary and summary_url in number else "",
        "確認できた事実（観点ごと）": facts,
        "出典どうしの食い違い": [
            f"{c['topic']}: " + " / ".join(f"{f['text']} [{number[f['source']]}]" for f in c["facts"]
                                         if f["source"] in number)
            for c in state.get("conflicts", [])],
        "わからないこと": state.get("unknown", []),
    }, ensure_ascii=False, indent=1)
