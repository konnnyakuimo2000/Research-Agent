"""情報源の Tier（設計書 6.3）。一覧は config/source_tiers.toml。"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from urllib.parse import unquote, urlparse

from my_llm_app.config import REPO_ROOT

TIERS_FILE = REPO_ROOT / "config" / "source_tiers.toml"
EXCLUDED_TIER = 5   # 出典にしない（読まない）


@lru_cache(maxsize=1)
def _load() -> dict:
    with open(TIERS_FILE, "rb") as f:
        return tomllib.load(f)


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def tier_of(url: str) -> int:
    host = (urlparse(url).hostname or "").lower()
    conf = _load()
    domains = [d for d in conf.get("domains", {}) if _matches(host, d)]
    if domains:
        return conf["domains"][max(domains, key=len)]
    suffixes = [s for s in conf.get("suffixes", {}) if _matches(host, s)]
    if suffixes:
        return conf["suffixes"][max(suffixes, key=len)]
    return conf.get("default", 4)


def canonical(url: str) -> str:
    """同じページを指す URL の表記ゆれ（%C3%BC と %c3%bc、#以降）をそろえた形。重複の判定に使う。"""
    return unquote(url.split("#", 1)[0])


def by_tier(urls: list[str]) -> list[str]:
    """Tier 5 と、表記が違うだけの重複を除き、Tier の高い順に並べる（同じ Tier の中では元の順を保つ）。"""
    seen: set[str] = set()
    unique = []
    for u in urls:
        if (key := canonical(u)) not in seen:
            seen.add(key)
            unique.append(u)
    kept = [u for u in unique if tier_of(u) != EXCLUDED_TIER]
    return sorted(kept, key=tier_of)
