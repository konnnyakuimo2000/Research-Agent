"""Wikidata / Wikipedia の公式 API（Tier 2。SearXNG 経由にせず直接呼ぶ。設計書 6.6 G-2）。"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from my_llm_app.tools import http

WIKIDATA_API = "https://www.wikidata.org/w/api.php"


@dataclass
class Entity:
    id: str
    label: str
    description: str
    sitelinks: int = 0   # 記事がある Wikipedia などの数（どれだけ一般的に知られているかの目安）
    matched: str = ""    # 検索で一致した名前（ラベルまたは別名。例: 水星は英語名「Mercury」で一致する）

    @property
    def url(self) -> str:
        return f"https://www.wikidata.org/wiki/{self.id}"


@dataclass
class WikiSummary:
    title: str
    extract: str
    url: str


def search_entities(query: str, languages: tuple[str, ...] = ("ja", "en"), limit: int = 10,
                    timeout: float = 30.0) -> list[Entity]:
    """同名の候補を探す。

    「マーキュリー」で日本語だけを検索すると惑星（日本語名は「水星」）が出てこないため、
    複数の言語で検索して ID で重複を除く。説明文はできるだけ日本語で受け取る。
    """
    seen: dict[str, Entity] = {}
    for lang in languages:
        r = http.get(
            WIKIDATA_API,
            params={"action": "wbsearchentities", "search": query, "language": lang,
                    "uselang": "ja", "limit": limit, "format": "json"},
            timeout=timeout,
        )
        r.raise_for_status()
        for item in r.json().get("search", []):
            if item["id"] not in seen:
                seen[item["id"]] = Entity(
                    id=item["id"],
                    label=item.get("label", ""),
                    description=item.get("description", ""),
                    matched=item.get("match", {}).get("text", ""),
                )
    return list(seen.values())


def get_entities(ids: list[str], timeout: float = 30.0) -> dict[str, Entity]:
    """ID から項目の名前・説明（日本語優先、なければ英語）と sitelinks の数を取る。"""
    if not ids:
        return {}
    r = http.get(
        WIKIDATA_API,
        params={"action": "wbgetentities", "ids": "|".join(ids[:50]), "props": "labels|descriptions|sitelinks",
                "languages": "ja|en", "format": "json"},
        timeout=timeout,
    )
    r.raise_for_status()

    def pick(d: dict) -> str:
        return (d.get("ja") or d.get("en") or {}).get("value", "")

    return {
        eid: Entity(id=eid, label=pick(e.get("labels", {})), description=pick(e.get("descriptions", {})),
                    sitelinks=len(e.get("sitelinks", {})))
        for eid, e in r.json().get("entities", {}).items()
        if "missing" not in e
    }


def summary_for_entity(entity_id: str, timeout: float = 30.0) -> WikiSummary | None:
    """Wikidata の項目に対応する Wikipedia 記事の冒頭（日本語優先、なければ英語）を返す。"""
    r = http.get(
        WIKIDATA_API,
        params={"action": "wbgetentities", "ids": entity_id, "props": "sitelinks",
                "sitefilter": "jawiki|enwiki", "format": "json"},
        timeout=timeout,
    )
    r.raise_for_status()
    sitelinks = r.json().get("entities", {}).get(entity_id, {}).get("sitelinks", {})
    for site, lang in (("jawiki", "ja"), ("enwiki", "en")):
        if site not in sitelinks:
            continue
        title = sitelinks[site]["title"]
        s = http.get(
            f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title, safe='')}",
            timeout=timeout,
        )
        if s.status_code != 200:
            continue
        data = s.json()
        return WikiSummary(
            title=data.get("title", title),
            extract=data.get("extract", ""),
            url=data.get("content_urls", {}).get("desktop", {}).get("page",
                f"https://{lang}.wikipedia.org/wiki/{quote(title)}"),
        )
    return None


def claims(entity_id: str, timeout: float = 30.0) -> dict:
    """項目の主張（プロパティ → 値の一覧）。"""
    r = http.get(WIKIDATA_API, params={"action": "wbgetentities", "ids": entity_id, "props": "claims",
                                       "format": "json"}, timeout=timeout)
    r.raise_for_status()
    return r.json().get("entities", {}).get(entity_id, {}).get("claims", {})


def claim_values(c: dict, prop: str) -> list:
    """主張の値。文字列（URL・ファイル名）はそのまま、項目は Q 番号にする。"""
    out = []
    for x in c.get(prop, []):
        v = x.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and "id" in v:
            out.append(v["id"])
        elif isinstance(v, str):
            out.append(v)
    return out


# 対象を作った・出した組織（製品のメーカーなど）。その公式サイトも対象の公式な情報源とみなす（設計書 6.3）
MAKER_PROPS = ("P176", "P178", "P123")   # 製造者、開発者、発行者


def official_sites(entity_id: str, timeout: float = 30.0) -> list[str]:
    """対象自身の公式サイト（P856）と、メーカーなどの公式サイト。"""
    c = claims(entity_id, timeout)
    sites = claim_values(c, "P856")
    makers = [m for p in MAKER_PROPS for m in claim_values(c, p)][:2]
    for m in makers:
        sites += claim_values(claims(m, timeout), "P856")[:1]
    return list(dict.fromkeys(sites))
