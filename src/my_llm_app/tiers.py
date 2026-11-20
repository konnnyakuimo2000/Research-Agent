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


# パスの中で、文字に戻すと区切りに化ける符号（/ ? # %）。これらは符号のまま残す
_RESERVED_IN_PATH = re.compile(r"%(2f|3f|23|25)", re.IGNORECASE)


def _path_key(path: str) -> str:
    """パスの表記ゆれをそろえる。%C3%BC と ü は同じにするが、%2F（文字の /）と区切りの / は区別する。"""
    held: list[str] = []

    def hold(m: re.Match) -> str:
        held.append(m.group(0).upper())
        return f"\x00{len(held) - 1}\x00"

    text = unquote(_RESERVED_IN_PATH.sub(hold, path))
    return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], text)


def canonical(url: str) -> str:
    """同じページを指す URL の表記ゆれをそろえた形。重複の判定に使う。

    そろえるもの: %C3%BC と %c3%bc と ü、#以降、;jsessionid=…、追跡用のパラメータ（Google の srsltid など）。
    パスとクエリは別々にそろえる。URL 全体を文字に戻すと、クエリの値の中の符号（%26 = &、%3D = =）が
    区切りに化けて、別の資料が同じ出典になってしまうため。クエリは parse_qsl → urlencode で符号の形にそろえる。
    """
    parts = urlsplit(url.split("#", 1)[0])
    path = _path_key(re.sub(r";jsessionid=[^/?]*", "", parts.path, flags=re.IGNORECASE))
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


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


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


# 観点ごとの例外（設計書 6.3）: 対象自身の公式サイトは Tier 1 だが、評価・評判の観点では Tier 4 とする（自己評価のため）
EVALUATION_ASPECT = re.compile(r"評価|評判|レビュー|口コミ|感想|批評|反響")


def is_official(url: str, official_hosts: list[str]) -> bool:
    h = host_of(url)
    return any(_matches(h, o) for o in official_hosts)


def page_tier(url: str, state: dict | None = None, aspect: str = "") -> int:
    """ページ単位の Tier（設計書 6.3「Tier の調整」）。ドメインの Tier から始めて、次の順に変える。

    1. 対象自身の公式サイト（Wikidata の公式サイト）→ Tier 1。ただし評価・評判の観点では Tier 4
    2. ページを読んだ LLM の判定で 1 段上げ下げしたもの（state["page_tiers"]。プログラムが幅を確かめて入れる）
    """
    tier = tier_of(url)
    if not state:
        return tier
    if is_official(url, state.get("official_hosts", [])):
        if aspect and EVALUATION_ASPECT.search(aspect):
            return 4
        tier = 1
    return state.get("page_tiers", {}).get(canonical(url), tier)


def adjusted_tier(base: int, adjust: str) -> int:
    """LLM の「上げる / 下げる」を、設計書の幅（1 段だけ。上げるのは Tier 3〜4、下げるのは Tier 1〜2）に収める。"""
    if adjust == "上げる" and base in (3, 4):
        return base - 1
    if adjust == "下げる" and base in (1, 2):
        return base + 1
    return base


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
