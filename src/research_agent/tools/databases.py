"""分野別のデータベース（L3。設計書 8.1、6.6、実装計画フェーズ 6）。

対象の種類ごとに、専門のデータベースの公開 API を直接呼ぶ（SearXNG 経由にしない。G-2）。
どの種類でどれを使うか、API の条件・制限は config/databases.toml に書く。

返すのは Record（1 件 = 読むページ 1 つ分）。text は API の値から作った文章で、
文章の深掘り（read_pages）はページ本文の代わりにこれを読み、引用はこの文章と照合する。
画像があれば、画像の候補（L3）として一緒に返す。名前が対象と合わない項目は返さない（同名の別人・別種を混ぜない）。

分野別データベースの充実化（.claude/database-expansion.md）:
- 資料 1 件 = Record 1 件。個別の URL・提供元の資料 ID（source_id）・根拠に使った項目（fields）・種類（kind:
  「書誌情報」「所蔵品情報」など）を持つ。前は国立国会図書館サーチの複数の書誌を 1 件にまとめ、検索結果の一覧の
  URL を付けていたので、報告からどの本の記述かをたどれなかった
- 依頼で求められた項目に合うデータベースだけを引く（config の when。「東京タワーの高さ」で、題名に「東京タワー」を
  含む本や写真の記録を読まない）。依頼が対象の全体を尋ねるとき（「〜について」など）は引く
- データベースごとの結果（引いた・0 件・失敗・設定がない・依頼に合わない）を statuses に残す（テレメトリー）
"""

from __future__ import annotations

import os
import re
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import quote

from research_agent.agent.names import mentions
from research_agent.config import REPO_ROOT
from research_agent.tools import http

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
    scientific_name: str = ""   # 学名（生物）。GBIF はこれで引く（表示用の text の見出しに頼らない）
    source_id: str = ""         # 提供元の資料 ID（国会図書館の R…、ジャパンサーチの項目 ID など）
    kind: str = ""              # 構造化データの種類（書誌情報・所蔵品情報・研究者情報など。出典に添える）
    fields: list[tuple[str, str]] = field(default_factory=list)   # 根拠に使った項目名と値
    holder_url: str = ""        # 所蔵元・提供元の個別ページ（ジャパンサーチの所蔵元へのリンクなど）
    identity: str = ""          # 人の資料で、本人のものか: confirmed（ORCID などで確かめた）・unconfirmed（名前が同じだけ）


@lru_cache(maxsize=1)
def _config() -> dict:
    with open(DATABASES_FILE, "rb") as f:
        return tomllib.load(f)


def databases_for(subject_type: str) -> list[dict]:
    """その種類で使うデータベース（使えるものだけ）。"""
    return [d for d in _config().get("databases", [])
            if subject_type in d.get("types", []) and d.get("available", True)
            and (not d.get("requires_env") or os.getenv(d["requires_env"]))]


# 依頼が対象の全体を尋ねている（特定の項目に絞っていない）
_GENERAL = re.compile(r"について|とは|の概要|を調べて$|を教えて$|全般|詳しく|まとめ")


def wanted(db: dict, request: str) -> bool:
    """依頼で求められた項目に、このデータベースが答えられるか。when（その資料を求める言葉）がなければいつも引く。
    when があれば、依頼にその言葉があるか、依頼が対象の全体を尋ねているときだけ引く。"""
    when = db.get("when") or []
    if not when:
        return True
    if not request:
        # 依頼文がない（CLI・テストで種類だけを渡す）ときは、その資料を求めたときだけ引くもの（general = false）は引かない
        return db.get("general", True)
    # general = false のデータベース（写真などの所蔵品）は、全体を尋ねる依頼では引かない（その資料を求めたときだけ）
    return any(w in request for w in when) or (db.get("general", True) and bool(_GENERAL.search(request)))


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
                          "ORCID", source_id=oid, kind="研究者情報",
                          fields=[("氏名", lines[1].removeprefix("氏名: ")), ("所属機関", "、".join(r.get("institution-name") or [])),
                                  ("ORCID iD", oid)]))
    return out[:MAX_ITEMS]


def normalize_doi(doi: str) -> str:
    doi = (doi or "").strip().lower()
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi)


def _doi_of(it: dict) -> str:
    """CiNii の項目の DOI（prism:doi か、dc:identifier のうち DOI の型のもの）。"""
    if it.get("prism:doi"):
        return normalize_doi(it["prism:doi"])
    ids = it.get("dc:identifier") or []
    for x in ids if isinstance(ids, list) else [ids]:
        if isinstance(x, dict) and "doi" in str(x.get("@type", "")).lower():
            return normalize_doi(x.get("@value", ""))
    return ""


def cinii(name: str) -> list[Record]:
    """CiNii Research で、対象が著者の文献（業績）。1 件 = 文献 1 本（個別のページ・DOI があれば DOI）。"""
    data = _json("https://cir.nii.ac.jp/opensearch/all",
                 {"q": name, "format": "json", "count": 20, "appid": os.getenv("CINII_APPID", "")})
    out = []
    for it in data.get("items") or []:
        creators = it.get("dc:creator") or []
        creators = [creators] if isinstance(creators, str) else creators
        if not any(mentions(c, [name]) for c in creators):
            continue
        url = it.get("@id") or (it.get("link") or {}).get("@id") or ""
        if not url:
            continue                                       # どの文献かを特定できない
        doi = _doi_of(it)
        fields = [(k, v) for k, v in (("論文名", it.get("title", "")), ("著者", "、".join(creators[:8])),
                                      ("掲載誌", it.get("prism:publicationName", "")),
                                      ("公開日", it.get("prism:publicationDate", "")), ("種類", it.get("dc:type", "")),
                                      ("DOI", doi)) if v]
        out.append(Record(url, f"CiNii Research: {it.get('title', '')}",
                          "CiNii Research の書誌情報（文献 1 本）\n" + "\n".join(f"{k}: {v}" for k, v in fields),
                          "CiNii Research", source_id=doi or url.rsplit("/", 1)[-1], kind="論文の書誌情報", fields=fields))
        if len(out) >= 5:
            break
    return out


def crossref(doi: str) -> dict | None:
    """Crossref に登録された書誌（論文名・著者と ORCID・公開日・掲載誌）。登録がなければ None。"""
    try:
        msg = _json(f"https://api.crossref.org/works/{quote(doi, safe='/')}").get("message") or {}
    except Exception as e:  # noqa: BLE001
        if "404" in str(e):
            return None
        raise
    def names(a: dict) -> list[str]:
        """名と姓の両方の順（日本語は姓が先）。"""
        g, f = a.get("given") or "", a.get("family") or ""
        return [x for x in (f"{g} {f}".strip(), f"{f}{g}", f"{f} {g}".strip(), a.get("name", "")) if x]
    authors = [{"name": (names(a) or [""])[0], "names": names(a),
                "orcid": (a.get("ORCID") or "").rsplit("/", 1)[-1],
                "affiliation": "、".join(x.get("name", "") for x in a.get("affiliation") or [])}
               for a in msg.get("author") or []]
    parts = ((msg.get("published") or msg.get("issued") or {}).get("date-parts") or [[]])[0]
    return {"doi": normalize_doi(msg.get("DOI", doi)), "title": "".join(msg.get("title") or []),
            "container": "".join(msg.get("container-title") or []), "authors": authors,
            "published": "-".join(str(p) for p in parts if p), "url": f"https://doi.org/{normalize_doi(msg.get('DOI', doi))}"}


def match_papers(records: list[Record], name: str, statuses: list[dict]) -> list[Record]:
    """CiNii の論文を Crossref で照合し、本人の論文だけを残す（同じ DOI は 1 本）。

    - 対象の ORCID iD が 1 つに決まっているとき（ORCID の記録が 1 件）: Crossref の著者のうち名前が合う人の ORCID が
      それと違えば、同名の別人の論文として外す
    - 手掛かり（ORCID・所属）がなければ外さず、「同定: 未確定」と書く
    - CiNii と Crossref が同じ DOI を返したら、論文 1 本として扱い、照合に使った双方の出典（CiNii のページ・DOI のページ）を残す
    """
    orcids = [r.source_id for r in records if r.database == "ORCID" and r.source_id]
    own = orcids[0] if len(orcids) == 1 else ""
    out, seen, checked, failed = [], set(), 0, ""
    for r in records:
        if r.kind != "論文の書誌情報":
            out.append(r)
            continue
        doi = dict(r.fields).get("DOI", "")
        if doi and doi in seen:
            continue                                       # 同じ論文（同じ DOI）は 1 本
        cr = None
        if doi:
            try:
                cr, checked = crossref(doi), checked + 1
            except Exception as e:  # noqa: BLE001
                failed = type(e).__name__
        same_name = [a for a in (cr or {}).get("authors", []) if any(mentions(n, [name]) for n in a.get("names") or [a["name"]])]
        if own and same_name and all(a["orcid"] and a["orcid"] != own for a in same_name):
            continue                                       # 同名の別人（ORCID が違う）
        if own and any(a["orcid"] == own for a in same_name):
            who, identity = f"同定: ORCID iD {own} が著者に登録されている", "confirmed"
        else:
            who, identity = "同定: 未確定（ORCID・所属などの手掛かりがない）", "unconfirmed"
        lines = r.text.split("\n") + [who]
        fields = list(r.fields)
        if cr:
            lines += [f"Crossref の書誌（同じ DOI）: {cr['title']}（{cr['container']}、{cr['published']}）",
                      f"照合に使った出典: {r.url} と {cr['url']}"]
            fields += [("Crossref の論文名", cr["title"]), ("Crossref の公開日", cr["published"]), ("DOI のページ", cr["url"])]
        if doi:
            seen.add(doi)
        out.append(Record(r.url, r.title, "\n".join(lines), r.database, r.images, source_id=r.source_id, kind=r.kind,
                          fields=fields, holder_url=cr["url"] if cr else r.holder_url, identity=identity))
    if checked or failed:
        statuses.append(_status("Crossref", "failed" if failed and not checked else "ok", failed, checked))
    return out


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
        out.append(Record(url, f"iNaturalist: {common or t.get('name', '')}", "\n".join(lines), "iNaturalist", images,
                          scientific_name=t.get("name", "")))
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
    """ジャパンサーチ（全国の博物館・図書館などの所蔵品）。1 件 = 所蔵品 1 つ（個別のページ・提供機関・所蔵元のページ）。"""
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
        lines.append(f"ジャパンサーチの項目 ID: {it['id']}")
        license_ = _RIGHTS.get(c.get("contentsRightsType", ""), c.get("contentsRightsType", ""))
        images = [{"src": src, "page_url": url, "title": f"ジャパンサーチ: {title}",
                   "caption": f"{c.get('provider', '')}の所蔵品「{title}」の画像", "license": license_}
                  for src in (c.get("thumbnailUrl") or [])[:2]]
        provider = c.get("provider", "")
        provider = "、".join(provider) if isinstance(provider, list) else provider
        out.append(Record(url, f"ジャパンサーチ: {title}", "\n".join(lines), "ジャパンサーチ", images,
                          source_id=str(it["id"]), kind="所蔵品情報", holder_url=c.get("linkUrl") or "",
                          fields=[(k, v) for k, v in (("名称", title), ("提供機関", provider),
                                                      ("所蔵元のページ", c.get("linkUrl") or "")) if v]))
        if len(out) >= MAX_ITEMS:
            break
    return out


_NS = {"dc": "http://purl.org/dc/elements/1.1/", "dcterms": "http://purl.org/dc/terms/",
       "dcndl": "http://ndl.go.jp/dcndl/terms/"}


def _ndl_id(link: str) -> str:
    """国会図書館サーチの資料のページ（…/books/R100000002-I000…）から資料 ID。"""
    m = re.search(r"/books/([A-Za-z0-9-]+)", link or "")
    return m.group(1) if m else ""


def ndl(name: str, by_creator: bool = False) -> list[Record]:
    """国立国会図書館サーチ。対象を題名に含む資料（人物なら、対象が著者の資料）。1 件 = 資料 1 冊（個別のページ・
    資料 ID・書名・著者・出版者・刊行年）。個別のページがない項目は、どの資料かを特定できないので返さない。"""
    r = http.get("https://ndlsearch.ndl.go.jp/api/opensearch",
                 params={"creator" if by_creator else "title": name, "cnt": 10}, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out, seen = [], set()
    for item in root.iter("item"):
        title = item.findtext("dc:title", "", _NS) or item.findtext("title", "")
        creators = [c.text.strip() for c in item.findall("dc:creator", _NS) if c.text and c.text.strip()]
        creator = "、".join(creators) or item.findtext("author", "")
        if not (mentions(creator, [name]) if by_creator else mentions(title, [name])):
            continue
        link = (item.findtext("link", "") or item.findtext("guid", "")).strip()
        source_id = _ndl_id(link)
        if not link or not source_id or source_id in seen:
            continue                                       # どの資料かを特定できない・同じ資料
        seen.add(source_id)
        publisher = "、".join(p.text.strip() for p in item.findall("dc:publisher", _NS) if p.text and p.text.strip())
        issued = item.findtext("dcterms:issued", "", _NS) or item.findtext("dc:date", "", _NS) or ""
        # 種別（図書・記事など）。記事は出版者・刊行年がなく、掲載誌が dc:description にある（「掲載誌：日経アーキテクチュア 892」）
        kinds = [c.text.strip() for c in item.findall("category") if c.text and c.text.strip() not in ("紙", "デジタル")]
        desc = (item.findtext("dc:description", "", _NS) or "").strip()
        series = (item.findtext("dcndl:seriesTitle", "", _NS) or "").strip()
        fields = [(k, v) for k, v in (("書名", title), ("種別", "・".join(kinds[:2])), ("著者", creator), ("出版者", publisher),
                                      ("刊行年", issued.strip()), ("掲載", desc.removeprefix("掲載誌：") if desc.startswith("掲載誌") else ""),
                                      ("シリーズ", series), ("資料 ID", source_id)) if v]
        text = "国立国会図書館サーチの書誌情報（資料 1 冊）\n" + "\n".join(f"{k}: {v}" for k, v in fields)
        if by_creator:
            # 著者名で引いた資料は、名前が同じだけの別の人のものかもしれない（所属などで確かめられない）
            text += "\n同定: 未確定（著者名が同じというだけで、本人の資料か確かめていない）"
        out.append(Record(link, f"国立国会図書館サーチ: {title}", text, "国立国会図書館サーチ",
                          source_id=source_id, kind="書誌情報", fields=fields,
                          identity="unconfirmed" if by_creator else ""))
        if len(out) >= MAX_ITEMS:
            break
    return out


def ndl_creator(name: str) -> list[Record]:
    return ndl(name, by_creator=True)


# --- 統計（e-Stat） -----------------------------------------------------------

ESTAT = "https://api.e-stat.go.jp/rest/3.0/app/json"
# 秘匿・欠測などの記号は 0 にしない（そのまま「記号と意味」で書く）
_SPECIAL = re.compile(r"^[-x…*.]+$|^\*+$|^X$")


def _as_list(x) -> list:
    return x if isinstance(x, list) else [x] if x else []


def estat_population(name: str, request: str = "", stats_data_id: str = "") -> list[Record]:
    """e-Stat の統計表から、対象の地域・時点の人口（国勢調査の市区町村の人口など。統計表 ID は config の stats_data_id）。

    依頼に年（「2020年」）があればその時点、なければ表のいちばん新しい時点。統計表 ID・表題・地域・時点・集計区分・
    単位・値を一緒に持つ。秘匿値・欠測値（「-」「x」など）は 0 にせず、記号と注記のまま返す。"""
    app = os.getenv("ESTAT_APPID", "")
    meta = _json(f"{ESTAT}/getMetaInfo", {"appId": app, "statsDataId": stats_data_id})
    inf = (meta.get("GET_META_INFO") or {}).get("METADATA_INF") or {}
    table = inf.get("TABLE_INF") or {}
    classes = {c.get("@id"): _as_list(c.get("CLASS")) for c in _as_list((inf.get("CLASS_INF") or {}).get("CLASS_OBJ"))}
    areas = [a for a in classes.get("area", []) if a.get("@name") == name or a.get("@name", "").endswith(name)]
    if not areas:
        return []
    area = areas[0]
    times = classes.get("time", [])
    year = re.search(r"(19|20)\d{2}", request or "")
    time = next((t for t in times if year and year.group(0) in t.get("@name", "") + t.get("@code", "")), None) \
        or (max(times, key=lambda t: t.get("@code", "")) if times else None)
    params = {"appId": app, "statsDataId": stats_data_id, "cdArea": area["@code"]}
    if time:
        params["cdTime"] = time["@code"]
    data = (_json(f"{ESTAT}/getStatsData", params).get("GET_STATS_DATA") or {}).get("STATISTICAL_DATA") or {}
    inf2 = data.get("DATA_INF") or {}
    notes = {n.get("@char"): n.get("$", "") for n in _as_list(inf2.get("NOTE"))}
    cats = {k: {c.get("@code"): c.get("@name") for c in v} for k, v in classes.items() if k.startswith("cat") or k == "tab"}
    out = []
    title = (table.get("TITLE") or {}).get("$", "") if isinstance(table.get("TITLE"), dict) else str(table.get("TITLE", ""))
    stat = (table.get("STATISTICS_NAME") or "") if isinstance(table.get("STATISTICS_NAME"), str) else ""
    for v in _as_list(inf2.get("VALUE")):
        if v.get("@area") != area["@code"] or (time and v.get("@time") != time["@code"]):
            continue
        raw = str(v.get("$", ""))
        value = f"{raw}（{notes[raw]}）" if raw in notes else (f"{raw}（数値なし）" if _SPECIAL.match(raw) else raw)
        breakdown = "、".join(cats[k].get(v.get(f"@{k}"), "") for k in sorted(cats) if v.get(f"@{k}"))
        fields = [(k, x) for k, x in (("統計表 ID", stats_data_id), ("統計", stat), ("表題", title), ("地域", area["@name"]),
                                      ("時点", (time or {}).get("@name", "")), ("集計区分", breakdown),
                                      ("単位", v.get("@unit", "")), ("値", value)) if x]
        out.append(Record(f"https://www.e-stat.go.jp/dbview?sid={stats_data_id}", f"e-Stat: {title or stat} {area['@name']}",
                          "e-Stat の統計表の値\n" + "\n".join(f"{k}: {x}" for k, x in fields), "e-Stat",
                          source_id=f"{stats_data_id}:{area['@code']}:{(time or {}).get('@code', '')}", kind="統計表の値",
                          fields=fields))
    return out[:MAX_ITEMS]


# --- 論文（対象の題目についての研究）-----------------------------------------
# 人の業績（CiNii。kind = 論文の書誌情報）とは分ける: 題目の論文は、同名の別人の照合（match_papers）に回さない
PAPER = "研究論文の書誌情報"
PAPER_MAX = 5


def _plain(s: str) -> str:
    """書誌の題・要旨の飾り（JATS の <jats:p> など）を外す。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _paper(url: str, database: str, source_id: str, fields: list[tuple[str, str]], abstract: str = "") -> Record:
    fields = [(k, v) for k, v in fields if v]
    lines = [f"{database} の書誌情報（論文 1 本）", *(f"{k}: {v}" for k, v in fields)]
    if abstract:
        lines.append(f"要旨: {abstract[:1200]}")
    return Record(url, f"{database}: {dict(fields).get('論文名', '')}", "\n".join(lines), database,
                  source_id=source_id, kind=PAPER, fields=fields)


# 論文として読む Crossref の種類（図・表の部品 component、データセット、本全体などは読まない）
_CROSSREF_TYPES = {"journal-article", "proceedings-article", "posted-content", "book-chapter", "report", "dissertation"}


def crossref_search(name: str) -> list[Record]:
    """Crossref の書誌の検索（登録なし）。題か要旨に対象の名前がある論文だけ（関連の薄いものを読まない）。"""
    data = _json("https://api.crossref.org/works",
                 {"query.bibliographic": name, "rows": 40,
                  "select": "DOI,title,author,issued,container-title,is-referenced-by-count,type,abstract"})
    out = []
    # 候補 40 件のうち、よく引用されている順に読む（Crossref の関連順のままだと、ほとんど引用のない論文が先に来る）
    items = sorted((data.get("message") or {}).get("items") or [], key=lambda it: -(it.get("is-referenced-by-count") or 0))
    for it in items:
        title, abstract = _plain("".join(it.get("title") or [])), _plain(it.get("abstract", ""))
        doi = normalize_doi(it.get("DOI", ""))
        if not title or not doi or it.get("type") not in _CROSSREF_TYPES or not mentions(f"{title} {abstract}", [name]):
            continue
        authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "") for a in it.get("author") or []]
        year = str(((it.get("issued") or {}).get("date-parts") or [[""]])[0][0] or "")
        out.append(_paper(f"https://doi.org/{doi}", "Crossref", doi, [
            ("論文名", title), ("著者", "、".join(authors[:6]) + ("ほか" if len(authors) > 6 else "")),
            ("掲載誌", "".join(it.get("container-title") or [])), ("発行年", year), ("種類", it.get("type", "")),
            ("被引用数（Crossref）", str(it.get("is-referenced-by-count", ""))), ("DOI", doi)], abstract))
        if len(out) >= PAPER_MAX:
            break
    return out


_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def arxiv(name: str) -> list[Record]:
    """arXiv のプレプリント（登録なし。査読前の論文）。arXiv の求め: 3 秒に 1 回まで。"""
    r = http.get("https://export.arxiv.org/api/query",
                 params={"search_query": f'all:"{name}"', "max_results": PAPER_MAX, "sortBy": "relevance"},
                 headers=http.HEADERS, timeout=45.0, min_interval=3.0)   # arXiv の API は混むと 40 秒ほどかかる
    r.raise_for_status()
    out = []
    for e in ET.fromstring(r.content).findall("a:entry", _ATOM):
        title = _plain(e.findtext("a:title", "", _ATOM))
        url = (e.findtext("a:id", "", _ATOM) or "").strip()
        if not title or not url:
            continue
        authors = [_plain(a.findtext("a:name", "", _ATOM)) for a in e.findall("a:author", _ATOM)]
        cat = e.find("arxiv:primary_category", _ATOM)
        out.append(_paper(url, "arXiv", url.rsplit("/abs/", 1)[-1], [
            ("論文名", title), ("著者", "、".join(authors[:6]) + ("ほか" if len(authors) > 6 else "")),
            ("公開日", (e.findtext("a:published", "", _ATOM) or "")[:10]), ("分野", cat.get("term", "") if cat is not None else ""),
            ("種類", "プレプリント（査読前）")], _plain(e.findtext("a:summary", "", _ATOM))))
    return out


def openalex(name: str) -> list[Record]:
    """OpenAlex の論文（OPENALEX_API_KEY が要る。無料の登録）。被引用数・掲載誌・要旨がそろう。"""
    data = _json("https://api.openalex.org/works", {"search": name, "per_page": PAPER_MAX, "api_key": os.getenv("OPENALEX_API_KEY", "")})
    out = []
    for w in data.get("results") or []:
        title = _plain(w.get("display_name", ""))
        doi = normalize_doi(w.get("doi") or "")
        url = f"https://doi.org/{doi}" if doi else (w.get("id") or "")
        if not title or not url:
            continue
        inv = w.get("abstract_inverted_index") or {}
        words = sorted((p, word) for word, ps in inv.items() for p in ps)
        authors = [(a.get("author") or {}).get("display_name", "") for a in w.get("authorships") or []]
        source = ((w.get("primary_location") or {}).get("source") or {}).get("display_name", "")
        out.append(_paper(url, "OpenAlex", doi or url.rsplit("/", 1)[-1], [
            ("論文名", title), ("著者", "、".join(authors[:6]) + ("ほか" if len(authors) > 6 else "")),
            ("掲載誌", source), ("発行年", str(w.get("publication_year") or "")), ("種類", w.get("type", "")),
            ("被引用数（OpenAlex）", str(w.get("cited_by_count", ""))), ("DOI", doi)], " ".join(word for _, word in words)))
    return out


# --- 商品の販売（EC）--------------------------------------------------------
# 価格は変わるので、取った日時を必ず書く。在庫のある商品だけ。1 件 = 店の商品 1 つ
SHOP = "商品の販売情報"
SHOP_MAX = 5


def _now() -> str:
    from datetime import datetime
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def _item(url: str, database: str, source_id: str, fields: list[tuple[str, str]]) -> Record:
    fields = [(k, v) for k, v in fields if v] + [("取得日時", _now())]
    text = "\n".join([f"{database} の商品情報（店の商品 1 つ。価格は取得日時のもの）", *(f"{k}: {v}" for k, v in fields)])
    return Record(url, f"{database}: {dict(fields).get('商品名', '')}", text, database, source_id=source_id, kind=SHOP, fields=fields)


def _yen(v) -> str:
    try:
        return f"{int(float(v)):,} 円"
    except (TypeError, ValueError):
        return ""


def yahoo_shopping(name: str) -> list[Record]:
    """Yahoo!ショッピングの商品検索 v3（YAHOO_CLIENT_ID が要る。1 秒に 1 回まで。クレジット表示が要る）。"""
    r = http.get("https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch",
                 params={"appid": os.getenv("YAHOO_CLIENT_ID", ""), "query": name, "results": 10, "in_stock": "true", "sort": "-score"},
                 headers={**http.HEADERS, "Accept": "application/json"}, timeout=TIMEOUT, min_interval=1.0)
    r.raise_for_status()
    out = []
    for h in r.json().get("hits") or []:
        title, url = h.get("name", ""), h.get("url", "")
        if not title or not url or not mentions(title, [name]):
            continue
        review = h.get("review") or {}
        taxed = (h.get("priceLabel") or {}).get("taxable")
        out.append(_item(url, "Yahoo!ショッピング", h.get("code", "") or url, [
            ("商品名", title), ("価格", _yen(h.get("price")) + ("（税込）" if taxed else "")), ("店", (h.get("seller") or {}).get("name", "")),
            ("ブランド", (h.get("brand") or {}).get("name", "")), ("JAN コード", h.get("janCode", "")),
            ("レビュー", f"{review.get('rate')}（{review.get('count')} 件）" if review.get("count") else "")]))
        if len(out) >= SHOP_MAX:
            break
    return out


# 楽天市場の商品検索 API（2026 年の新しい API）。公式の説明が読めなかったので、移行の報告（2026-07 〜 10）に合わせている:
# 住所 openapi.rakuten.co.jp、applicationId（UUID）と accessKey の両方、Referer（アプリに登録したサイトの URL）が要る
RAKUTEN_SEARCH = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"


def rakuten(name: str) -> list[Record]:
    """楽天市場の商品検索（RAKUTEN_APP_ID・RAKUTEN_ACCESS_KEY・RAKUTEN_REFERER が要る）。"""
    r = http.get(RAKUTEN_SEARCH,
                 params={"applicationId": os.getenv("RAKUTEN_APP_ID", ""), "accessKey": os.getenv("RAKUTEN_ACCESS_KEY", ""),
                         "keyword": name, "hits": 10, "availability": 1, "formatVersion": 2, "format": "json"},
                 headers={**http.HEADERS, "Accept": "application/json", "Referer": os.getenv("RAKUTEN_REFERER", "")},
                 timeout=TIMEOUT, min_interval=1.0)
    if r.status_code >= 400:
        try:
            body = r.json()
            err = (body.get("errors") or {}).get("errorMessage") or body.get("error_description") or body.get("error") or ""
        except ValueError:
            err = ""
        raise RuntimeError(f"HTTP {r.status_code} {err}".strip())
    out = []
    for it in r.json().get("Items") or []:
        it = it.get("Item", it) if isinstance(it, dict) else {}           # formatVersion 1 は {"Item": {...}}
        title, url = it.get("itemName", ""), it.get("itemUrl", "")
        if not title or not url or not mentions(title, [name]):
            continue
        out.append(_item(url, "楽天市場", it.get("itemCode", "") or url, [
            ("商品名", title), ("価格", _yen(it.get("itemPrice")) + ("（税込）" if it.get("taxFlag") == 0 else "")),
            ("店", it.get("shopName", "")),
            ("レビュー", f"{it.get('reviewAverage')}（{it.get('reviewCount')} 件）" if it.get("reviewCount") else "")]))
        if len(out) >= SHOP_MAX:
            break
    return out


FUNCTIONS = {"orcid": orcid, "cinii": cinii, "inaturalist": inaturalist, "japan_search": japan_search,
             "ndl": ndl, "ndl_creator": ndl_creator, "estat_population": estat_population,
             "crossref_search": crossref_search, "arxiv": arxiv, "openalex": openalex,
             "yahoo_shopping": yahoo_shopping, "rakuten": rakuten}
# 依頼文や設定（config の項目）も受け取る関数
_WITH_CONTEXT = {"estat_population"}


class LookupResult(tuple):
    """(records, failures, used) として分けられる結果。statuses にデータベースごとの結果を持つ
    （{name, state, reason, records}。state: ok・empty・failed・unconfigured・disabled・skipped）。"""
    statuses: list[dict]


def _status(name: str, state: str, reason: str = "", records: int = 0) -> dict:
    return {"name": name, "state": state, "reason": reason, "records": records}


def lookup(subject_type: str, name: str, request: str = "") -> LookupResult:
    """その種類のデータベースを順に引く。request（依頼文）があれば、依頼に合うものだけを引く（wanted）。

    戻り値は (見つかった Record, 使えなかったデータベースと理由, 問い合わせたデータベースの名前)。
    問い合わせた名前には、続けて引いたもの（iNaturalist の学名で引く GBIF）も入れる（報告で申告するため）。
    statuses には、引かなかったもの（設定がない・依頼に合わない）も含めて、データベースごとの結果を残す。
    """
    records: list[Record] = []
    failures: list[str] = []
    used: list[str] = []
    statuses: list[dict] = []
    for db in _config().get("databases", []):
        if subject_type not in db.get("types", []):
            continue
        if not db.get("available", True):
            statuses.append(_status(db["name"], "disabled", "API を使わない設定（site: 検索に回す）"))
            continue
        need = db.get("requires_env") or []
        missing = [e for e in ([need] if isinstance(need, str) else need) if not os.getenv(e)]
        if missing:
            statuses.append(_status(db["name"], "unconfigured", f"{'・'.join(missing)} が設定されていない"))
            continue
        if not wanted(db, request):
            statuses.append(_status(db["name"], "skipped", "依頼の項目に合わない"))
            continue
        fn = FUNCTIONS.get(db["function"])
        if not fn:
            continue
        if db["function"] in _WITH_CONTEXT and not db.get("stats_data_id"):
            statuses.append(_status(db["name"], "unconfigured", "使う統計表 ID（stats_data_id）が設定されていない"))
            continue
        used.append(db["name"])
        try:
            found = fn(name, request, db.get("stats_data_id", "")) if db["function"] in _WITH_CONTEXT else fn(name)
        except Exception as e:  # noqa: BLE001 - 1 つ使えなくても他を引く。使えなかったことは申告する
            failures.append(f"{db['name']}（{type(e).__name__}）")
            statuses.append(_status(db["name"], "failed", type(e).__name__))
            continue
        records += found
        statuses.append(_status(db["name"], "ok" if found else "empty", "" if found else "0 件", len(found)))
        if db["function"] == "inaturalist" and found:
            # GBIF は学名で引く（和名では引けない）
            sci = found[0].scientific_name
            if sci:
                used.append("GBIF")
                try:
                    g = gbif(sci)
                    records += g
                    statuses.append(_status("GBIF", "ok" if g else "empty", "" if g else "0 件", len(g)))
                except Exception as e:  # noqa: BLE001
                    failures.append(f"GBIF（{type(e).__name__}）")
                    statuses.append(_status("GBIF", "failed", type(e).__name__))
    if any(r.kind == "論文の書誌情報" for r in records):
        records = match_papers(records, name, statuses)    # 論文は Crossref で照合し、同名の別人・同じ DOI を除く
        if any(s["name"] == "Crossref" for s in statuses):
            used.append("Crossref")
            failures += [f"Crossref（{s['reason']}）" for s in statuses if s["name"] == "Crossref" and s["state"] == "failed"]
    result = LookupResult((records, failures, used))
    result.statuses = statuses
    return result
