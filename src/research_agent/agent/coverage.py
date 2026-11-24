"""観点チェックリストと網羅表（設計書 6.1, 6.4）。

調査の進み具合は LLM の記憶ではなく表で管理する。表の集計はすべてプログラムが行う。
"""

from __future__ import annotations

import re
import tomllib
from functools import lru_cache
from typing import TypedDict
from urllib.parse import urlparse

from research_agent.config import REPO_ROOT
from research_agent.tiers import canonical, page_tier, tier_of

ASPECTS_FILE = REPO_ROOT / "config" / "aspects.toml"
OTHER_TYPE = "その他"      # どの種類にも当たらない対象
OTHER_ASPECT = "その他"    # どの観点にも当たらない事実

CONFIRMED = "確認済み"
WEAK = "出典不足"
MISSING = "未発見"


class Aspect(TypedDict):
    name: str
    words: str


class CoverageRow(TypedDict):
    aspect: str
    facts: int
    sources: list[str]     # 出典の URL（重複なし）
    best_tier: int         # 出典の中で最も高い Tier（数字が小さいほど高い。出典なしは 0）
    independent: int       # 独立した出典のサイトの数（転載は元のページのサイトとして数える）
    status: str            # 確認済み / 出典不足 / 未発見


@lru_cache(maxsize=1)
def _types() -> dict[str, list[Aspect]]:
    with open(ASPECTS_FILE, "rb") as f:
        conf = tomllib.load(f)
    return {t["name"]: t["aspects"] for t in conf["types"]}


def type_names() -> list[str]:
    return list(_types())


@lru_cache(maxsize=1)
def type_descriptions() -> dict[str, str]:
    """種類の名前 → 説明（LLM が種類を選ぶときに見せる）。"""
    with open(ASPECTS_FILE, "rb") as f:
        return {t["name"]: t.get("description", "") for t in tomllib.load(f)["types"]}


def type_guides() -> dict[str, dict]:
    """種類の名前 → 説明とお手本の観点（LLM が観点を作るときに見せる）。"""
    return {name: {"説明": desc, "お手本の観点": [a["name"] for a in _types()[name]]}
            for name, desc in type_descriptions().items()}


def aspects_for(subject_type: str) -> list[Aspect]:
    """対象の種類の観点。知らない種類なら「その他」の観点を使う。"""
    types = _types()
    return list(types.get(subject_type) or types[OTHER_TYPE])


MIN_ASPECTS = 3
MAX_ASPECTS = 8
MAX_NAME_LEN = 20
MAX_WORDS_LEN = 30


MAX_WORDS = 3


def _clean_words(words: str) -> str:
    """検索に付け足す言葉を整える。数字を含む語（年・数値の推測）は捨て、3 語までにする。"""
    tokens = [w for w in re.split(r"[,、，\s]+", words) if w and not re.search(r"\d", w)]
    return " ".join(tokens[:MAX_WORDS])[:MAX_WORDS_LEN]


def make_aspects(raw: list[dict] | None, subject_type: str) -> tuple[list[Aspect], str]:
    """LLM が作った観点を確かめて網羅表の行にする。使えなければ種類のチェックリストに戻す。

    戻り値は (観点, 出どころ)。出どころは "LLM" か "チェックリスト"。
    名前が空・長すぎる・重複・「その他」のものは捨てる。残りが MIN_ASPECTS 未満ならチェックリストを使う。
    """
    aspects: list[Aspect] = []
    seen: set[str] = set()
    for a in raw or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name", "")).strip()
        words = _clean_words(str(a.get("words", ""))) or name
        if not name or len(name) > MAX_NAME_LEN or name == OTHER_ASPECT or name in seen:
            continue
        seen.add(name)
        aspects.append({"name": name, "words": words})
    if len(aspects) < MIN_ASPECTS:
        return aspects_for(subject_type), "チェックリスト"
    return aspects[:MAX_ASPECTS], "LLM"


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def status_of(sources: list[str], tier=tier_of, original=None) -> str:
    """出典の状態（設計書 6.3「観点が埋まった」の基準）。

    確認済み: Tier 1 の出典がある、または独立した（別のサイトの）出典が 2 つ以上あり、うち 1 つ以上が Tier 1〜2
    出典不足: 出典はあるが、上の条件を満たさない（Tier 3〜4 だけ、または出典が 1 つだけ）
    未発見:   出典なし
    tier:     URL → Tier（ページ単位の調整を含む）。original: 転載なら元のページの URL（独立した出典として数えない）
    """
    if not sources:
        return MISSING
    tiers = [tier(u) for u in sources]
    if min(tiers) == 1 or (independent_sites(sources, original) >= 2 and min(tiers) <= 2):
        return CONFIRMED
    return WEAK


def independent_sites(sources: list[str], original=None) -> int:
    """独立した出典のサイトの数。転載のページは元のページのサイトとして数える。"""
    original = original or (lambda u: u)
    return len({_host(original(u)) for u in sources})


def build_coverage(aspects: list[Aspect], verified: list[dict], state: dict | None = None) -> list[CoverageRow]:
    """網羅表を作る。state を渡すと、ページ単位の Tier（公式サイト・LLM の調整）と転載を考えに入れる。"""
    reprints = (state or {}).get("reprints", {})

    def original(u: str) -> str:
        return reprints.get(canonical(u), u)

    rows: list[CoverageRow] = []
    for a in aspects:
        facts = [f for f in verified if f.get("aspect") == a["name"]]
        sources = list(dict.fromkeys(f["source"] for f in facts))

        def tier(u: str, name: str = a["name"]) -> int:
            return page_tier(u, state, name)

        rows.append({
            "aspect": a["name"],
            "facts": len(facts),
            "sources": sources,
            "best_tier": min((tier(u) for u in sources), default=0),
            "independent": independent_sites(sources, original),
            "status": status_of(sources, tier, original),
        })
    return rows


def facts_to_compare(verified: list[dict]) -> dict[str, list[dict]]:
    """食い違いを調べる観点と、その事実。別々のサイトの事実が 2 つ以上ある観点だけを選ぶ（横の絞り込み）。"""
    by_aspect: dict[str, list[dict]] = {}
    for f in verified:
        by_aspect.setdefault(f.get("aspect") or OTHER_ASPECT, []).append(f)
    return {a: fs for a, fs in by_aspect.items() if len({_host(f["source"]) for f in fs}) >= 2}


def different_sites(facts: list[dict]) -> bool:
    return len({_host(f["source"]) for f in facts}) >= 2


def unfilled(coverage: list[CoverageRow]) -> list[CoverageRow]:
    return [r for r in coverage if r["status"] != CONFIRMED]


def gaps_text(coverage: list[CoverageRow]) -> str:
    """報告の「確認できなかったこと」をプログラムで作る（設計書 9.1, 9.2）。出典が 1 つもない観点だけ。"""
    return "\n".join(f"- {r['aspect']}: 確認できる出典が見つからなかった"
                     for r in coverage if r["status"] == MISSING)


def weak_text(coverage: list[CoverageRow]) -> str:
    """報告の「出典が限られること」。本文には書いたが、出典が弱い観点（設計書 6.3 の基準に届かない）。

    「確認できなかった」と書くと、本文に書いた内容と食い違って見えるので分けて書く。
    """
    lines = []
    for r in coverage:
        if r["status"] != WEAK:
            continue
        sites = r.get("independent") or len({_host(u) for u in r["sources"]})
        if sites == 1:
            lines.append(f"- {r['aspect']}: 1 サイト（Tier {r['best_tier']}）の情報だけで、ほかの出典では確かめていない")
        else:
            # 出典不足で 2 サイト以上 → どれも Tier 3〜4（Tier 1〜2 が入っていれば確認済みになる）
            lines.append(f"- {r['aspect']}: {sites} サイトに書かれているが、いずれも販売店・個人のサイトなど"
                         f"（最も信頼度の高いもので Tier {r['best_tier']}）で、公的機関・メーカー・百科事典では確かめていない")
    return "\n".join(lines)


def merge_conflicts(conflicts: list[dict]) -> list[dict]:
    """同じ観点・同じ事柄の食い違いを 1 つにまとめる（同じ事柄が何行も出ないように）。"""
    merged: dict[tuple[str, str], dict] = {}
    for c in conflicts:
        key = (c["aspect"], c["topic"])
        if key not in merged:
            merged[key] = {**c, "facts": list(c["facts"])}
            continue
        have = merged[key]["facts"]
        have += [f for f in c["facts"] if f not in have]
    return list(merged.values())
