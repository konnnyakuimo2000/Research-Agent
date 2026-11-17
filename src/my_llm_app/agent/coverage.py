"""観点チェックリストと網羅表（設計書 6.1, 6.4）。

調査の進み具合は LLM の記憶ではなく表で管理する。表の集計はすべてプログラムが行う。
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from typing import TypedDict
from urllib.parse import urlparse

from my_llm_app.config import REPO_ROOT
from my_llm_app.tiers import tier_of

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


def aspects_for(subject_type: str) -> list[Aspect]:
    """対象の種類の観点。知らない種類なら「その他」の観点を使う。"""
    types = _types()
    return list(types.get(subject_type) or types[OTHER_TYPE])


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def status_of(sources: list[str]) -> str:
    """出典の状態（設計書 6.3「観点が埋まった」の基準）。

    確認済み: Tier 1 の出典がある、または独立した（別のサイトの）出典が 2 つ以上あり、うち 1 つ以上が Tier 1〜2
    出典不足: 出典はあるが、上の条件を満たさない（Tier 3〜4 だけ、または出典が 1 つだけ）
    未発見:   出典なし
    """
    if not sources:
        return MISSING
    tiers = [tier_of(u) for u in sources]
    hosts = {_host(u) for u in sources}
    if min(tiers) == 1 or (len(hosts) >= 2 and min(tiers) <= 2):
        return CONFIRMED
    return WEAK


def build_coverage(aspects: list[Aspect], verified: list[dict]) -> list[CoverageRow]:
    rows: list[CoverageRow] = []
    for a in aspects:
        facts = [f for f in verified if f.get("aspect") == a["name"]]
        sources = list(dict.fromkeys(f["source"] for f in facts))
        rows.append({
            "aspect": a["name"],
            "facts": len(facts),
            "sources": sources,
            "best_tier": min((tier_of(u) for u in sources), default=0),
            "status": status_of(sources),
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
    """報告の「確認できなかったこと」をプログラムで作る（設計書 9.1, 9.2）。"""
    lines = []
    for r in unfilled(coverage):
        if r["status"] == MISSING:
            lines.append(f"- {r['aspect']}: 確認できる出典が見つからなかった")
        else:
            lines.append(f"- {r['aspect']}: 出典が {len({_host(u) for u in r['sources']})} サイト"
                         f"（最も信頼度の高いもので Tier {r['best_tier']}）のみで、十分に確認できていない")
    return "\n".join(lines)
