"""グラフのノード。

- decide:   LLM が次の行動を選び、プログラムが検査する（guard.py）
- search:   Wikidata（同名候補）と SearXNG（Web）を調べ、LLM が状態を更新する
- ask_user: 利用者に聞き返す（interrupt で一時停止）
- respond:  集めた材料から回答を作る
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from langgraph.types import interrupt

from my_llm_app.agent import prompts
from my_llm_app.agent.guard import check_decision, is_ambiguous
from my_llm_app.agent.quotes import normalize, quote_in_text
from my_llm_app.agent.state import DOMINANCE_RATIO, PAGES_PER_READ, AgentState, Candidate, PageRead, Source
from my_llm_app.config import Settings
from my_llm_app.llm import LLM
from my_llm_app.tiers import by_tier, tier_of
from my_llm_app.tools import fetch, searxng, wikimedia

# LLM に渡す本文の長さの上限（文字数）。長いページは先頭から
MAX_PAGE_CHARS = 8000
# 対象が決まったときに site: で狙って探す Tier 1 のドメイン
TIER1_SITES = ("go.jp", "ac.jp")

log = logging.getLogger(__name__)


class Nodes:
    def __init__(self, settings: Settings, llm: LLM) -> None:
        self.settings = settings
        self.llm = llm

    def check_feasibility(self, state: AgentState) -> dict:
        """公開情報で調べられる依頼かを、調査を始める前に判定する（設計書 4 章 ④、例 D）。"""
        out = self.llm.chat_json(prompts.FEASIBILITY_SYSTEM, state["request"], prompts.FEASIBILITY_SCHEMA)
        feasibility = {
            "verdict": out.get("verdict", "unsure"),
            "reason": out.get("reason", ""),
            "infeasible_parts": out.get("infeasible_parts", []),
            "suggestions": out.get("suggestions", []),
        }
        log.info("feasibility: %s (%s)", feasibility["verdict"], feasibility["reason"])
        return {"feasibility": feasibility, "trace": [{"node": "check_feasibility", "llm": out}]}

    def report_infeasible(self, state: AgentState) -> dict:
        """調べられない依頼を、探し回らずに正直に申告する（設計書 9.2 の形）。文面はプログラムが作る。"""
        answer = infeasible_report(state["request"], state["feasibility"])
        return {"answer": answer, "trace": [{"node": "report_infeasible", "answer": answer}]}

    def decide(self, state: AgentState) -> dict:
        raw = self.llm.chat_json(prompts.DECIDE_SYSTEM, prompts.decide_user(state), prompts.DECIDE_SCHEMA)
        decision = check_decision(state, raw)
        log.info("decide: %s (%s)%s", decision["action"], decision.get("reason", ""),
                 f" [guard: {decision['guard']}]" if decision.get("guard") else "")
        return {
            "decision": decision,
            "steps": state.get("steps", 0) + 1,
            "trace": [{"node": "decide", "llm": raw, "decision": decision}],
        }

    def search(self, state: AgentState) -> dict:
        decision = state["decision"]
        query = decision["query"]
        started = time.perf_counter()
        errors: list[str] = []

        # Wikidata は名前の前方一致で引くため、長い検索文ではなく短い名前で引く（横の展開はプログラム）
        names = [n.strip() for n in decision.get("names", []) if n.strip()] or [query.split()[0]]
        entities: list[wikimedia.Entity] = []
        for name in names[:3]:
            try:
                found = wikimedia.search_entities(name, limit=8)
            except Exception as e:  # noqa: BLE001 - 失敗は記録して続ける
                errors.append(f"wikidata({name}): {e}")
                continue
            have = {x.id for x in entities}
            entities += [e for e in found if e.id not in have]
        try:
            web = searxng.web_search(self.settings.searxng_url, query)
        except Exception as e:  # noqa: BLE001
            web, errors = searxng.WebSearchResponse(results=[]), errors + [f"searxng: {e}"]
            web_failed = f"Web 検索（SearXNG）に接続できなかった（{type(e).__name__}）"
        else:
            web_failed = ""

        update = self.llm.chat_json(
            prompts.UPDATE_SYSTEM,
            prompts.update_user(state, query, entities, web.results),
            prompts.UPDATE_SCHEMA,
        )

        # LLM には id を選ばせるだけにし、実際に取得した項目にある id だけを認める。
        # 名前と説明は Wikidata の実データで埋める（LLM の書き写し間違い・作り話の防止）
        valid_ids = list(dict.fromkeys([e.id for e in entities] + [c["id"] for c in state.get("candidates", [])]))
        chosen = [i for i in update.get("candidate_ids", []) if i in valid_ids]
        target_id = update.get("target_id", "")
        target_id = target_id if target_id in valid_ids else ""
        try:
            info = wikimedia.get_entities(list(dict.fromkeys(chosen + [target_id] if target_id else chosen)))
        except Exception as e:  # noqa: BLE001
            errors.append(f"wikidata(get): {e}")
            info = {e.id: e for e in entities}
        candidates = rank([as_candidate(info[i]) for i in chosen if i in info]) or state.get("candidates", [])
        target = state.get("target") or (as_candidate(info[target_id]) if target_id in info else None)
        if (target and not state.get("target") and not state.get("clarifications") and len(candidates) >= 2
                and target != dominant(candidates)
                and not evidence_ok(update.get("target_evidence", ""), state["request"], target["label"])):
            # 利用者が答えていないのに LLM が対象を決めた。依頼文に根拠となる語句がなければ認めない（推測を防ぐ）
            errors.append(f"target rejected: evidence={update.get('target_evidence', '')!r}")
            target = None
        assumptions = state.get("assumptions", [])
        if not target and (primary := dominant(candidates)):
            target = primary
        if target and not state.get("target") and not state.get("clarifications"):
            # 利用者に確かめずに対象を決めた場合は、その判断を前提として報告に書く
            assumptions = assumptions + [assumption_text(target, candidates,
                                                         by_dominance=target == dominant(candidates))]

        # 根拠の URL は、実際に取得した検索結果・項目のものだけを認める（設計書 M-4 の簡易版）
        pages = {**state.get("pages", {}),
                 **{w.url: w.title for w in web.results},
                 **{e.url: f"Wikidata: {e.label}" for e in entities}}
        # LLM には URL を書き写させず短い参照（W1 / Q308）で答えさせ、プログラムが URL に戻す（出力を短くするため）
        refs = {f"W{i}": w.url for i, w in enumerate(web.results, 1)} | {e.id: e.url for e in entities}
        known = [{"text": k["text"], "source": refs.get(k.get("source", "").strip(), "")} for k in update["known"]]

        result: dict = {
            "known": known,
            "unknown": update["unknown"],
            "candidates": candidates,
            "target": target,
            "assumptions": assumptions,
            "searched": state.get("searched", []) + [query],
            "unresponsive": sorted(set(state.get("unresponsive", []) + web.unresponsive)),
            "pages": pages,
            # 使えなかった道具は記録して、報告で申告する（調べ尽くしていないのに調べたように見せない。設計書 G-6）
            "failures": list(dict.fromkeys(state.get("failures", []) + ([web_failed] if web_failed else [])
                                           + [f"Wikidata の検索に失敗した（{x.split(':')[0]}）"
                                              for x in errors if x.startswith("wikidata")])),
            "web_failures_in_row": state.get("web_failures_in_row", 0) + 1 if web_failed else 0,
        }

        # 対象が決まったら、Wikipedia の概要を取る（Tier 2）
        current = result["target"]
        if current and current.get("id") and not state.get("target_summary"):
            try:
                ws = wikimedia.summary_for_entity(current["id"])
            except Exception as e:  # noqa: BLE001
                ws = None
                errors.append(f"wikipedia: {e}")
            if ws:
                pages[ws.url] = f"Wikipedia: {ws.title}"
                result.update(target_summary=ws.extract, summary_url=ws.url)

        # 対象が決まっている（または曖昧でない）ときの検索結果を、深掘りするページの候補にする。
        # 曖昧なまま集めた結果は、別の対象のページが混ざるので使わない
        if not is_ambiguous({**state, **result}):
            first = [result["summary_url"]] if result.get("summary_url") else []
            urls = first + [w.url for w in web.results]
            if not state.get("tier1_searched"):
                # Tier 1（公的機関・大学）のページを狙って探す。横の展開なのでプログラムが行う（設計書 6.3）
                name = current["label"] if current else query
                for site in TIER1_SITES:
                    try:
                        found = searxng.web_search(self.settings.searxng_url, f"{name} site:{site}", limit=5)
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"searxng({site}): {e}")
                        continue
                    urls += [w.url for w in found.results]
                    pages.update({w.url: w.title for w in found.results if w.url not in pages})
                    result["unresponsive"] = sorted(set(result["unresponsive"] + found.unresponsive))
                result["tier1_searched"] = True
            # 上の Tier から読む。Tier 5（転載・まとめ）は読まない
            result["reading_list"] = by_tier(state.get("reading_list", []) + urls)

        log.info("search: %r names=%s -> wikidata %d, web %d (%.1fs)", query, names, len(entities),
                 len(web.results), time.perf_counter() - started)
        result["trace"] = [{
            "node": "search", "query": query, "names": names, "errors": errors,
            "wikidata": [e.__dict__ for e in entities],
            "web": [w.__dict__ for w in web.results],
            "llm": update,
        }]
        return result

    def read_pages(self, state: AgentState) -> dict:
        """ページを 1 つずつ読み、引用付きで事実を抜き出し、引用を本文と照合する（縦の調査）。"""
        done = {p["url"] for p in state.get("pages_read", [])}
        urls = [u for u in state.get("reading_list", []) if u not in done][:PAGES_PER_READ]
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:   # LLM 側の同時実行数の上限と合わせる
            results = list(pool.map(lambda u: self._read_one(state, u), urls))

        pages = dict(state.get("pages", {}))
        verified = list(state.get("verified", []))
        reads: list[PageRead] = []
        traces = []
        for page, facts, dropped in results:
            if page.ok and page.title:
                pages.setdefault(page.url, page.title)
            verified += facts
            reads.append({"url": page.url, "ok": page.ok, "reason": page.reason,
                          "extracted": len(facts) + len(dropped), "verified": len(facts)})
            traces.append({"url": page.url, "ok": page.ok, "reason": page.reason,
                           "verified": facts, "dropped": dropped})
        log.info("read_pages: %d pages -> verified %d facts (%.1fs)", len(urls),
                 sum(r["verified"] for r in reads), time.perf_counter() - started)
        return {
            "pages_read": state.get("pages_read", []) + reads,
            "verified": verified,
            "pages": pages,
            "trace": [{"node": "read_pages", "pages": traces}],
        }

    def _read_one(self, state: AgentState, url: str):
        page = fetch.fetch_page(url)
        if not page.ok:
            return page, [], []
        text = page.text[:MAX_PAGE_CHARS]
        out = self.llm.chat_json(prompts.EXTRACT_SYSTEM, prompts.extract_user(state, page.title, text),
                                 prompts.EXTRACT_SCHEMA)
        facts, dropped = [], []
        for f in out.get("facts", []):
            # 引用が本文に実在するものだけを認める（設計書 M-4）
            if quote_in_text(f.get("quote", ""), text):
                facts.append({"text": f["text"], "quote": f["quote"], "source": url})
            else:
                dropped.append(f)
        return page, facts, dropped

    def ask_user(self, state: AgentState) -> dict:
        question = state["decision"]["question"]
        answer = str(interrupt({"question": question})).strip()
        result: dict = {"questions_asked": state.get("questions_asked", 0) + 1}

        result["last_question"] = question
        candidates = state.get("candidates", [])

        # 回答を選択肢と照らし合わせて、その場で対象を決める（決めないまま進むと同じ質問を繰り返すため）
        # 番号での回答はプログラムが対応づける（LLM には番号付きの選択肢が見えないため）
        picked = pick_candidate(answer, candidates)
        matched = [picked] if picked else self._match_answer(question, answer, candidates)
        if len(matched) == 1:
            result["target"] = matched[0]
            answer_text = f"{answer}（{matched[0]['label']}: {matched[0]['description']}）"
        elif len(matched) >= 2:
            # 回答に合う候補が複数（例:「山です」）→ 候補を絞る。一般的な意味が 1 つに偏っていればそれを選ぶ
            result["candidates"] = matched
            answer_text = f"{answer}（該当しうる候補: " + "、".join(c["label"] + "/" + c["description"] for c in matched) + "）"
            if primary := dominant(matched):
                result["target"] = primary
                result["assumptions"] = state.get("assumptions", []) + [
                    assumption_text(primary, matched)]
        else:
            answer_text = answer   # 選択肢にない内容（例:「川のほうです」）→ 次の検索で対象を探す
        result["clarifications"] = state.get("clarifications", []) + [answer_text]
        result["trace"] = [{"node": "ask_user", "question": question, "answer": answer,
                            "matched": [c["id"] for c in matched], "target": result.get("target")}]
        return result

    def _match_answer(self, question: str, answer: str, candidates: list[Candidate]) -> list[Candidate]:
        """文章での回答が、どの選択肢を指しているかを LLM に判定させる（1 回の呼び出しで 1 件だけ扱う）。"""
        if not candidates:
            return []
        out = self.llm.chat_json(prompts.MATCH_SYSTEM, prompts.match_user(question, answer, candidates),
                                 prompts.MATCH_SCHEMA)
        valid = {c["id"]: c for c in candidates}
        return [valid[i] for i in dict.fromkeys(out.get("matched_ids", [])) if i in valid]

    def respond(self, state: AgentState) -> dict:
        sources = collect_sources(state)
        body = self.llm.chat_text(prompts.RESPOND_SYSTEM, prompts.respond_user(state, sources))
        parts = [body.strip()]
        if state.get("failures"):
            parts.append("\n※ 調査中に使えなかったもの（その分、調べ尽くせていない可能性があります）:\n"
                         + "\n".join(f"- {f}" for f in state["failures"]))
        if state.get("unresponsive"):
            parts.append(f"\n※ SearXNG で応答しなかった検索エンジン: {', '.join(state['unresponsive'])}")
        if sources:
            parts.append("\n### 出典\n" + "\n".join(
                f"[{i}] {s['title']} — {s['url']}（Tier {s['tier']}）" for i, s in enumerate(sources, 1)))
        answer = "\n".join(parts)
        return {"answer": answer, "trace": [{"node": "respond", "answer": answer}]}


# 依頼文によく出る決まり文句（対象を絞り込む根拠にならない）
_GENERIC_PHRASES = ("について", "に関して", "調べて", "教えて", "知りたい", "ください", "とは", "の情報", "情報")


def evidence_ok(evidence: str, request: str, label: str) -> bool:
    """LLM が示した根拠の語句が、依頼文にあり、対象の名前や決まり文句以外の言葉を含むか。"""
    e = normalize(evidence)
    if not e or e not in normalize(request):
        return False
    rest = e.replace(normalize(label), "")
    for phrase in _GENERIC_PHRASES:
        rest = rest.replace(phrase, "")
    return len(rest.strip("のをはがにでとや、。")) >= 2


def as_candidate(e: wikimedia.Entity) -> Candidate:
    return {"id": e.id, "label": e.label, "description": e.description, "sitelinks": e.sitelinks}


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """名前のない候補を外し、一般的に知られている順（sitelinks の多い順）に並べる。"""
    return sorted([c for c in candidates if c.get("label")], key=lambda c: -c.get("sitelinks", 0))


def dominant(candidates: list[Candidate]) -> Candidate | None:
    """1 位が 2 位より圧倒的に一般的なら、その候補を返す（DOMINANCE_RATIO）。"""
    ranked = rank(candidates)
    if len(ranked) < 2:
        return None
    first, second = ranked[0].get("sitelinks", 0), ranked[1].get("sitelinks", 0)
    return ranked[0] if first >= DOMINANCE_RATIO * max(second, 1) else None


def assumption_text(chosen: Candidate, candidates: list[Candidate], by_dominance: bool = True) -> str:
    others = [f"{c['label']}（{c['description']}）" for c in candidates if c["id"] != chosen["id"]][:3]
    name = f"「{chosen['label']}（{chosen['description']}）」"
    if by_dominance:
        # 依頼文は長いことがあるため、対象の名前で書く（例:「富士山」は一般に…）
        text = f"「{chosen['label']}」は一般に{name}を指すため、それとして調べました。"
    else:
        text = f"依頼の内容から{name}のことと判断して調べました。"
    return text + (f"別のもの（{'、'.join(others)} など）であれば教えてください。" if others else "")


def pick_candidate(answer: str, candidates: list[Candidate]) -> Candidate | None:
    """「2」「2番」のような番号だけの回答なら、その候補を返す。"""
    digits = answer.translate(str.maketrans("０１２３４５６７８９", "0123456789")).rstrip("番 .")
    if digits.isdigit() and 1 <= int(digits) <= len(candidates):
        return candidates[int(digits) - 1]
    return None


def collect_sources(state: AgentState) -> list[Source]:
    """回答で使う出典を、概要 → わかったことの順に重複なく並べる（出典番号はこの順）。"""
    pages = state.get("pages", {})
    urls = [state["summary_url"]] if state.get("target_summary") and state.get("summary_url") else []
    urls += [k["source"] for k in state.get("verified", [])]
    ordered = list(dict.fromkeys(urls))
    return [{"title": pages.get(u, u), "url": u, "tier": tier_of(u)} for u in ordered]


def infeasible_report(request: str, f: dict) -> str:
    parts = f.get("infeasible_parts") or [request]
    lines = ["この依頼は、公開されている情報からは調べられないと判断しました。", "",
             "### 調べられないこと"]
    lines += [f"- {p}" for p in parts]
    lines += ["", "### 理由", f.get("reason", "")]
    lines += ["", "### 試したこと",
              "公開情報（Web・百科事典など）の検索は行っていません。公開されていない情報は、"
              "検索しても見つからないか、関係のない情報を誤って示してしまうおそれがあるためです。"]
    if f.get("suggestions"):
        lines += ["", "### 代わりにできること"] + [f"- {s}" for s in f["suggestions"]]
    lines += ["", "公開されている情報で調べられる形（例: 対象の名前や、公開されている資料）があれば、教えてください。"]
    return "\n".join(lines)


def route_feasibility(state: AgentState) -> str:
    return "report_infeasible" if state["feasibility"]["verdict"] == "no" else "decide"


def route(state: AgentState) -> str:
    return state["decision"]["action"]
