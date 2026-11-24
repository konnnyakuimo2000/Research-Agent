"""各ノードで LLM に渡す指示（プロンプト）とスキーマ。"""

from __future__ import annotations

import json

from research_agent.agent.state import AgentState
from research_agent.tiers import UrlNumbers

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
- infeasible_parts には、原理的に調べられない部分だけを短く書く（yes / unsure なら空）。
  「同名がいるかもしれない」「載っていないかもしれない」など、調べてみないとわからないことは入れない
- 研究者・教員の顔写真は、大学や研究室のページ・講演の紹介などに公開されていることが多いので、調べられないものに入れない
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
- 画像・写真はプログラムが別に探して確かめる。画像や写真の URL・ファイルを探すための search はしない
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
- unknown: まだわからないこと（例:「利用者がどの意味のマーキュリーを指しているか」）。画像・写真のことは入れない（プログラムが別に探す）
- candidate_ids: 依頼の言葉が指しうる対象の候補を、Wikidata の項目の id（Q 番号）で返す。
  入力にある id だけを使い、書き写すときに番号を変えないこと。一般によく知られているもの（天体・元素・神話など）を先に、最大 8 件
- target_id: 利用者の回答などから対象が 1 つに決まった場合だけ、その候補の id。決まらなければ空文字
- target_evidence: target_id を決めた根拠となる、依頼文または利用者の回答の語句をそのまま写す
  （例: 依頼が「太陽系の惑星の水星について」なら「太陽系の惑星」）。対象の名前そのものは根拠にならない。
  「一般にこの意味が多い」は根拠にならない（その場合は target_id を空文字にする）

- specific_subject: 依頼文に対象を 1 つに絞る条件（所属・型番・地域など）が書かれていて、Web 検索の結果に
  その条件に合う対象が 1 つだけ見つかった場合に、その対象の名前（検索結果にある表記）。Wikidata に項目がなくてもよい。
  条件が書かれていない（例:「マーキュリーについて」）、または条件に合う対象が見つからない・複数ある場合は空文字
- specific_evidence: specific_subject を絞り込んだ、依頼文の語句をそのまま写す（例:「〇〇大学の△△学科の」「カメラの」）

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
        "specific_subject": {"type": "string"},
        "specific_evidence": {"type": "string"},
    },
    "required": ["known", "unknown", "candidate_ids", "target_id", "target_evidence", "specific_subject",
                 "specific_evidence"],
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


SUBJECT_FIX_SYSTEM = """調査対象の名前を、検索結果にある表記に直す役です。
前に答えた名前は、検索結果の題名に書かれていなかった。同じ対象を指す名前を、検索結果の題名から
1 文字も変えずにそのまま写す（読み方・訳し方・並べ替えをしない。題名のうち名前の部分だけを写す）。
題名に見つからなければ空文字にする。"""

SUBJECT_FIX_SCHEMA = {"type": "object", "properties": {"specific_subject": {"type": "string"}},
                      "required": ["specific_subject"]}


def subject_fix_user(subject: str, results: list) -> str:
    return json.dumps({
        "前に答えた名前": subject,
        "検索結果": [{"題名": w.title, "抜粋": w.snippet[:200]} for w in results],
    }, ensure_ascii=False, indent=1)


CLASSIFY_SYSTEM = """調査対象の種類を選び、この依頼で調べる観点（網羅表の行）を作る役です。

1. subject_type: 対象の名前・説明・概要と、各種類の説明を見て 1 つ選ぶ。どれにも当てはまらなければ「その他」。
2. aspects: この対象・この依頼について「何を調べれば調べたことになるか」を 3〜8 個挙げる。
   - 選んだ種類の「お手本の観点」を土台にする。この対象に意味のない観点は外し、依頼で特に聞かれていることや、
     この対象ならではの大事な観点があれば足す（例: 依頼が「外観の写真付きで」なら外観の観点を入れる）。
   - name: 観点の名前。短く（20 字以内）。観点どうしが重ならないようにする。「その他」は使わない。
   - words: その観点のページを探すとき、検索語に付け足す言葉（1〜3 語、空白区切り）。
     「経歴」「仕様」「建設費」のような一般的な言葉だけにする。年・数値・機種名・人名など、
     まだ確かめていない事実の推測は入れない（推測が外れると検索結果がゆがむ）。
   - 画像そのものは観点にしない（画像は別の仕組みで探す）。"""


def classify_schema(type_names: list[str]) -> dict:
    aspect = {"type": "object", "properties": {"name": {"type": "string"}, "words": {"type": "string"}},
              "required": ["name", "words"]}
    return {"type": "object", "properties": {
        "subject_type": {"type": "string", "enum": type_names},
        "aspects": {"type": "array", "items": aspect},
    }, "required": ["subject_type", "aspects"]}


def classify_user(state: AgentState, types: dict[str, dict]) -> str:
    target = state.get("target")
    return json.dumps({
        "種類の選択肢とお手本の観点": types,
        "依頼": state.get("request", ""),
        "利用者の補足": state.get("clarifications", []),
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
- about: その事実が調査対象そのものについてか。型番・世代・版が違うもの（例: iPhone 13 を調べているときの
  iPhone 13 mini や iPhone 12）、シリーズや組織の全体、同じ研究室の別の人などについての記述は「別のもの」。
  対象が主語で、別のものとの違いを述べる記述（例:「iPhone 13 は iPhone 12 より電池が長持ち」）は「対象そのもの」。
  ページが対象とその仲間をまとめて説明しているときは、どれについての記述かを確かめる
- aspect には、その事実が当てはまる観点を「調べる観点」から 1 つ選ぶ。どれにも当てはまらなければ「その他」
- 「調べる観点」のうち、まだ事実がない観点についての記述を優先して抜き出す
- other_entity: このページが、調査対象とは別の、同じ名前のもの（同名の別人・別の場所など）について書いている場合に、
  その別のものを 30 文字以内で書く。調査対象について書いているページなら空にする
- 関係する事実がなければ facts は空にする
- features: 対象を、似たもの（同名の別人・兄弟機種・近縁種・似た建物など）と見分けるのに使える特徴で、
  このページに書かれているもの（例: 外見の色・形、刻印・型番の表記、レンズの名前、所属・専門）。最大 3 件。
  text は 30 文字以内、quote は本文から一字一句そのまま写す。なければ空
- new_terms: 本文にある、対象の別の呼び方（別名・旧称・略称・原語の表記・型番の別の書き方）。
  本文の表記を 1 文字も変えずに写す。調査対象の名前と同じもの・推測した読み方は入れない。最大 3 件。なければ空
- page_tier: このページを出典としてどれだけ信頼できるかの調整。「ページの仮の Tier」（サイトの種類で決めたもの）に対して:
  - 上げる: 仮の Tier が 3〜4 のページで、出典・資料名（原資料・論文・公式発表など）を本文で明記して一次情報を示している
  - 下げる: 仮の Tier が 1〜2 のページで、内容が明らかに古い（後の情報で置き換えられている）、
    他のページの転載であると本文に書かれている、または対象について出典のない伝聞だけを書いている
  - そのまま: 上のどれでもない、または迷う（ほとんどのページはこれ）
  reason は 40 文字以内。そのままなら空でよい"""

ABOUT_TARGET = "対象そのもの"   # 事実の主語が調査対象そのものか（型番違い・シリーズ全体などは除く）
TIER_ADJUSTMENTS = ["上げる", "そのまま", "下げる"]


def extract_schema(aspect_names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "facts": {"type": "array", "maxItems": 6, "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "quote": {"type": "string"},
                               "aspect": {"type": "string", "enum": aspect_names + ["その他"]},
                               "about": {"type": "string", "enum": [ABOUT_TARGET, "別のもの"]}},
                "required": ["text", "quote", "aspect", "about"],
            }},
            "other_entity": {"type": "string"},
            "features": {"type": "array", "maxItems": 3, "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["text", "quote"],
            }},
            "new_terms": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
            "page_tier": {"type": "object", "properties": {
                "adjust": {"type": "string", "enum": TIER_ADJUSTMENTS}, "reason": {"type": "string"}},
                "required": ["adjust", "reason"]},
        },
        "required": ["facts", "other_entity", "features", "new_terms", "page_tier"],
    }


def extract_user(state: AgentState, title: str, text: str, base_tier: int = 4) -> str:
    target = state.get("target")
    return json.dumps({
        "依頼": state.get("request", ""),
        "調査対象": f"{target['label']}（{target['description']}）" if target else state.get("request", ""),
        "調べる観点": [a["name"] for a in state.get("aspects", [])],
        "まだ事実がない観点": [r["aspect"] for r in state.get("coverage", []) if r["facts"] == 0],
        "ページの仮の Tier": base_tier,
        "ページのタイトル": title,
        "ページの本文": text,
    }, ensure_ascii=False, indent=1)


CONFLICT_SYSTEM = """あなたは、同じ観点について別々のページから集めた事実を読み、内容が食い違っている組を見つける役です。

- 同じこと（同じ数量・日時・場所・人数・名前など）について、値や内容が違うように見える事実の組を conflicts に入れ、
  kind でその違いの種類を選ぶ
  - 値が大きく違う: 同じ事柄で、丸めでは説明できないほど値・内容が違う
    例:「死者・行方不明者は30万人以上」と「死者・行方不明者は約23万人」、「2004年に発生」と「2005年に発生」
  - 丸め・概数の違い: 「約」「以上」や丸め方、単位・表し方の違いで説明できる
    例:「約23万人」と「22万人超」、「約430℃」と「427℃」、「地球の約2/5」と「地球の約4割」、「約5800万km」と「57,910,000km」、
    「M9.1」と「マグニチュード9.1」、「半径2440km」と「半径が地球の2/5」
  - 別の事柄・時点: 実は別のことや別の時点・定義について述べている
    例: 死者数と負傷者数、到着予定日と実際の到着日、「1昼夜は176日」と「昼と夜がそれぞれ88日」
- 同じ事柄についての違いは 1 つの組にまとめ、関係する事実の番号をすべて fact_ids に入れる
- fact_ids には事実の番号を 2 つ以上入れる。topic には何についての違いかを短く書く（20 文字以内）
- 迷ったら「値が大きく違う」にしない。利用者に「出典によって食い違う」と伝えるのは「値が大きく違う」だけ
- 違いがなければ conflicts は空にする"""

REAL_CONFLICT = "値が大きく違う"   # 利用者に食い違いとして伝えるのはこの種類だけ

CONFLICT_SCHEMA = {
    "type": "object",
    "properties": {
        "conflicts": {"type": "array", "maxItems": 5, "items": {
            "type": "object",
            "properties": {"topic": {"type": "string"},
                           "kind": {"type": "string", "enum": ["値が大きく違う", "丸め・概数の違い", "別の事柄・時点"]},
                           "fact_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 2}},
            "required": ["topic", "kind", "fact_ids"],
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
  - 「見分ける特徴」（ページから集めた、似たものと区別できる特徴）があれば、画像にその特徴が写っているかを見る。
    特徴と明らかに違うもの（色・形・刻印が違う）が写っていれば contradict。写っていなければ判断に使わない
- features_seen: 「見分ける特徴」のうち、画像で確かめられたものを、そのまま写す（なければ空）
- 画像だけで対象を特定しようとしない。マイナーな対象は見た目だけでは区別できないので、文脈（context）を重視する
- 地図・図表・関係のない人物写真などは、対象そのものの画像ではない（context は low か mid）
- ロゴは、対象が組織・会社・ブランド・製品のときは対象を表す画像として認めてよい。それ以外の対象では対象そのものの画像ではない
- visible_text: 画像の中にはっきり読める文字（製品の刻印・型番・ラベル・看板など）を、そのまま写す。
  読めない・ない場合は空文字。推測で補わない（ページの情報ではなく、画像に写っている文字だけ）
- description には画像に写っているものを 40 文字以内で書く。reason は 40 文字以内"""

IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "context": {"type": "string", "enum": ["high", "mid", "low"]},
        "visual": {"type": "string", "enum": ["consistent", "contradict", "unknown"]},
        "visible_text": {"type": "string"},
        "features_seen": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["context", "visual", "visible_text", "features_seen", "description", "reason"],
}


def image_user(state: AgentState, title: str, caption: str, page_url: str) -> str:
    target = state.get("target")
    facts = [f["text"] for f in state.get("verified", [])][:6]
    return json.dumps({
        "調査対象": f"{target['label']}（{target['description']}）" if target else state.get("request", ""),
        "対象の種類": state.get("subject_type", ""),
        "対象についての事実": facts,
        "見分ける特徴": [f["text"] for f in state.get("features", [])][:6],
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
- 人名・製品名・型番・組織名は、材料（特定できた対象・確認できた事実・出典のタイトル）にある表記のまま書く。
  読み方から別の表記（英語名・ミドルネームなど）を作らない
- 「調べられなかったこと」「わからなかったこと」の節は書かない（プログラムが付ける）
- 確認できた事実は観点ごとに整理し、観点の名前を小見出しにする（事実のない観点は書かない）
- 「出典どうしの食い違い」にある内容は、どちらか一方だけを書かず、両方の値を出典番号付きで併記し、
  「出典によって〜と〜で食い違っている」と書く。食い違いだけをまとめた節は作らない（プログラムが付ける）
- 確認できなかった観点の一覧はプログラムが付けるので、「わからなかったこと」は書かない
- 「出典」「参考文献」の節は書かない（プログラムが付ける）
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
    number = UrlNumbers([s["url"] for s in sources])   # 表記が違うだけの同じページは同じ番号
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
        "出典のタイトル": [s["title"] for s in sources],
    }, ensure_ascii=False, indent=1)
