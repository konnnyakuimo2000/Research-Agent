"""情報源の Tier（設計書 6.3）。一覧は config/source_tiers.toml。"""

from __future__ import annotations

import re
import tomllib
from functools import lru_cache
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlsplit, urlunsplit

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


# 検索エンジンや広告が付ける追跡用のパラメータ（ページの中身は変わらない）
_TRACKING = re.compile(r"^(srsltid|utm_\w+|gclid|fbclid|yclid|msclkid|mc_cid|mc_eid)$", re.IGNORECASE)


def canonical(url: str) -> str:
    """同じページを指す URL の表記ゆれをそろえた形。重複の判定に使う。

    そろえるもの: %C3%BC と %c3%bc と ü、#以降、;jsessionid=…、追跡用のパラメータ（Google の srsltid など）。
    """
    parts = urlsplit(url.split("#", 1)[0])
    path = re.sub(r";jsessionid=[^/?]*", "", parts.path, flags=re.IGNORECASE)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return unquote(urlunsplit((parts.scheme, parts.netloc, path, query, "")))


class UrlNumbers(dict):
    """URL → 出典番号。表記が違うだけの同じページ（canonical が同じ）は同じ番号を返す。"""

    def __init__(self, urls: list[str]) -> None:
        super().__init__()
        for i, u in enumerate(urls, 1):
            super().setdefault(canonical(u), i)

    def __getitem__(self, url: str) -> int:
        return super().__getitem__(canonical(url))

    def __contains__(self, url: object) -> bool:
        return isinstance(url, str) and super().__contains__(canonical(url))


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
