"""検索結果から、読むページを決める（副作用なし。全体アーキテクチャの分析 3）。

前は Nodes.search の中に、対象の同定・検索結果の選別・Tier 1 の追加検索・読む順番・イベントの生成が並んでいて、
「どの結果を対象のページとみなし、どの URL をどの順で読むか」を一か所で追えなかった。ここではネットワークにも
状態にも触らず、入力（今の状態・この検索の結果・判断）だけから決める。Tier 1 の追加検索（ネットワーク）は
Nodes.search に残し、その結果を足してから並べる。

- 関連性: 題名・抜粋に対象の名前（表記ゆれを含む）があるか（names が空なら、すべて）
- URL の比べ方: tiers.canonical（表記が違うだけの同じページは同じ）
- 読めない URL（PDF の一部・動画など fetch.is_unreadable）は入れない
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from research_agent.agent.names import mentions, mentions_parts, sibling_only
from research_agent.tiers import by_tier, canonical
from research_agent.tools import fetch


@dataclass
class PageChoice:
    relevant: list = field(default_factory=list)       # 対象のページとみなした検索結果（題名・抜粋に名前がある）
    urls: list[str] = field(default_factory=list)      # 読む一覧に足す URL（概要・公式・関連する結果の順）
    priority: list[str] | None = None                  # 先に読む URL（None なら変えない）
    new_relevant: int = 0                              # この検索で初めて見つかった、対象のページの数


def relevant_results(results: Iterable, names: list[str], loose: bool = False) -> list:
    """題名・抜粋に対象の名前がある検索結果（names が空なら、すべて）。題名の名前が、どれも番号の続く別の物
    （「はやぶさ」に対する「はやぶさ2」）だけなら外す（前は「はやぶさ」の調査で、はやぶさ2 のページも読んでいた）。
    loose: 概念の複合語は、分けた部分が両方あれば当たりとする（names.mentions_parts。人の名前では使わない）"""
    match = mentions_parts if loose else mentions
    return [w for w in results if not names or (match(f"{w.title} {w.snippet}", names)
                                                and not (names and sibling_only(w.title, names[0])))]


def keep_for_target(urls: list[str], titles: dict[str, str], names: list[str]) -> list[str]:
    """対象が決まる前に読む一覧へ入れたページのうち、題名が対象の名前を含まないものを外す（題名が分からないものは残す）。
    「青学の伊藤先生」で、答えを聞く前の検索で入れた別の伊藤先生（伊藤一成・伊藤晴祥）のページを、対象を
    伊藤雄一に決めた後も読んで、その事実を報告に入れていた（2026-11-25）。"""
    return [u for u in urls if not titles.get(u) or (mentions(titles[u], names)
                                                     and not (names and sibling_only(titles[u], names[0])))]


def choose_pages(state: dict, found: dict, results: list, decision: dict, names: list[str], loose: bool = False) -> PageChoice:
    """state は検索の前の状態、found はこの検索で分かったこと（summary_url・official_urls）。"""
    read = {canonical(p["url"]) for p in state.get("pages_read", [])}
    first = [found["summary_url"]] if found.get("summary_url") else []
    first += [u for u in found.get("official_urls", []) if not fetch.is_unreadable(u)]
    relevant = relevant_results(results, names, loose)
    urls = first + [w.url for w in relevant if not fetch.is_unreadable(w.url)]

    priority = None
    if decision.get("aspect") or decision.get("conflict_topic"):
        # 観点や食い違いを狙って探したページは、他より先に読む（R-1, R-6）
        priority = by_tier(state.get("priority_urls", [])
                           + [w.url for w in relevant if canonical(w.url) not in read and not fetch.is_unreadable(w.url)])
    summary = found.get("summary_url", "")
    if summary and not state.get("summary_url") and canonical(summary) not in read:
        # 対象の Wikipedia の記事は、全体を見渡せる最初の資料なので先に読む（読む一覧は Tier の順なので、
        # Tier 1 のページで枠が埋まり、最後まで読まないことがあった）
        base = priority if priority is not None else state.get("priority_urls", [])
        priority = [summary] + [u for u in base if canonical(u) != canonical(summary)]

    known = read | {canonical(u) for u in state.get("reading_list", [])}
    new_relevant = len({canonical(w.url) for w in relevant} - known)
    return PageChoice(relevant, urls, priority, new_relevant)
