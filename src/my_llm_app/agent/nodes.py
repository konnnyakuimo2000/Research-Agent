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
from urllib.parse import unquote, urlparse

from langgraph.types import interrupt

from my_llm_app.agent import coverage as cov
from my_llm_app.agent import imaging
from my_llm_app.agent import prompts
from my_llm_app.agent.guard import check_decision, is_ambiguous
from my_llm_app.agent.quotes import normalize, quote_in_text
from my_llm_app.agent.state import (DOMINANCE_RATIO, MAX_CANDIDATES, MAX_IMAGE_CHECKS, MAX_REF_DEPTH,
                                    PAGES_PER_READ, REFS_PER_PAGE, WANTED_IMAGES, AgentState,
                                    Candidate, PageRead, Source)
from my_llm_app.config import Settings
from my_llm_app.llm import LLM
from my_llm_app.tiers import by_tier, tier_of
from my_llm_app.tools import fetch, images, searxng, wikimedia

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
        return {"feasibility": feasibility, "wants_images": imaging.wants_images(state["request"]),
                "trace": [{"node": "check_feasibility", "llm": out}]}

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
            # 接続はできても、検索エンジンがアクセスの多さで一時的に止めていると結果が 0 件になる。
            # これも失敗として数える（同じ状態で検索を繰り返さず、報告で申告する）
            web_failed = "" if web.results or not web.unresponsive else (
                f"Web 検索の結果が 0 件だった（応答しない検索エンジン: {', '.join(web.unresponsive)}。"
                "アクセスが多すぎて一時的に止められている可能性がある）")

        update = self.llm.chat_json(
            prompts.UPDATE_SYSTEM,
            prompts.update_user(state, query, entities, web.results),
            prompts.UPDATE_SCHEMA,
        )

        # LLM には id を選ばせるだけにし、実際に取得した項目にある id だけを認める。
        # 名前と説明は Wikidata の実データで埋める（LLM の書き写し間違い・作り話の防止）
        valid_ids = list(dict.fromkeys([e.id for e in entities] + [c["id"] for c in state.get("candidates", [])]))
        chosen = [i for i in update.get("candidate_ids", []) if i in valid_ids]
        if not state.get("target"):
            # 名前が完全に一致した項目は、LLM が選ばなくても同名の候補に入れる（横の網羅はプログラム）。
            # LLM は「マーキュリー」と表記された項目だけを選び、英語名 Mercury で一致する水星を落とすことがある
            exact = {normalize(n).lower() for n in names}
            chosen = list(dict.fromkeys(chosen + [e.id for e in entities if normalize(e.matched).lower() in exact]))
        target_id = update.get("target_id", "")
        target_id = target_id if target_id in valid_ids else ""
        try:
            info = wikimedia.get_entities(list(dict.fromkeys(chosen + [target_id] if target_id else chosen)))
        except Exception as e:  # noqa: BLE001
            errors.append(f"wikidata(get): {e}")
            info = {e.id: e for e in entities}
        candidates = rank([as_candidate(info[i]) for i in chosen if i in info])[:MAX_CANDIDATES] \
            or state.get("candidates", [])
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
            "aspect_searched": state.get("aspect_searched", []) + ([decision["aspect"]] if decision.get("aspect") else []),
            "conflict_searched": state.get("conflict_searched", [])
            + ([decision["conflict_topic"]] if decision.get("conflict_topic") else []),
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
            urls = first + [w.url for w in web.results if not fetch.is_unreadable(w.url)]
            if not state.get("tier1_searched"):
                # Tier 1（公的機関・大学）のページを狙って探す。横の展開なのでプログラムが行う（設計書 6.3）
                name = current["label"] if current else query
                for site in TIER1_SITES:
                    try:
                        found = searxng.web_search(self.settings.searxng_url, f"{name} site:{site}", limit=5)
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"searxng({site}): {e}")
                        continue
                    urls += [w.url for w in found.results if not fetch.is_unreadable(w.url)]
                    pages.update({w.url: w.title for w in found.results if w.url not in pages})
                    result["unresponsive"] = sorted(set(result["unresponsive"] + found.unresponsive))
                result["tier1_searched"] = True
            # 上の Tier から読む。Tier 5（転載・まとめ）は読まない
            result["reading_list"] = by_tier(state.get("reading_list", []) + urls)
            if decision.get("aspect") or decision.get("conflict_topic"):
                # 観点や食い違いを狙って探したページは、他より先に読む（R-1, R-6）
                read = {p["url"] for p in state.get("pages_read", [])}
                result["priority_urls"] = by_tier(state.get("priority_urls", [])
                                                  + [w.url for w in web.results
                                                     if w.url not in read and not fetch.is_unreadable(w.url)])

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
        extra: dict = {}
        if not state.get("aspects"):
            # 対象の種類だけを LLM に選ばせ、調べる観点はプログラムがチェックリストから展開する（設計書 6.1）
            out = self.llm.chat_json(prompts.CLASSIFY_SYSTEM, prompts.classify_user(state, cov.type_descriptions()),
                                     prompts.classify_schema(cov.type_names()))
            subject_type = out.get("subject_type", cov.OTHER_TYPE)
            aspects = cov.aspects_for(subject_type)
            extra = {"subject_type": subject_type, "aspects": aspects,
                     "coverage": cov.build_coverage(aspects, state.get("verified", []))}
            log.info("subject_type: %s -> aspects %s", subject_type, [a["name"] for a in aspects])
            state = {**state, **extra}

        done = {p["url"] for p in state.get("pages_read", [])}
        queue = list(dict.fromkeys(state.get("priority_urls", []) + state.get("reading_list", [])))
        urls = [u for u in queue if u not in done][:PAGES_PER_READ]
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:   # LLM 側の同時実行数の上限と合わせる
            results = list(pool.map(lambda u: self._read_one(state, u), urls))

        pages = dict(state.get("pages", {}))
        verified = list(state.get("verified", []))
        page_imgs = list(state.get("page_images", []))
        depth = dict(state.get("link_depth", {}))
        new_links: list[str] = []
        others: list[str] = []
        reads: list[PageRead] = []
        traces = []
        for page, facts, dropped, other in results:
            if other:
                others.append(other)
            if facts and depth.get(page.url, 0) < MAX_REF_DEPTH:
                # R-2: 事実が取れたページの出典欄などから、より信頼度の高いページをたどる
                for link in follow_links(page, self._target_name(state), set(state.get("reading_list", []))):
                    depth[link] = depth.get(page.url, 0) + 1
                    new_links.append(link)
            if page.ok and page.title:
                pages.setdefault(page.url, page.title)
            verified += facts
            page_imgs += [{**im, "page_url": page.url, "title": page.title} for im in page.images]
            reads.append({"url": page.url, "ok": page.ok, "reason": page.reason,
                          "extracted": len(facts) + len(dropped), "verified": len(facts)})
            traces.append({"url": page.url, "ok": page.ok, "reason": page.reason,
                           "verified": facts, "dropped": dropped})
        log.info("read_pages: %d pages -> verified %d facts (%.1fs)", len(urls),
                 sum(r["verified"] for r in reads), time.perf_counter() - started)
        coverage = cov.build_coverage(state["aspects"], verified)
        gained = len(verified) - len(state.get("verified", []))
        if (others and not state.get("ambiguity_asked") and state.get("assumptions")
                and not state.get("clarifications")):
            # R-5: 聞き返さずに決めた対象なのに、同名の別の対象のページが見つかった → 確認する
            extra["new_ambiguity"] = others[0]
        return {
            **extra,
            "reading_list": by_tier(state.get("reading_list", []) + new_links),
            "link_depth": depth,
            "stale_reads": 0 if gained else state.get("stale_reads", 0) + 1,
            "pages_read": state.get("pages_read", []) + reads,
            "verified": verified,
            "page_images": page_imgs,
            "coverage": coverage,
            "pages": pages,
            "trace": [{"node": "read_pages", "pages": traces, "coverage": coverage, "followed": new_links,
                       "other_entities": others}],
        }

    def _read_one(self, state: AgentState, url: str):
        page = fetch.fetch_page(url)
        if not page.ok:
            return page, [], [], ""
        text = page.text[:MAX_PAGE_CHARS]
        names = [a["name"] for a in state.get("aspects", [])]
        out = self.llm.chat_json(prompts.EXTRACT_SYSTEM, prompts.extract_user(state, page.title, text),
                                 prompts.extract_schema(names))
        facts, dropped = [], []
        for f in out.get("facts", []):
            # 引用が本文に実在するものだけを認める（設計書 M-4）
            if quote_in_text(f.get("quote", ""), text):
                aspect = f.get("aspect") if f.get("aspect") in names else cov.OTHER_ASPECT
                facts.append({"text": f["text"], "quote": f["quote"], "aspect": aspect, "source": url})
            else:
                dropped.append(f)
        return page, facts, dropped, out.get("other_entity", "").strip()

    @staticmethod
    def _target_name(state: AgentState) -> str:
        return (state.get("target") or {}).get("label") or state.get("request", "")

    def check_conflicts(self, state: AgentState) -> dict:
        """観点ごとに、別々のサイトの事実どうしが食い違っていないかを調べる（設計書 7.4 T-3）。"""
        groups = cov.facts_to_compare(state.get("verified", []))
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:   # 1 回の呼び出しで 1 つの観点だけを扱う（M-1）
            outs = list(pool.map(lambda item: self.llm.chat_json(
                prompts.CONFLICT_SYSTEM, prompts.conflict_user(*item), prompts.CONFLICT_SCHEMA), groups.items()))

        conflicts = []
        for (aspect, facts), out in zip(groups.items(), outs):
            for c in out.get("conflicts", []):
                if c.get("kind") != prompts.REAL_CONFLICT:
                    continue   # 丸め・概数の違いや、別の事柄・時点の組は食い違いとしない
                # 番号は実在する事実のものだけを認め、同じサイトどうしの組は除く（プログラムで検査）
                picked = [facts[i - 1] for i in dict.fromkeys(c.get("fact_ids", [])) if 1 <= i <= len(facts)]
                if len(picked) >= 2 and cov.different_sites(picked):
                    conflicts.append({"aspect": aspect, "topic": c.get("topic", ""), "facts": picked})
        conflicts = cov.merge_conflicts(conflicts)
        log.info("check_conflicts: %d aspects -> %d conflicts (%.1fs)", len(groups), len(conflicts),
                 time.perf_counter() - started)
        return {"conflicts": conflicts, "conflicts_checked_at": len(state.get("verified", [])),
                "trace": [{"node": "check_conflicts", "aspects": list(groups), "llm": outs,
                           "conflicts": conflicts}]}

    def find_images(self, state: AgentState) -> dict:
        """画像の候補を複数の方法で集め（横・プログラム）、1 枚ずつ確かめる（縦・LLM）。設計書 8 章。"""
        target = state.get("target") or {}
        name = target.get("label") or state.get("request", "")
        started = time.perf_counter()
        candidates: list[images.ImageCandidate] = []
        counts: dict[str, int] = {}
        failures: list[str] = []

        def gather(stage: str, fn):
            try:
                found = fn()
            except Exception as e:  # noqa: BLE001 - 1 つの方法が使えなくても他の方法で続ける
                found = []
                failures.append(f"画像の候補集め（{stage}）に失敗した（{type(e).__name__}）")
            counts[stage] = len(found)
            candidates.extend(found)

        if target.get("id"):
            gather("L2-Wikidata", lambda: images.wikidata_image(target["id"], name))
        gather("L2-Commons", lambda: images.commons_search(name))
        gather("L1-画像検索", lambda: images.searxng_images(self.settings.searxng_url, name))
        gather("L4-ページ内", lambda: [
            images.ImageCandidate(src=im["src"], page_url=im["page_url"], stage="L4-ページ内",
                                  title=im.get("title", ""), caption=f"{im.get('alt', '')} {im.get('caption', '')}".strip())
            for im in state.get("page_images", [])])

        ranked = imaging.rank(candidates, [name])
        log_rows: list[dict] = []
        accepted: list[dict] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            for start in range(0, min(len(ranked), MAX_IMAGE_CHECKS), 2):
                batch = ranked[start:min(start + 2, MAX_IMAGE_CHECKS)]
                for row in pool.map(lambda c: self._check_image(state, c), batch):
                    log_rows.append(row)
                    if row["level"] != imaging.REJECTED:
                        accepted.append(row)
                if sum(r["level"] == imaging.CONFIRMED for r in accepted) >= WANTED_IMAGES:
                    break

        order = {imaging.CONFIRMED: 0, imaging.PROBABLE: 1}
        accepted.sort(key=lambda r: order[r["level"]])
        log.info("find_images: %d candidates %s -> checked %d, accepted %d (%.1fs)", len(ranked), counts,
                 len(log_rows), len(accepted), time.perf_counter() - started)
        return {
            "images_checked": True,
            "images": accepted[:3],
            "image_log": log_rows,
            "image_counts": counts,
            "failures": list(dict.fromkeys(state.get("failures", []) + failures)),
            "trace": [{"node": "find_images", "counts": counts, "checked": log_rows}],
        }

    def _check_image(self, state: AgentState, c: images.ImageCandidate) -> dict:
        row = {"src": c.src, "page_url": c.page_url, "stage": c.stage, "title": c.title,
               "license": c.license, "tier": tier_of(c.page_url)}
        if not row["license"] and images.is_wikimedia_file(c.src):
            try:
                row["license"] = images.commons_license(c.src)   # Wikimedia の画像なら利用条件を取りに行く
            except Exception:  # noqa: BLE001 - 取れなければ「不明」と書く
                pass
        fetched = images.fetch_image(c.src)
        if not fetched.ok:
            return {**row, "level": imaging.REJECTED, "reason": fetched.reason}
        out = self.llm.chat_json(prompts.IMAGE_SYSTEM, prompts.image_user(state, c.title, c.caption, c.page_url),
                                 prompts.IMAGE_SCHEMA, image_data_url=fetched.data_url)
        level = imaging.judge(row["tier"], True, out.get("context", "low"), out.get("visual", "unknown"))
        return {**row, "level": level, "context": out.get("context"), "visual": out.get("visual"),
                "description": out.get("description", ""), "reason": out.get("reason", ""),
                "size": f"{fetched.width}×{fetched.height}"}

    def ask_user(self, state: AgentState) -> dict:
        question = state["decision"]["question"]
        answer = str(interrupt({"question": question})).strip()
        result: dict = {"questions_asked": state.get("questions_asked", 0) + 1}

        result["last_question"] = question
        if state["decision"].get("confirm_target"):
            return {**result, **confirm_target(state, answer),
                    "trace": [{"node": "ask_user", "question": question, "answer": answer, "confirm": True}]}
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
        parts = [strip_program_sections(body).strip()]
        # 食い違いと確認できなかった観点は、プログラムが書く（LLM に書かせない）
        if state.get("conflicts"):
            number = {s["url"]: i for i, s in enumerate(sources, 1)}
            lines = [f"- {c['aspect']}・{c['topic']}: "
                     + " / ".join(f"「{f['text']}」[{number[f['source']]}]" for f in c["facts"] if f["source"] in number)
                     for c in state["conflicts"]]
            parts.append("\n### 出典どうしの食い違い\n" + "\n".join(lines))
        if gaps := cov.gaps_text(state.get("coverage", [])):
            parts.append("\n### 確認できなかったこと\n" + gaps)
        if state.get("images_checked"):
            parts.append("\n" + images_section(state))
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


# プログラムが付ける節の見出し。LLM が本文に同じ節を書いたら取り除く（同じ内容が二重に出ないように）
_PROGRAM_SECTIONS = ("食い違い", "確認できなかった", "わからなかった", "出典", "参考", "画像")


def _heading_text(line: str) -> str | None:
    """見出しの行（「### 〜」または行全体が太字の「**〜**」）なら、その文字を返す。"""
    s = line.strip()
    if s.startswith("#"):
        return s.lstrip("#").strip()
    if len(s) > 4 and s.startswith("**") and s.endswith("**") and s.count("**") == 2:
        return s[2:-2].strip()
    return None


def strip_program_sections(body: str) -> str:
    """LLM の本文から、プログラムが付ける節（食い違い・確認できなかったこと・出典・画像）を取り除く。"""
    out, skipping = [], False
    for line in body.splitlines():
        heading = _heading_text(line)
        if heading is not None:
            skipping = any(w in heading for w in _PROGRAM_SECTIONS)
        if not skipping:
            out.append(line)
    return "\n".join(out)


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


def follow_links(page: fetch.Page, name: str, known: set[str]) -> list[str]:
    """R-2: ページからたどる参照を選ぶ（プログラム）。出典欄の中のリンクか、文字や URL に対象の名前がある
    リンクのうち、別のサイトで Tier 1〜2 のものを、Tier の高い順に最大 REFS_PER_PAGE 件。"""
    host = urlparse(page.url).hostname
    picked = []
    for link in page.links:
        href = link["href"].split("#")[0]
        if (href in known or href in picked or urlparse(href).hostname == host or tier_of(href) > 2
                or fetch.is_unreadable(href)):
            continue
        if link["in_refs"] or (name and (name in link["text"] or name in unquote(href))):
            picked.append(href)
    return by_tier(picked)[:REFS_PER_PAGE]


def confirm_target(state: AgentState, answer: str) -> dict:
    """R-5 の確認への回答を反映する。「はい」なら前提を確認済みにし、違うなら対象を決め直す。"""
    yes = answer.strip().translate(str.maketrans("１", "1")) in ("1", "1番", "はい", "はい。", "yes")
    if yes:
        t = state["target"]
        return {"ambiguity_asked": True, "assumptions": [],
                "clarifications": state.get("clarifications", []) + [f"{t['label']}（{t['description']}）で合っている"]}
    # 違う対象だった: 集めた事実は別の対象のものなので捨て、対象を決め直す
    return {"ambiguity_asked": True, "target": None, "assumptions": [], "target_summary": "", "summary_url": "",
            "clarifications": state.get("clarifications", []) + [answer],
            "verified": [], "reading_list": [], "priority_urls": [], "pages_read": [], "coverage": [],
            "aspects": [], "conflicts": [], "conflicts_checked_at": -1, "tier1_searched": False, "stale_reads": 0,
            "new_ambiguity": ""}


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


def images_section(state: AgentState) -> str:
    """報告の「画像」（設計書 9.1, 9.2）。採用した画像と、見つからなかった場合の申告をプログラムが書く。"""
    lines = ["### 画像"]
    for img in state.get("images", []):
        note = "" if img["level"] == imaging.CONFIRMED else "（確認しきれていません）"
        lines += [f"![{img.get('description', '')}]({img['src']})",
                  f"- 確信度: {img['level']}{note}",
                  f"- 写っているもの: {img.get('description', '')}",
                  f"- 出典: {img.get('title') or img['page_url']} — {img['page_url']}（Tier {img['tier']}）",
                  f"- ライセンス: {img.get('license') or '不明（掲載ページで確認してください）'}", ""]
    if not state.get("images"):
        counts = state.get("image_counts", {})
        checked = state.get("image_log", [])
        reasons = list(dict.fromkeys(r.get("reason", "") for r in checked if r.get("reason")))[:3]
        lines += ["対象の画像として確認できるものは見つかりませんでした。",
                  "- 試したこと: " + "、".join(f"{k} {v} 件" for k, v in counts.items())
                  + f" の候補から {len(checked)} 件を確かめ、採用 0 件",
                  "- 主な不採用の理由: " + ("／".join(reasons) if reasons else "候補がなかった"),
                  "- 次にできること: 対象の公式サイトや所蔵機関に問い合わせる。お持ちの画像があれば、それが対象かを確認できます"]
    return "\n".join(lines).rstrip()


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
