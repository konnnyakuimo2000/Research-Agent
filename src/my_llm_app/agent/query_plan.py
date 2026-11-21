"""次に打つ検索をプログラムが選ぶ（クエリ探索）。

2 つのアルゴリズムを組み合わせる。

A. 疑似適合フィードバック（harvest）
   検索のたびに、対象の名前を含む結果から「名前の表記ゆれ」と「信頼度の高いドメイン」を拾って状態に貯める。
   LLM が作った読み方ではなく、実際の検索結果にある表記だけを次の検索に使う。

B. 期待利得による最良優先（next_search）
   候補の検索（表記 × 観点、site:ドメイン）を作り、点数を付けて最も高いものだけを打つ。
   点数 = 観点の重さ × 名前の確かさ × 新しさ × 空振りの罰。最高点が MIN_SCORE 未満なら検索しない（打ち切り）。
   LLM は呼ばない。点数の内訳は trace に残す。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from my_llm_app.agent.coverage import MISSING, WEAK
from my_llm_app.agent.names import mentions, name_variants
from my_llm_app.tiers import tier_of

MIN_SCORE = 0.3          # これ未満の候補しかなければ検索しない
MAX_VARIANTS = 3         # 検索に使う表記ゆれの数
MAX_SITES = 3            # site: で狙うドメインの数
WEIGHT = {MISSING: 1.0, WEAK: 0.6}   # 観点の重さ（未発見を先に）
SITE_WITH_FACTS = 0.9    # すでに事実が取れたドメイン（同じサイトの別ページに続きがあることが多い）
SITE_SEEN = 0.6          # 対象の名前を含む結果が出た、まだ事実の取れていないドメイン
FAIL_PENALTY = 0.5       # 同じ観点で空振りする（新しいページが出ない）たびに掛ける


# 依頼文の終わりの決まり文句（「について調べて」など）と、人を呼ぶときの敬称
_REQUEST_TAIL = re.compile(r"(について|に関して|に関する|のこと)?(を)?(詳しく|くわしく)?"
                           r"(調べて|教えて|知りたい|まとめて)(ください|下さい|ほしい|欲しい)?[。．.!！?？\s]*$")
_ABOUT_TAIL = re.compile(r"(について|に関して|のこと)[。．.!！?？\s]*$")
_HONORIFIC = re.compile(r"(先生|准教授|教授|助教|講師|さん|様|氏|くん|ちゃん)$")
_TITLES = ("准教授", "教授", "助教", "講師")
_SCHOOL = re.compile(r"大学|学科|学部|研究科|研究室|高校|高等学校|学校|学院")


def request_parts(request: str) -> list[str]:
    """依頼文を、手がかりの語に分ける（「AのBのC先生について調べて」→ A・B・C）。敬称は外す。"""
    core = _ABOUT_TAIL.sub("", _REQUEST_TAIL.sub("", request.strip()))
    parts = []
    for p in re.split(r"[の、,，\s　]+", core):
        p = p.strip()
        m = _HONORIFIC.search(p)
        if m and len(p) > len(m.group(0)):
            p = p[:m.start()]
        if len(p) >= 2:
            parts.append(p)
    return list(dict.fromkeys(parts))


def request_query(request: str) -> str:
    """依頼文の手がかりをそのまま並べた検索語（LLM が言葉を選び直さない）。

    「先生」は、学校の文脈なら「教員」にする（「先生」は医師・作家にも使われ、検索結果が散らばる）。
    「教授」などの職名は残す。
    """
    core = _ABOUT_TAIL.sub("", _REQUEST_TAIL.sub("", request.strip()))
    school = bool(_SCHOOL.search(core))
    words = list(request_parts(request))
    for p in re.split(r"[の、,，\s　]+", core):
        m = _HONORIFIC.search(p.strip())
        if not m:
            continue
        if m.group(0) in _TITLES:
            words.append(m.group(0))
        elif m.group(0) == "先生" and school:
            words.append("教員")
    return " ".join(dict.fromkeys(words))


def host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def harvest(state: dict, results: list, label: str, decision: dict, new_relevant: int) -> dict:
    """A: 検索結果から表記ゆれと信頼度の高いドメインを拾い、検索の成果を記録する。"""
    variants = dict(state.get("name_variants", {}))
    if label:
        hits = [w for w in results if mentions(f"{w.title} {w.snippet}", [label])]
        for v, n in name_variants([w.title for w in hits], label).items():
            variants[v] = variants.get(v, 0) + n
        sites = list(state.get("seen_sites", []))
        sites += [h for h in dict.fromkeys(host(w.url) for w in hits if tier_of(w.url) <= 2) if h not in sites]
    else:
        sites = list(state.get("seen_sites", []))
    log = state.get("query_log", []) + [{"query": decision.get("query", ""), "aspect": decision.get("aspect", ""),
                                         "site": decision.get("site", ""), "new": new_relevant}]
    return {"name_variants": variants, "seen_sites": sites, "query_log": log}


def _tokens(q: str) -> set[str]:
    # 語順と句読点の違いは無視する（「DÜRST, Martin J.」と「Martin J. Dürst」は同じ検索とみなす）
    return {t.strip(".,、。・") for t in re.split(r"\s+", q.lower()) if t.strip(".,、。・")}


def novelty(name: str, words: str, past: list[str]) -> float:
    """過去の検索と似ていないほど 1 に近い。

    名前の部分と、付け足す言葉の部分を別々に見る。過去の検索に「名前の語」と「付け足す言葉」の両方が
    含まれていれば 0（同じ検索）。名前だけ同じ・言葉だけ同じなら新しい検索とみなす。
    1 つにまとめて似かよりを測ると、付け足す言葉が多い検索ほど新しく見えてしまう。
    """
    tn, tw = _tokens(name), _tokens(words)
    sims = []
    for p in past:
        tp = _tokens(p)
        on = len(tn & tp) / len(tn) if tn else 1.0
        ow = len(tw & tp) / len(tw) if tw else 1.0
        sims.append(on * ow)
    return 1.0 - max(sims, default=0.0)


def names_to_try(state: dict) -> list[tuple[str, float]]:
    """検索に使う名前と、その確かさ。対象の名前は 1.0、表記ゆれは出てきた題名が多いほど高い。"""
    label = (state.get("target") or {}).get("label", "")
    variants = sorted(state.get("name_variants", {}).items(), key=lambda kv: -kv[1])[:MAX_VARIANTS]
    return [(label, 1.0)] + [(v, min(1.0, 0.6 + 0.2 * (n - 1))) for v, n in variants]


def candidates(state: dict) -> list[dict]:
    """B: 候補の検索と点数。点数の高い順。"""
    label = (state.get("target") or {}).get("label", "")
    if not label:
        return []
    rows = [r for r in state.get("coverage", []) if r["status"] in WEIGHT]
    if not rows:
        return []
    words = {a["name"]: a["words"] for a in state.get("aspects", [])}
    past = state.get("searched", [])
    log = state.get("query_log", [])
    fails = {}
    for e in log:
        if e.get("aspect") and not e.get("new"):
            fails[e["aspect"]] = fails.get(e["aspect"], 0) + 1
    out = []
    for name, conf in names_to_try(state):
        for r in rows:
            w = words.get(r["aspect"], r["aspect"])
            q = f"{name} {w}"
            if q in past:
                continue
            parts = {"観点": WEIGHT[r["status"]], "名前": conf, "新しさ": novelty(name, w, past),
                     "空振り": FAIL_PENALTY ** fails.get(r["aspect"], 0)}
            out.append({"query": q, "names": [name], "aspect": r["aspect"], "site": "", "parts": parts})

    # site: で、信頼度の高いドメインの別のページを探す。観点を 1 つに絞らないので、埋まっていない観点の多さで重みを付ける
    fact_sites = list(dict.fromkeys(host(f["source"]) for f in state.get("verified", [])
                                    if f.get("source") and tier_of(f["source"]) <= 2))
    tried = {e.get("site") for e in log if e.get("site")}
    sites = [(s, SITE_WITH_FACTS) for s in fact_sites] + [(s, SITE_SEEN) for s in state.get("seen_sites", [])
                                                         if s not in fact_sites]
    gain = min(1.0, sum(WEIGHT[r["status"]] for r in rows) / 2)
    for s, base in [x for x in sites if x[0] not in tried][:MAX_SITES]:
        q = f"{label} site:{s}"
        if q in past:
            continue
        top = max(rows, key=lambda r: WEIGHT[r["status"]])
        parts = {"観点": gain, "サイト": base, "新しさ": novelty(label, f"site:{s}", past)}
        out.append({"query": q, "names": [label], "aspect": top["aspect"], "site": s, "parts": parts})

    for c in out:
        score = 1.0
        for v in c["parts"].values():
            score *= v
        c["score"] = round(score, 3)
    return sorted(out, key=lambda c: -c["score"])


def next_search(state: dict) -> dict | None:
    """最も点数の高い検索。MIN_SCORE 未満しかなければ None（検索をやめる）。"""
    ranked = candidates(state)
    if not ranked or ranked[0]["score"] < MIN_SCORE:
        return None
    best = ranked[0]
    return {**best, "alternatives": [{"query": c["query"], "score": c["score"]} for c in ranked[1:4]]}
