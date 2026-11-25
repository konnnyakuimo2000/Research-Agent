"""グラフのノード。

- decide:   LLM が次の行動を選び、プログラムが検査する（guard.py）
- search:   Wikidata（同名候補）と SearXNG（Web）を調べ、LLM が状態を更新する
- ask_user: 利用者に聞き返す（interrupt で一時停止）
- respond:  集めた材料から回答を作る
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import unquote, urlparse

from langgraph.types import interrupt

from research_agent.agent import coverage as cov
from research_agent.agent import imaging
from research_agent.agent import name_facts
from research_agent.agent import prompts
from research_agent.agent import query_plan
from research_agent.agent import reprints
from research_agent.agent import telemetry
from research_agent.agent.guard import check_decision, has_choices, is_ambiguous, pages_to_read, time_over
from research_agent.agent.page_choice import choose_pages, keep_for_target
from research_agent.agent.report import (SourceTable, aspect_headings, assemble, cited_db_records, collect_sources,
                                         unconfirmed_sources,  # noqa: F401
                                         drop_sentences_with, images_section, infeasible_report, sources_text,
                                         strip_program_sections, unsupported_names, _image_lines)
from research_agent.agent.names import abbreviations_in, mentions, same_name
from research_agent.agent.quotes import normalize, quote_in_text
from research_agent.agent.state import (DOMINANCE_RATIO, MAX_CANDIDATES, MAX_FEATURES, MAX_IMAGE_CHECKS,
                                    MAX_IMAGE_FETCHES, MAX_REF_DEPTH, MAX_REFERENCE_IMAGES,
                                    PAGES_PER_READ, REFS_PER_PAGE, WANTED_IMAGES, AgentState,
                                    Candidate, PageRead, setting)
from research_agent.config import Settings
from research_agent.llm import LLM, ContextPool, LLMOutputError
from research_agent.tiers import adjusted_tier, by_tier, canonical, host_of, page_tier, tier_of
from research_agent.tools import databases, fetch, images, searxng, wikimedia

L3_STAGE = "L3-分野別データベース"

# LLM に渡す本文の長さの上限（文字数）。長いページは先頭から
MAX_PAGE_CHARS = 8000
# 対象が決まったときに site: で狙って探す Tier 1 のドメイン
TIER1_SITES = ("go.jp", "ac.jp")

log = logging.getLogger(__name__)


class Nodes:
    def __init__(self, settings: Settings, llm: LLM) -> None:
        self.settings = settings
        self.llm = llm

    def _llm(self, role: str, state: AgentState | None = None):
        """役割（decide・extract・vision・report）のモデルで頼む部品。依頼ごとの選び直し（state["models"]）に従う。

        役割で頼めない LLM（テストの偽物など）は、そのまま使う。
        """
        for_role = getattr(self.llm, "for_role", None)
        state = state or {}
        return for_role(role, state.get("models"), state.get("budget")) if for_role else self.llm

    def check_feasibility(self, state: AgentState) -> dict:
        """公開情報で調べられる依頼かを、調査を始める前に判定する（設計書 4 章 ④、例 D）。"""
        out = self._llm("decide", state).chat_json(prompts.FEASIBILITY_SYSTEM, state["request"], prompts.FEASIBILITY_SCHEMA)
        feasibility = {
            "verdict": out.get("verdict", "unsure"),
            "reason": out.get("reason", ""),
            "infeasible_parts": out.get("infeasible_parts", []),
            "suggestions": out.get("suggestions", []),
        }
        log.info("feasibility: %s (%s)", feasibility["verdict"], feasibility["reason"])
        telemetry.emitter()("feasibility", **feasibility)
        wants = setting(state, "images") and imaging.wants_images(state["request"])   # 設定で切れる
        return {"feasibility": feasibility, "wants_images": wants,
                "trace": [{"node": "check_feasibility", "llm": out}]}

    def report_infeasible(self, state: AgentState) -> dict:
        """調べられない依頼を、探し回らずに正直に申告する（設計書 9.2 の形）。文面はプログラムが作る。"""
        answer = infeasible_report(state["request"], state["feasibility"])
        return {"answer": answer, "trace": [{"node": "report_infeasible", "answer": answer}]}

    def decide(self, state: AgentState) -> dict:
        raw = self._llm("decide", state).chat_json(prompts.DECIDE_SYSTEM, prompts.decide_user(state), prompts.DECIDE_SCHEMA)
        decision = check_decision(state, raw)
        telemetry.emitter()("decision", llm={k: raw.get(k) for k in ("action", "reason", "query")},
                            action=decision["action"], query=decision.get("query", ""), guard=decision.get("guard", ""),
                            aspect=decision.get("aspect", ""), site=decision.get("site", ""), plan=decision.get("plan"))
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
        ev = telemetry.emitter()
        kind = ("site" if decision.get("site") else "aspect" if decision.get("aspect")
                else "conflict" if decision.get("conflict_topic") else "free")
        ev("search.start", query=query, kind=kind, aspect=decision.get("aspect", ""), site=decision.get("site", ""))
        relevant: list = []

        # Wikidata は名前の前方一致で引くため、長い検索文ではなく短い名前で引く（横の展開はプログラム）。
        # 人を指す依頼では、検索語の最初の語（「九大」などの手がかり）ではなく、人の名前で引く
        person, clues = query_plan.request_person(state["request"])
        names = [n.strip() for n in decision.get("names", []) if n.strip()] or (
            [person] if person and person in query else [query.split()[0]])
        if person:
            # LLM が所属の名前（「九州大学」）も渡すことがある。人の名前・決めた対象の名前・利用者が答えた名前を
            # 含むものだけで引く（所属で引くと、大学そのものが候補になり、対象に選ばれてしまう）
            known = [person, (state.get("target") or {}).get("label", "")] + state.get("clarifications", [])
            names = [n for n in names if any(k and (mentions(n, [k]) or mentions(k, [n])) for k in known)] or [person]
        entities: list[wikimedia.Entity] = []
        for name in names[:3]:
            try:
                found = wikimedia.search_entities(name, limit=8)
            except Exception as e:  # noqa: BLE001 - 失敗は記録して続ける
                errors.append(f"wikidata({name}): {e}")
                continue
            have = {x.id for x in entities}
            entities += [e for e in found if e.id not in have]
        if person and clues:
            # 手がかり（所属など）の言葉に一致した項目は、人の候補にしない（「九大」→ 九州大学）
            dropped_ids = [e.id for e in entities if is_clue_entity(e, person, clues)]
            if dropped_ids:
                errors.append(f"clue entities dropped: {dropped_ids}")
            entities = [e for e in entities if e.id not in dropped_ids]
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

        update = self._llm("extract", state).chat_json(
            prompts.UPDATE_SYSTEM,
            prompts.update_user(state, query, entities, web.results),
            prompts.UPDATE_SCHEMA,
        )

        # LLM には id を選ばせるだけにし、実際に取得した項目にある id だけを認める。
        # 名前と説明は Wikidata の実データで埋める（LLM の書き写し間違い・作り話の防止）
        valid_ids = list(dict.fromkeys([e.id for e in entities] + [c["id"] for c in state.get("candidates", [])
                                                                    if c.get("id")]))
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
        if person and info:
            # 人を指す依頼（「青学の伊藤先生」）では、人（P31 = Q5）でない項目を候補にも対象にもしない。
            # 前は同じ名前の小惑星「伊藤」が、Wikipedia の記事の多さで対象に選ばれていた
            try:
                people = wikimedia.humans(list(info))
            except Exception as e:  # noqa: BLE001 - 確かめられなければ、名前だけの項目は対象にしない
                errors.append(f"wikidata(humans): {e}")
                people = set()
            if dropped := [i for i in info if i not in people]:
                errors.append(f"non-human entities dropped: {dropped}")
            info = {i: e for i, e in info.items() if i in people}
            chosen = [i for i in chosen if i in info]
            target_id = target_id if target_id in info else ""
        candidates = rank([as_candidate(info[i]) for i in chosen if i in info])[:MAX_CANDIDATES] \
            or state.get("candidates", [])
        target = state.get("target") or (as_candidate(info[target_id]) if target_id in info else None)
        if (target and not state.get("target") and not state.get("clarifications") and len(candidates) >= 2
                and target != dominant(candidates)
                and not evidence_ok(update.get("target_evidence", ""), state["request"], target["label"])):
            # 利用者が答えていないのに LLM が対象を決めた。依頼文に根拠となる語句がなければ認めない（推測を防ぐ）
            errors.append(f"target rejected: evidence={update.get('target_evidence', '')!r}")
            target = None
        subject = update.get("specific_subject", "").strip()
        titles = " ".join(w.title for w in web.results) + " " + " ".join(e.label for e in entities)
        found_text = titles + " " + " ".join(w.snippet for w in web.results)
        from_user = bool(subject) and any(mentions(c, [subject]) for c in state.get("clarifications", []))
        if subject and not from_user and not mentions(titles, [subject]):
            # 利用者が答えた名前（「松田泰治」）はそのまま使う。題名で確かめるのは、LLM が作った名前を防ぐため。
            # 名前は検索結果の題名にある表記を使う。LLM が作った読み方（例: 実在しないカタカナ表記）や、
            # 抜粋にだけある別の文字の表記で対象を決めると、その名前で検索し、その名前で読むページを絞ってしまう
            # （題名の「DÜRST, Martin J.」が抜粋の「テュールスト マーティン ヤコブ」と一致しない）。1 回だけ書き直させる
            errors.append(f"specific_subject not in titles: {subject!r}")
            fixed = self._llm("extract", state).chat_json(prompts.SUBJECT_FIX_SYSTEM, prompts.subject_fix_user(subject, web.results),
                                       prompts.SUBJECT_FIX_SCHEMA).get("specific_subject", "").strip()
            if fixed and mentions(titles, [fixed]):
                subject = fixed
            elif not mentions(found_text, [subject]):
                errors.append(f"specific_subject rejected: {fixed!r}")
                subject = ""                    # 抜粋にもない名前は使わない。抜粋にある名前なら、そのまま使う
        # 根拠は依頼文と、利用者の答え（「松田泰治」）から探す。前は答えた後にこの道を通らなかったので、
        # Wikidata にない人（大学の先生の多く）は、名前を答えても対象が決まらなかった
        # 略称（「九大」）は正式な名前（「九州大学」）でも照らし合わせる
        context = " ".join([state["request"]] + state.get("clarifications", []) + abbreviations_in(state["request"]))
        # 利用者が答えた名前（「伊藤雄一」）なら、それ自体が根拠（前は名前だけの根拠を認めず、答えても対象が決まらないまま
        # 同じ名字の別の先生のページも読み、3 人の事実を 1 つの報告に混ぜていた。2026-11-25 の伊藤先生）
        if (not target and not state.get("target") and subject
                and (from_user or evidence_ok(update.get("specific_evidence", ""), context, subject))):
            # 依頼文の条件（所属・型番など）で、検索結果の対象が 1 つに決まった。同名の別の意味は聞き返さない
            same = [c for c in candidates if normalize(c["label"]) == normalize(subject)]
            target = same[0] if same else {"id": "", "label": subject,
                                           "description": update["specific_evidence"].strip(), "sitelinks": 0}
            candidates = [target]
        assumptions = state.get("assumptions", [])
        if not target and (primary := dominant(candidates)):
            target = primary
        if target and not state.get("target") and not state.get("clarifications") and not subject:
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
            "rephrase_searched": state.get("rephrase_searched", []) + ([query] if decision.get("rephrase") else []),
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
        if current and current.get("id") and not state.get("official_checked"):
            # 対象自身・メーカーの公式サイト（Wikidata の P856）は Tier 1 として扱い、site: で狙う（設計書 6.3）
            try:
                official = wikimedia.official_sites(current["id"])
            except Exception as e:  # noqa: BLE001
                official = []
                errors.append(f"wikidata(official): {e}")
            hosts = list(dict.fromkeys(host_of(u) for u in official if host_of(u)))
            result.update(official_checked=True, official_hosts=hosts, official_urls=official[:1])

        # 対象が決まっている（または曖昧でない）ときの検索結果を、深掘りするページの候補にする。
        # 曖昧なまま集めた結果は、別の対象のページが混ざるので使わない
        if not is_ambiguous({**state, **result}):
            # 題名・抜粋に対象の名前がある結果だけを読む（観点の言葉で無関係なページが混ざるため）。
            # 対象がまだ決まっていなければ、検索に使った名前で見る（「EE-3」を含まない会社のページなどを読まない）。
            # どれを読み、どれを先に読むかは page_choice.choose_pages（副作用なし）で決める
            # 対象が決まっていれば、対象の名前の一部でしかない名前（対象「伊藤雄一」に対する「伊藤」）では見ない
            # （同じ名字の別の先生のページを読んでいた）。別の表記（英語名など）は残す
            label = current["label"] if current else ""
            names_for_match = ([label] + [n for n in names if not (mentions(label, [n]) and normalize(n) != normalize(label))]
                               if current else list(names))
            choice = choose_pages(state, result, web.results, decision, names_for_match)
            relevant, urls = choice.relevant, list(choice.urls)
            if not state.get("tier1_searched"):
                # Tier 1（公的機関・大学）のページを狙って探す。横の展開なのでプログラムが行う（設計書 6.3）
                name = current["label"] if current else query
                names_for_match = names_for_match or [name] + list(names)
                for site in TIER1_SITES:
                    try:
                        found = searxng.web_search(self.settings.searxng_url, f"{name} site:{site}", limit=5)
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"searxng({site}): {e}")
                        continue
                    # site: 検索は対象と関係ないページも返すので、題名・抜粋に対象の名前があるものだけを読む
                    hits = [w for w in found.results if mentions(f"{w.title} {w.snippet}", names_for_match)]
                    urls += [w.url for w in hits if not fetch.is_unreadable(w.url)]
                    pages.update({w.url: w.title for w in hits if w.url not in pages})
                    result["unresponsive"] = sorted(set(result["unresponsive"] + found.unresponsive))
                result["tier1_searched"] = True
            # 上の Tier から読む。Tier 5（転載・まとめ）は読まない
            earlier = state.get("reading_list", [])
            if current and not state.get("target"):
                # この検索で対象が決まった: 決まる前に入れた、別の人・物のページ（題名に対象の名前がない）は読まない
                earlier = keep_for_target(earlier, pages, names_for_match)
                result["priority_urls"] = keep_for_target(state.get("priority_urls", []), pages, names_for_match)
            result["reading_list"] = by_tier(earlier + urls)
            if choice.priority is not None:
                result["priority_urls"] = (keep_for_target(choice.priority, pages, names_for_match)
                                           if current and not state.get("target") else choice.priority)
            # この検索で初めて見つかった、対象のページの数（次の検索を選ぶときの「空振り」の判定に使う）
            result.update(query_plan.harvest(state, web.results, current["label"] if current else "",
                                             {**decision, "query": query}, choice.new_relevant))
            if result.get("official_hosts"):
                # 公式サイトは、事実が取れる前から site: 検索の候補にする
                result["seen_sites"] = list(dict.fromkeys(result["seen_sites"] + result["official_hosts"]))

        log.info("search: %r names=%s -> wikidata %d, web %d (%.1fs)", query, names, len(entities),
                 len(web.results), time.perf_counter() - started)
        tstate = {**state, **result}
        hits = {w.url for w in relevant}
        ev("search.done", query=query, kind=kind, failed=web_failed, retried=web.retried, unresponsive=web.unresponsive,
           results=[{**telemetry.page_info(w.url, tstate), "title": w.title, "relevant": w.url in hits}
                    for w in web.results],
           candidates=[{"id": c["id"], "label": c["label"], "description": c["description"]} for c in candidates],
           target=result.get("target"), summary_url=result.get("summary_url", ""),
           official=result.get("official_hosts", []), variants=sorted(result.get("name_variants", {}))[:8])
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
        ev = telemetry.emitter()
        if not state.get("aspects"):
            # 観点は LLM が依頼に合わせて作る（種類のチェックリストがお手本）。
            # 確かめるのはプログラム。使えない出力ならチェックリストに戻す（設計書 6.1）
            out = self._llm("decide", state).chat_json(prompts.CLASSIFY_SYSTEM, prompts.classify_user(state, cov.type_guides()),
                                     prompts.classify_schema(cov.type_names()))
            subject_type = out.get("subject_type")
            if subject_type not in cov.type_names():
                subject_type = cov.OTHER_TYPE
            aspects, origin = cov.make_aspects(out.get("aspects"), subject_type)
            extra = {"subject_type": subject_type, "aspects": aspects, "aspects_origin": origin,
                     "coverage": cov.build_coverage(aspects, state.get("verified", []), state)}
            log.info("subject_type: %s -> aspects (%s) %s", subject_type, origin, [a["name"] for a in aspects])
            ev("aspects", subject_type=subject_type, origin=origin, aspects=[a["name"] for a in aspects])
            state = {**state, **extra}
        if state.get("target") and state.get("subject_type") and not state.get("databases_checked"):
            extra.update(self._lookup_databases(state, ev))
            state = {**state, **extra}

        # 読んだページは、要求した URL と転送先の URL の両方で既読とする（同じ転送元・転送先を読み直さない）。
        # 読むかどうかの判断（guard.research_step）と同じ関数で選ぶ
        urls = pages_to_read(state, PAGES_PER_READ)
        started = time.perf_counter()
        for u in urls:   # 読む順番が来たページ（2 つずつ並行に読むので、残りは「予定」）
            ev("page.planned", **telemetry.page_info(u, state),
               db=state.get("db_records", {}).get(canonical(u), {}).get("database", ""))
        with ContextPool(max_workers=2) as pool:   # LLM 側の同時実行数の上限と合わせる
            results = list(pool.map(lambda u: self._read_one(state, u, ev), urls))

        pages = dict(state.get("pages", {}))
        verified = list(state.get("verified", []))
        page_imgs = list(state.get("page_images", []))
        depth = dict(state.get("link_depth", {}))
        signatures = dict(state.get("page_signatures", {}))
        copies = dict(state.get("reprints", {}))
        page_tiers = dict(state.get("page_tiers", {}))
        features = list(state.get("features", []))
        variants = dict(state.get("name_variants", {}))
        label = (state.get("target") or {}).get("label", "")
        new_links: list[str] = []
        others: list[str] = []
        reads: list[PageRead] = []
        traces = []
        for page, facts, dropped, other, meta in results:
            if other:
                others.append(other)
            if facts and depth.get(page.url, 0) < MAX_REF_DEPTH:
                # R-2: 事実が取れたページの出典欄などから、より信頼度の高いページをたどる
                for link in follow_links(page, self._target_name(state), set(state.get("reading_list", []))):
                    depth[link] = depth.get(page.url, 0) + 1
                    new_links.append(link)
            final = getattr(page, "source_url", page.url)
            if page.ok and page.title:
                pages.setdefault(page.url, page.title)
                pages.setdefault(final, page.title)
            key = canonical(final)
            if meta.get("signature"):
                # 転載の見分け（6.3）: 先に読んだページとほぼ同じ文章なら、そのページの写しとして記録する
                if original := reprints.find_original(meta["signature"], signatures):
                    copies[key] = reprints.original_of(original, copies)   # 写しの写しは、原典まで戻して記録する
                signatures[key] = meta["signature"]
            if meta.get("tier") is not None and meta["tier"] != meta.get("base_tier"):
                page_tiers[key] = meta["tier"]
            have = {f["text"] for f in features}
            features += [f for f in meta.get("features", []) if f["text"] not in have][:MAX_FEATURES - len(features)]
            for term in meta.get("new_terms", []):
                if label and not same_name(term, label):
                    variants.setdefault(term, 1)   # 本文にあった別の呼び方（7.2 の 3）。題名で見つかったものより弱く扱う
            verified += facts
            # 画像の掲載元は、本文と同じく転送をたどった後のページ（Tier・文脈の評価と報告の出典に使う）。
            # 前は要求した URL を入れていたので、Tier 1 の URL から Tier 4 のページへ転送されたとき、画像だけ Tier 1 として
            # 評価され、「確認済み」に上がることがあった（再レビュー 6）。要求した URL は取得の履歴として残す
            page_imgs += [{**im, "page_url": final, "requested_url": page.url, "title": page.title} for im in page.images]
            ok, reason = page.ok and not meta.get("error"), meta.get("error") or page.reason
            reads.append({"url": page.url, "final_url": final, "ok": ok, "reason": reason,
                          "extracted": len(facts) + len(dropped), "verified": len(facts)})
            traces.append({"url": page.url, "ok": ok, "reason": reason,
                           "verified": facts, "dropped": dropped, "reprint_of": copies.get(key, ""),
                           **{k: meta[k] for k in ("base_tier", "tier", "tier_reason", "new_terms") if k in meta}})
        log.info("read_pages: %d pages -> verified %d facts (%.1fs)", len(urls),
                 sum(r["verified"] for r in reads), time.perf_counter() - started)
        tiers_state = {**state, "page_tiers": page_tiers, "reprints": copies}
        coverage = cov.build_coverage(state["aspects"], verified, tiers_state)
        for url, original in copies.items():
            if url not in state.get("reprints", {}):
                ev("page.reprint", url=url, original=original)
        if new_links:
            ev("links.followed", urls=new_links)
        ev("coverage", rows=[{k: r[k] for k in ("aspect", "facts", "status", "best_tier", "independent")}
                             for r in coverage])
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
            "page_signatures": signatures,
            "reprints": copies,
            "page_tiers": page_tiers,
            "features": features,
            "name_variants": variants,
            "coverage": coverage,
            "pages": pages,
            "trace": [{"node": "read_pages", "pages": traces, "coverage": coverage, "followed": new_links,
                       "other_entities": others}],
        }

    def _lookup_databases(self, state: AgentState, ev: telemetry.Emitter | None = None) -> dict:
        """L3: 対象の種類に合う分野別データベースを引き、見つかった記録を先に読むページにする（設計書 8.1、6.6）。"""
        name = state["target"]["label"]
        started = time.perf_counter()
        try:
            result = databases.lookup(state["subject_type"], name, state.get("request", ""))
            records, failed, used = result[:3]
            statuses = getattr(result, "statuses", [])
        except Exception as e:  # noqa: BLE001
            records, failed, used, statuses = [], [f"分野別データベース（{type(e).__name__}）"], [], []
        db_records = dict(state.get("db_records", {}))
        db_images = list(state.get("db_images", []))
        for r in records:
            db_records[canonical(r.url)] = {"url": r.url, "title": r.title, "text": r.text, "database": r.database,
                                            "source_id": getattr(r, "source_id", ""), "kind": getattr(r, "kind", ""),
                                            "holder_url": getattr(r, "holder_url", ""),
                                            "identity": getattr(r, "identity", ""),
                                            "fields": [list(f) for f in getattr(r, "fields", [])]}
            db_images += [{**im, "database": r.database} for im in r.images]
        log.info("databases: %s -> %d records %s (%.1fs)", state["subject_type"], len(records), failed,
                 time.perf_counter() - started)
        sites = [s for s in databases.sites_for(state["subject_type"]) if s not in state.get("seen_sites", [])]
        (ev or telemetry.Emitter())("databases", used=used, failures=failed, statuses=statuses,
                                    seconds=round(time.perf_counter() - started, 1),
                                    records=[{"url": r.url, "title": r.title, "database": r.database,
                                              "images": len(r.images), "source_id": getattr(r, "source_id", ""),
                                              "kind": getattr(r, "kind", "")} for r in records])
        return {
            "databases_checked": True,
            "db_records": db_records,
            "db_images": db_images,
            "databases_used": used,
            # 記録は信頼度の高い一次情報なので、他より先に読む
            "priority_urls": by_tier(state.get("priority_urls", []) + [r.url for r in records]),
            "pages": {**state.get("pages", {}), **{r.url: r.title for r in records}},
            # API で引けない専門のサイト（researchmap など）は site: 検索の候補にする
            "seen_sites": state.get("seen_sites", []) + sites,
            "failures": list(dict.fromkeys(state.get("failures", [])
                                           + [f"分野別データベースに問い合わせられなかった: {f}" for f in failed])),
        }

    def _read_one(self, state: AgentState, url: str, ev: telemetry.Emitter | None = None):
        ev = ev or telemetry.Emitter()
        record = state.get("db_records", {}).get(canonical(url))
        ev("page.start", **telemetry.page_info(url, state), db=record["database"] if record else "")
        if record:
            # 分野別データベースの記録は、API の値から作った文章を本文として読む（ページを取りに行かない）
            page = fetch.Page(url, title=record["title"], text=record["text"], ok=True)
        else:
            page = fetch.fetch_page(url)
        ev("page.fetched", url=url, final_url=getattr(page, "final_url", "") or url, ok=page.ok,
           reason=getattr(page, "reason", ""), title=page.title,
           chars=len(page.text or ""), images=len(getattr(page, "images", []) or []))
        if not page.ok:
            return page, [], [], "", {}
        text = page.text[:MAX_PAGE_CHARS]
        names = [a["name"] for a in state.get("aspects", [])]
        source = getattr(page, "source_url", url) if not record else url   # 本文の発行元（転送されていれば転送先）
        base = page_tier(source, state)
        ev("page.llm", url=url)
        try:
            out = self._llm("extract", state).chat_json(prompts.EXTRACT_SYSTEM, prompts.extract_user(state, page.title, text, base, len(page.text)),
                                     prompts.extract_schema(names))
        except LLMOutputError as err:
            # このページだけの失敗にする（並行して読んでいる他のページの結果を失わない）
            reason = "LLM の出力を JSON として読めなかった"
            log.warning("read_pages: %s: %s (%s)", url, reason, err)
            ev("page.done", url=url, title=page.title, base_tier=base, tier=base, tier_reason="", other_entity="",
               facts=[], dropped=[], features=[], new_terms=[], error=reason)
            return page, [], [], "", {"error": reason}
        facts, dropped = [], []
        for f in out.get("facts", []):
            # 引用が本文に実在するものだけを認める（設計書 M-4）
            if f.get("about", prompts.ABOUT_TARGET) != prompts.ABOUT_TARGET:
                dropped.append({**f, "dropped": "対象そのものについての記述ではない"})
            elif quote_in_text(f.get("quote", ""), text):
                aspect = f.get("aspect") if f.get("aspect") in names else cov.OTHER_ASPECT
                facts.append({"text": f["text"], "quote": f["quote"], "aspect": aspect, "source": source})
            else:
                dropped.append(f)
        meta = page_meta(out, text, source, base, from_database=bool(record))
        # 名前の観点は、対象についての事実が取れたページ（データベースの記録）の題名・本文から、プログラムが作る
        # （LLM はどのページにもある名前を事実として出さないので、氏名・別名がいつまでも埋まらなかった）。
        # 名前の候補には、このページで LLM が見つけた別の呼び方も入れる（英語名・本名などの別名が書ける）
        aspect = name_facts.name_aspect(names)
        if aspect and (facts or record) and not any(f["aspect"] == aspect for f in facts):
            body = name_facts.source_text(page.title, text)
            nf = name_facts.name_fact(body, name_facts.candidates(state, meta.get("new_terms", [])), aspect, source)
            if nf and quote_in_text(nf["quote"], body):
                facts.append(nf)
        ev("page.done", url=url, final_url=source, title=page.title, base_tier=base, tier=meta.get("tier", base),
           tier_reason=meta.get("tier_reason", ""), other_entity=out.get("other_entity", "").strip(),
           facts=[{k: f[k] for k in ("text", "aspect", "quote")} for f in facts],
           dropped=[{"text": f.get("text", ""), "quote": f.get("quote", ""),
                     "why": f.get("dropped", "引用が本文にない")} for f in dropped],
           features=[f["text"] for f in meta.get("features", [])], new_terms=meta.get("new_terms", []))
        return page, facts, dropped, out.get("other_entity", "").strip(), meta

    @staticmethod
    def _target_name(state: AgentState) -> str:
        return (state.get("target") or {}).get("label") or state.get("request", "")

    def check_conflicts(self, state: AgentState) -> dict:
        """観点ごとに、別々のサイトの事実どうしが食い違っていないかを調べる（設計書 7.4 T-3）。"""
        groups = cov.facts_to_compare(state.get("verified", []))
        started = time.perf_counter()
        with ContextPool(max_workers=2) as pool:   # 1 回の呼び出しで 1 つの観点だけを扱う（M-1）
            llm = self._llm("decide", state)
            outs = list(pool.map(lambda item: llm.chat_json(
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
        telemetry.emitter()("conflicts", aspects=list(groups), conflicts=[
            {"aspect": c["aspect"], "topic": c["topic"],
             "facts": [{"text": f["text"], "source": f["source"]} for f in c["facts"]]} for c in conflicts])
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
        # Commons はカテゴリからも探す。ファイル名に型番がない画像（例: Olympus pen ee3.jpg）はファイル検索に出ない。
        # カテゴリ名が対象の名前と一致するものだけを使う（型番のある名前は型番で見るので、兄弟機種のカテゴリは入らない）
        gather("L2-Commonsカテゴリ", lambda: [c for c in images.commons_category(name)
                                             if mentions(c.extra.get("category", ""), [name])])
        gather(L3_STAGE, lambda: [
            images.ImageCandidate(src=im["src"], page_url=im["page_url"], stage=L3_STAGE, title=im.get("title", ""),
                                  caption=im.get("caption", ""), license=im.get("license", ""),
                                  extra={"database": im.get("database", "")})
            for im in state.get("db_images", [])])
        gather("L2-Commons", lambda: images.commons_search(name))
        gather("L1-画像検索", lambda: images.searxng_images(self.settings.searxng_url, name))
        gather("L4-ページ内", lambda: [
            images.ImageCandidate(src=im["src"], page_url=im["page_url"], stage="L4-ページ内",
                                  title=im.get("title", ""), caption=f"{im.get('alt', '')} {im.get('caption', '')}".strip())
            for im in state.get("page_images", [])])

        ranked = imaging.rank(candidates, [name])
        ev = telemetry.emitter()
        ev("image.candidates", counts=counts, ranked=len(ranked), fetching=min(len(ranked), MAX_IMAGE_FETCHES))
        log_rows: list[dict] = []
        accepted: list[dict] = []
        # 先に画像を取りに行き（プログラム。速い）、使える大きさのものだけを LLM で確かめる（遅い）。
        # 取れない・小さすぎる候補で、確かめる回数（MAX_IMAGE_CHECKS）を使い切らないように
        usable: list[tuple[images.ImageCandidate, images.FetchedImage]] = []
        with ContextPool(max_workers=4) as pool:
            for c, fetched in zip(ranked[:MAX_IMAGE_FETCHES], pool.map(lambda c: images.fetch_image(c.src), ranked[:MAX_IMAGE_FETCHES])):
                ev("image.fetched", **image_event(c, state), ok=fetched.ok, reason=fetched.reason,
                   size=f"{fetched.width}×{fetched.height}" if fetched.width else "")
                if fetched.ok:
                    usable.append((c, fetched))
                else:
                    log_rows.append({**self._image_row(c, state), "level": imaging.REJECTED, "reason": fetched.reason})
        with ContextPool(max_workers=2) as pool:
            for start in range(0, min(len(usable), MAX_IMAGE_CHECKS), 2):
                batch = usable[start:min(start + 2, MAX_IMAGE_CHECKS)]
                for row in pool.map(lambda cf: self._check_image(state, *cf, ev), batch):
                    log_rows.append(row)
                    if row["level"] != imaging.REJECTED:
                        accepted.append(row)
                if sum(r["level"] == imaging.CONFIRMED for r in accepted) >= WANTED_IMAGES:
                    break

        if not accepted and target.get("id"):
            # L5: 対象そのものの画像がない → 関係のある項目（シリーズ・上位の分類・所在地など）の画像を、
            # 「対象そのものではない」と明記して出す（設計書 8.1 L5、8.3 参考画像）
            gather("L5-周辺", lambda: images.related_images(target["id"]))
            for c in [c for c in candidates if c.stage == "L5-周辺"]:
                fetched = images.fetch_image(c.src)
                ev("image.fetched", **image_event(c, state), ok=fetched.ok, reason=fetched.reason,
                   size=f"{fetched.width}×{fetched.height}" if fetched.width else "",
                   related=c.extra.get("related", ""), relation=c.extra.get("relation", ""))
                row = {**self._image_row(c, state), "related": c.extra.get("related", ""),
                       "relation": c.extra.get("relation", "")}
                if fetched.ok:
                    ev("image.done", src=c.src, level=imaging.REFERENCE, reason=f"関係のある「{row['related']}」の画像",
                       description=row["related"], license=row["license"])
                if fetched.ok:
                    accepted.append({**row, "level": imaging.REFERENCE, "description": c.extra.get("related", ""),
                                     "size": f"{fetched.width}×{fetched.height}"})
                    log_rows.append(accepted[-1])
                else:
                    log_rows.append({**row, "level": imaging.REJECTED, "reason": fetched.reason})
                if len(accepted) >= MAX_REFERENCE_IMAGES:
                    break

        order = {imaging.CONFIRMED: 0, imaging.PROBABLE: 1, imaging.REFERENCE: 2}
        accepted.sort(key=lambda r: order[r["level"]])
        log.info("find_images: %d candidates %s -> checked %d, accepted %d (%.1fs)", len(ranked), counts,
                 len(log_rows), len(accepted), time.perf_counter() - started)
        shown = imaging.choose_shown(accepted)
        ev("image.final", shown=[r["src"] for r in shown],
           withheld=[{"src": r["src"], "why": r["withheld"]} for r in accepted if r.get("withheld")])
        return {
            "images_checked": True,
            "images": shown,
            "image_log": log_rows,
            "image_counts": counts,
            "failures": list(dict.fromkeys(state.get("failures", []) + failures)),
            "trace": [{"node": "find_images", "counts": counts, "checked": log_rows}],
        }

    @staticmethod
    def _image_row(c: images.ImageCandidate, state: AgentState | None = None) -> dict:
        # 掲載ページの Tier はページ単位の調整を含める（V-2）。同じ画像が載っている別のサイトも残す（V-4）
        return {"src": c.src, "page_url": c.page_url, "stage": c.stage, "title": c.title,
                "license": c.license, "tier": page_tier(c.page_url, state), "also_on": c.extra.get("also_on", [])}

    def _check_image(self, state: AgentState, c: images.ImageCandidate, fetched: images.FetchedImage,
                     ev: telemetry.Emitter | None = None) -> dict:
        ev = ev or telemetry.Emitter()
        row = self._image_row(c, state)
        ev("image.llm", src=c.src)
        if not row["license"] and images.is_wikimedia_file(c.src):
            try:
                row["license"] = images.commons_license(c.src)   # Wikimedia の画像なら利用条件を取りに行く
            except Exception:  # noqa: BLE001 - 取れなければ「不明」と書く
                pass
        out = self._llm("vision", state).chat_json(prompts.IMAGE_SYSTEM, prompts.image_user(state, c.title, c.caption, c.page_url),
                                 prompts.IMAGE_SCHEMA, image_data_url=fetched.data_url)
        # 画像の中の文字（刻印・型番）と対象の名前の照合はプログラムが行う（型番のある名前は型番で見るので兄弟機種は通らない）
        label = (state.get("target") or {}).get("label", "")
        visible = out.get("visible_text", "").strip()
        # 型番（数字を含む語）のある名前だけに使う。駅名の看板などで別の物を対象と取り違えないように
        text_match = bool(re.search(r"\d", label) and len(visible) >= 2 and mentions(visible, [label]))
        level = imaging.judge(row["tier"], True, out.get("context", "low"), out.get("visual", "unknown"), text_match)
        ev("image.done", src=c.src, level=level, reason=out.get("reason", ""), description=out.get("description", ""),
           context=out.get("context"), visual=out.get("visual"), visible_text=visible, text_match=text_match,
           features_seen=out.get("features_seen", []), license=row["license"])
        return {**row, "level": level, "context": out.get("context"), "visual": out.get("visual"),
                "visible_text": visible, "text_match": text_match, "features_seen": out.get("features_seen", []),
                "description": out.get("description", ""), "reason": out.get("reason", ""),
                "size": f"{fetched.width}×{fetched.height}"}

    def ask_user(self, state: AgentState) -> dict:
        question = state["decision"]["question"]
        ev = telemetry.emitter()
        # 再開したときもう一度送られる（画面は同じ質問を 1 つにまとめる）。choices: 番号で答える選択肢があるか
        ev("question", question=question, choices=has_choices(question))
        answer = str(interrupt({"question": question})).strip()
        ev("answer", question=question, answer=answer)
        result: dict = {"questions_asked": state.get("questions_asked", 0) + 1}

        result["last_question"] = question
        if state["decision"].get("confirm_target"):
            return {**result, **confirm_target(state, answer),
                    "trace": [{"node": "ask_user", "question": question, "answer": answer, "confirm": True}]}
        candidates = state.get("candidates", [])

        # 回答を選択肢と照らし合わせて、その場で対象を決める（決めないまま進むと同じ質問を繰り返すため）
        # 番号での回答はプログラムが対応づける（LLM には番号付きの選択肢が見えないため）
        picked = pick_candidate(answer, candidates)
        matched = [picked] if picked else self._match_answer(question, answer, candidates, state)
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
            target = state.get("target")
            if target and not names_target(answer, target):
                # 決めた対象（九州大学）と違うことを答えた（「松田泰治」）→ 対象と、それで集めたものを捨てて決め直す。
                # 答えが対象の名前を含むだけ（対象「伊藤」に「伊藤雄一」）では、同じ対象とみなさない
                result.update(forget_target())
        result["clarifications"] = state.get("clarifications", []) + [answer_text]
        result["trace"] = [{"node": "ask_user", "question": question, "answer": answer,
                            "matched": [c["id"] for c in matched], "target": result.get("target")}]
        return result

    def _match_answer(self, question: str, answer: str, candidates: list[Candidate],
                      state: AgentState | None = None) -> list[Candidate]:
        """文章での回答が、どの選択肢を指しているかを LLM に判定させる（1 回の呼び出しで 1 件だけ扱う）。"""
        if not candidates:
            return []
        out = self._llm("decide", state).chat_json(prompts.MATCH_SYSTEM, prompts.match_user(question, answer, candidates),
                                 prompts.MATCH_SCHEMA)
        valid = {c["id"]: c for c in candidates}
        return [valid[i] for i in dict.fromkeys(out.get("matched_ids", [])) if i in valid]

    def respond(self, state: AgentState) -> dict:
        """報告の本文は LLM が書き、ほかの節（食い違い・確かめきれなかったこと・画像・出典）は report が組み立てる。"""
        table = SourceTable(collect_sources(state))
        if time_over(state) and (unread := len(pages_to_read(state, 10_000))):
            # 時間の上限で調査をやめた: 読めなかったページの数を、報告の「調べきれなかったこと」に書く
            state = {**state, "stop_note": f"時間の上限（{setting(state, 'max_seconds')} 秒。報告をまとめる時間を含む）に"
                                            f"達したため、見つけたページのうち {unread} 件を読めなかった"}
        # 本人のものか確かめていない資料（著者名が同じだけ）の事実は、本文の材料にしない。別の節にプログラムが並べる
        # （2026-11-25「松田教授」: 同じ名前の別の人の画像処理の論文を、本人の業績として書いていた）
        unsure = unconfirmed_sources(state)
        materials = prompts.respond_user({**state, "verified": [k for k in state.get("verified", [])
                                                                if canonical(k["source"]) not in unsure]}, table.sources)
        body = strip_program_sections(self._llm("report", state).chat_text(prompts.RESPOND_SYSTEM, materials))
        if bad := unsupported_names(body, materials):
            # 材料にない名前（読みから作った英語名など）を書いた → 1 回だけ書き直させ、それでも残る文は除く
            log.info("respond: unsupported names %s -> retry", bad)
            retry = materials + ("\n\n注意: 前の回答に、材料にない名前・表記（" + "、".join(bad)
                                 + "）がありました。材料にある表記だけを使って書き直してください。")
            body = strip_program_sections(self._llm("report", state).chat_text(prompts.RESPOND_SYSTEM, retry))
            if bad := unsupported_names(body, materials):
                body = drop_sentences_with(body, bad)
        answer = assemble(state, body, table)
        # 出典は本文の番号の順（[1] が 1 つ目）。id は成果物の source_id と同じ（s1 = [1]）。画面はこれで番号から資料を開く
        telemetry.emitter()("report", answer=answer, sources=[{**e, "host": telemetry.host(e["url"])}
                                                               for e in table.entries()],
                            db_cited=cited_db_records(state, table))
        return {"answer": answer, "trace": [{"node": "respond", "answer": answer}]}


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


def image_event(c: images.ImageCandidate, state: AgentState) -> dict:
    """画像の候補を表すイベントの共通の中身。"""
    return {"src": c.src, "page_url": c.page_url, "host": telemetry.host(c.page_url), "stage": c.stage,
            "title": c.title, "tier": page_tier(c.page_url, state), "license": c.license}


def page_meta(out: dict, text: str, url: str, base: int, from_database: bool = False) -> dict:
    """1 ページの深掘りで、事実のほかに取れたもの（プログラムが確かめてから使う）。

    - signature: 転載の見分けに使う文章の署名（分野別データベースの記録は比べない）
    - tier: ページ単位の Tier。LLM の「上げる / 下げる」を、理由があり、設計書の幅に収まるときだけ認める（6.3）
    - features: 見分ける特徴（7.2 の 3）。引用が本文にあるものだけ
    - new_terms: 別の呼び方（7.2 の 3）。本文にそのままの表記であるものだけ
    """
    meta: dict = {"base_tier": base, "tier": base}
    if not from_database:
        meta["signature"] = reprints.signature(text)
        pt = out.get("page_tier") or {}
        adjusted = adjusted_tier(base, pt.get("adjust", ""))
        if adjusted != base and pt.get("reason", "").strip():
            meta.update(tier=adjusted, tier_reason=f"{pt['adjust']}: {pt['reason'].strip()}")
    meta["features"] = [{"text": f["text"].strip()[:30], "source": url} for f in out.get("features", [])
                        if f.get("text", "").strip() and quote_in_text(f.get("quote", ""), text)]
    body = normalize(text)
    meta["new_terms"] = [t.strip() for t in out.get("new_terms", [])
                         if 2 <= len(t.strip()) <= 40 and normalize(t.strip()) in body]
    return meta


def follow_links(page: fetch.Page, name: str, known: set[str]) -> list[str]:
    """R-2: ページからたどる参照を選ぶ（プログラム）。出典欄の中のリンクか、文字や URL に対象の名前がある
    リンクのうち、別のサイトで Tier 1〜2 のものを、Tier の高い順に最大 REFS_PER_PAGE 件。"""
    host = urlparse(getattr(page, "source_url", page.url)).hostname   # 転送先のサイトを基準にする
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
    return {"ambiguity_asked": True, "clarifications": state.get("clarifications", []) + [answer], **forget_target()}


def names_target(answer: str, target: Candidate) -> bool:
    """答えが、決めた対象そのものを指しているか。答えが対象の名前・説明の中に収まるときだけ（「伊藤」「小惑星」）。
    答えのほうが長い（対象「伊藤」に「伊藤雄一」）なら、別のもの（もっと絞った名前）を答えている。"""
    a = _ANSWER_TAIL.sub("", normalize(answer).strip("。、 ")).strip("。、 ")
    return bool(a) and mentions(f"{target.get('label', '')} {target.get('description', '')}", [a])


# 答えの終わりの決まり文句（「東京タワーのことです」→「東京タワー」）
_ANSWER_TAIL = re.compile(r"(のこと)?(です|でお願いします|をお願いします|で合っています|で合ってます|で大丈夫です)?[。！!]*$|の(方|ほう)(です)?$")


def is_clue_entity(e: wikimedia.Entity, person: str, clues: list[str]) -> bool:
    """人を指す依頼で、手がかり（所属など）の言葉に一致しただけの項目か（「九大」で一致した九州大学）。"""
    hit = e.matched or e.label
    if mentions(hit, [person]) or mentions(e.label, [person]):
        return False
    return any(mentions(hit, [c]) or mentions(c, [hit]) for c in clues)


def forget_target() -> dict:
    """対象を決め直すときに捨てるもの（集めた事実・ページ・画像は、前の対象のもの）。"""
    return {"target": None, "assumptions": [], "target_summary": "", "summary_url": "",
            "verified": [], "reading_list": [], "priority_urls": [], "pages_read": [], "coverage": [],
            "aspects": [], "conflicts": [], "conflicts_checked_at": -1, "tier1_searched": False, "stale_reads": 0,
            "new_ambiguity": "", "name_variants": {}, "seen_sites": [], "query_log": [],
            "official_checked": False, "official_hosts": [], "official_urls": [], "databases_checked": False,
            "databases_used": [], "db_records": {}, "db_images": [], "page_signatures": {}, "reprints": {},
            "page_tiers": {}, "features": [], "page_images": [],
            # 旧対象の画像・判定・件数も消す（新しい対象の画像を探し直し、旧対象の画像を報告に出さない）
            "images": [], "images_checked": False, "image_log": [], "image_counts": {}}


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


def route_feasibility(state: AgentState) -> str:
    return "report_infeasible" if state["feasibility"]["verdict"] == "no" else "decide"


def route(state: AgentState) -> str:
    return state["decision"]["action"]
