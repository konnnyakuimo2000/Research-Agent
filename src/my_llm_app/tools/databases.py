"""分野別のデータベース（L3。設計書 8.1、6.6、実装計画フェーズ 6）。

対象の種類ごとに、専門のデータベースの公開 API を直接呼ぶ（SearXNG 経由にしない。G-2）。
どの種類でどれを使うか、API の条件・制限は config/databases.toml に書く。

返すのは Record（1 件 = 読むページ 1 つ分）。text は API の値から作った文章で、
文章の深掘り（read_pages）はページ本文の代わりにこれを読み、引用はこの文章と照合する。
画像があれば、画像の候補（L3）として一緒に返す。名前が対象と合わない項目は返さない（同名の別人・別種を混ぜない）。
"""

from __future__ import annotations

import os
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import quote

from my_llm_app.agent.names import mentions
from my_llm_app.config import REPO_ROOT
from my_llm_app.tools import http

DATABASES_FILE = REPO_ROOT / "config" / "databases.toml"
TIMEOUT = 20.0
MAX_ITEMS = 3


@dataclass
class Record:
    url: str                 # 人が見られるページ（出典にする URL）
    title: str
    text: str                # API の値から作った文章（深掘りと引用の照合に使う）
    database: str            # データベースの名前
    images: list[dict] = field(default_factory=list)   # src, page_url, title, caption, license


@lru_cache(maxsize=1)
def _config() -> dict:
    with open(DATABASES_FILE, "rb") as f:
        return tomllib.load(f)


def databases_for(subject_type: str) -> list[dict]:
    """その種類で使うデータベース（使えるものだけ）。"""
    return [d for d in _config().get("databases", [])
            if subject_type in d.get("types", []) and d.get("available", True)
            and (not d.get("requires_env") or os.getenv(d["requires_env"]))]


def sites_for(subject_type: str) -> list[str]:
    """その種類で、site: で狙う専門のサイト（API が使えないデータベースなど）。"""
    return list(_config().get("sites", {}).get(subject_type, []))


def _json(url: str, params: dict | None = None) -> dict:
    r = http.get(url, params=params, headers={**http.HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# --- 研究者 ---------------------------------------------------------------

def orcid(name: str) -> list[Record]:
    """ORCID の研究者情報（本人が登録したもの）。"""
    data = _json("https://pub.orcid.org/v3.0/expanded-search/", {"q": name, "rows": 5})
    out = []
    for r in data.get("expanded-result") or []:
        names = [r.get("credit-name") or "", f"{r.get('given-names') or ''} {r.get('family-names') or ''}".strip()]
        names += r.get("other-name") or []
        if not any(n and mentions(n, [name]) for n in names):
            continue
        oid = r["orcid-id"]
        lines = ["ORCID の研究者情報（本人が登録したもの）",
                 f"氏名: {next(n for n in names if n)}"]
        if r.get("other-name"):
            lines.append("別の表記: " + "、".join(r["other-name"]))
        if r.get("institution-name"):
            lines.append("所属機関: " + "、".join(r["institution-name"]))
        lines.append(f"ORCID iD: {oid}")
        out.append(Record(f"https://orcid.org/{oid}", f"ORCID: {lines[1].removeprefix('氏名: ')}", "\n".join(lines),
                          "ORCID"))
    return out[:MAX_ITEMS]


def cinii(name: str) -> list[Record]:
    """CiNii Research で、対象が著者の文献（業績）。"""
    data = _json("https://cir.nii.ac.jp/opensearch/all",
                 {"q": name, "format": "json", "count": 20, "appid": os.getenv("CINII_APPID", "")})
    lines = []
    for it in data.get("items") or []:
        creators = it.get("dc:creator") or []
        creators = [creators] if isinstance(creators, str) else creators
        if not any(mentions(c, [name]) for c in creators):
            continue
        kind = it.get("dc:type", "")
        date = it.get("prism:publicationDate", "")
        where = it.get("prism:publicationName", "")
        detail = "、".join(x for x in (kind, where, date) if x)
        lines.append(f"- {it.get('title', '')}（{detail}）著者: {'、'.join(creators[:5])}")
    if not lines:
        return []
    text = f"CiNii Research で「{name}」が著者に含まれる文献\n" + "\n".join(lines[:10])
    return [Record(f"https://cir.nii.ac.jp/all?q={quote(name)}", f"CiNii Research: {name} の文献", text,
                   "CiNii Research")]


# --- 生物 -----------------------------------------------------------------

def inaturalist(name: str) -> list[Record]:
    """iNaturalist の分類群（学名・和名・代表写真）。"""
    data = _json("https://api.inaturalist.org/v1/taxa", {"q": name, "locale": "ja", "per_page": 3})
    out = []
    for t in data.get("results") or []:
        common = t.get("preferred_common_name") or ""
        if not mentions(f"{common} {t.get('name', '')} {t.get('matched_term', '')}", [name]):
            continue
        url = f"https://www.inaturalist.org/taxa/{t['id']}"
        lines = ["iNaturalist の分類群の情報", f"学名: {t.get('name', '')}"]
        if common:
            lines.append(f"和名: {common}")
        lines += [f"分類の階級: {t.get('rank', '')}", f"観察記録の数: {t.get('observations_count', 0)}"]
        photo = t.get("default_photo") or {}
        images = []
        if photo.get("medium_url"):
            images.append({"src": photo["medium_url"], "page_url": url, "title": f"iNaturalist: {common or t['name']}",
                           "caption": f"iNaturalist の分類群「{common}（{t.get('name', '')}）」の代表写真。"
                                      f"{photo.get('attribution', '')}",
                           "license": (photo.get("license_code") or "all rights reserved").upper()})
        out.append(Record(url, f"iNaturalist: {common or t.get('name', '')}", "\n".join(lines), "iNaturalist", images))
    return out[:1]


def gbif(scientific_name: str) -> list[Record]:
    """GBIF の分類（界〜種）。学名で引く。"""
    m = _json("https://api.gbif.org/v1/species/match", {"name": scientific_name})
    if m.get("matchType") not in ("EXACT", "FUZZY") or not m.get("usageKey"):
        return []
    ranks = [("界", "kingdom"), ("門", "phylum"), ("綱", "class"), ("目", "order"), ("科", "family"),
             ("属", "genus"), ("種", "species")]
    lines = ["GBIF の分類情報", f"学名: {m.get('scientificName', '')}"]
    lines += [f"{ja}: {m[en]}" for ja, en in ranks if m.get(en)]
    return [Record(f"https://www.gbif.org/species/{m['usageKey']}", f"GBIF: {m.get('canonicalName', '')}",
                   "\n".join(lines), "GBIF")]


# --- 文化財・歴史・製品（博物館の所蔵品など） ---------------------------------

_RIGHTS = {"cc0": "CC0", "pdm": "パブリックドメイン", "ccby": "CC BY", "ccbysa": "CC BY-SA", "ccbync": "CC BY-NC",
           "ccbynd": "CC BY-ND", "ccbyncsa": "CC BY-NC-SA", "ccbyncnd": "CC BY-NC-ND", "incr": "著作権あり",
           "incr_edu": "著作権あり（教育目的の利用可）", "edu": "教育目的の利用可", "noncom": "非営利の利用可",
           "com": "商用の利用可"}


def japan_search(name: str) -> list[Record]:
    """ジャパンサーチ（全国の博物館・図書館などの所蔵品）。"""
    data = _json("https://jpsearch.go.jp/api/item/search/jps-cross", {"keyword": name, "size": 10})
    out = []
    for it in data.get("list") or []:
        c = it.get("common") or {}
        title = c.get("title", "")
        if not mentions(title, [name]):
            continue
        url = f"https://jpsearch.go.jp/item/{it['id']}"
        lines = ["ジャパンサーチの所蔵品の情報", f"名称: {title}"]
        for label, key in (("提供", "provider"), ("所蔵・作成", "contributor"), ("時代・年", "temporal"),
                           ("分類", "subCategory")):
            v = c.get(key)
            if v:
                lines.append(f"{label}: {'、'.join(v) if isinstance(v, list) else v}")
        if c.get("linkUrl"):
            lines.append(f"所蔵元のページ: {c['linkUrl']}")
        license_ = _RIGHTS.get(c.get("contentsRightsType", ""), c.get("contentsRightsType", ""))
        images = [{"src": src, "page_url": url, "title": f"ジャパンサーチ: {title}",
                   "caption": f"{c.get('provider', '')}の所蔵品「{title}」の画像", "license": license_}
                  for src in (c.get("thumbnailUrl") or [])[:2]]
        out.append(Record(url, f"ジャパンサーチ: {title}", "\n".join(lines), "ジャパンサーチ", images))
        if len(out) >= MAX_ITEMS:
            break
    return out


_NS = {"dc": "http://purl.org/dc/elements/1.1/"}


def ndl(name: str, by_creator: bool = False) -> list[Record]:
    """国立国会図書館サーチ。対象を題名に含む資料（人物なら、対象が著者の資料）。"""
    r = http.get("https://ndlsearch.ndl.go.jp/api/opensearch",
                 params={"creator" if by_creator else "title": name, "cnt": 10}, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    lines = []
    for item in root.iter("item"):
        title = item.findtext("dc:title", "", _NS) or item.findtext("title", "")
        creator = item.findtext("dc:creator", "", _NS)
        if not (mentions(creator, [name]) if by_creator else mentions(title, [name])):
            continue
        desc = item.findtext("dc:description", "", _NS)
        lines.append(f"- {title}（{'、'.join(x for x in (creator, desc) if x)}）")
    if not lines:
        return []
    what = "が著者の資料" if by_creator else "を題名に含む資料"
    param = "creator" if by_creator else "title"
    return [Record(f"https://ndlsearch.ndl.go.jp/search?cs=bib&{param}={quote(name)}",
                   f"国立国会図書館サーチ: {name}", f"国立国会図書館サーチで「{name}」{what}\n" + "\n".join(lines[:10]),
                   "国立国会図書館サーチ")]


def ndl_creator(name: str) -> list[Record]:
    return ndl(name, by_creator=True)


FUNCTIONS = {"orcid": orcid, "cinii": cinii, "inaturalist": inaturalist, "japan_search": japan_search,
             "ndl": ndl, "ndl_creator": ndl_creator}


def lookup(subject_type: str, name: str) -> tuple[list[Record], list[str], list[str]]:
    """その種類のデータベースを順に引く。

    戻り値は (見つかった Record, 使えなかったデータベースと理由, 問い合わせたデータベースの名前)。
    問い合わせた名前には、続けて引いたもの（iNaturalist の学名で引く GBIF）も入れる（報告で申告するため）。
    """
    records: list[Record] = []
    failures: list[str] = []
    used: list[str] = []
    for db in databases_for(subject_type):
        fn = FUNCTIONS.get(db["function"])
        if not fn:
            continue
        used.append(db["name"])
        try:
            found = fn(name)
        except Exception as e:  # noqa: BLE001 - 1 つ使えなくても他を引く。使えなかったことは申告する
            failures.append(f"{db['name']}（{type(e).__name__}）")
            continue
        records += found
        if db["function"] == "inaturalist" and found:
            # GBIF は学名で引く（和名では引けない）
            sci = next((ln.removeprefix("学名: ") for ln in found[0].text.splitlines() if ln.startswith("学名: ")), "")
            if sci:
                used.append("GBIF")
                try:
                    records += gbif(sci)
                except Exception as e:  # noqa: BLE001
                    failures.append(f"GBIF（{type(e).__name__}）")
    return records, failures, used
