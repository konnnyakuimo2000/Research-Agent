"""画像の候補集めと、確認用の画像の取得（設計書 8 章）。

候補集め（横）はプログラムが行い、対象の画像かどうかの判断（縦）は LLM が 1 枚ずつ行う。
"""

from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlparse

import httpx
from lxml import html as lxml_html
from PIL import Image

from research_agent.tools import http

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

MIN_SIDE = 100            # これより小さい画像はアイコンとみなして使わない（V-5）。研究者の顔写真は 120 px 程度のことがある
MAX_BYTES = 8_000_000
VLM_MAX_SIDE = 768        # LLM に渡すときに縮小する大きさ（入力トークンを抑える）
_SKIP_WORDS = ("icon", "icn", "logo", "sprite", "banner", "bnr", "button", "btn", "spacer", "blank", "avatar",
               "emoji", "arrow", "lazyload", "placeholder", "loading", "noimage", "no_image", "dummy")


@dataclass
class ImageCandidate:
    src: str                 # 画像の URL
    page_url: str            # 画像が載っているページ（出典。Tier はこのページで決める）
    stage: str               # 見つけた段階（L2-Wikidata / L2-Commons / L1-画像検索 / L4-ページ内）
    title: str = ""          # ページやファイルのタイトル
    caption: str = ""        # キャプション・alt・説明文
    license: str = ""        # 利用条件（わかる場合）
    extra: dict = field(default_factory=dict)


@dataclass
class FetchedImage:
    ok: bool
    reason: str = ""
    data_url: str = ""       # LLM に渡す縮小画像
    width: int = 0
    height: int = 0


def fetch_image(src: str, timeout: float = 20.0) -> FetchedImage:
    """画像を取得し、使える大きさかを確かめ、LLM に渡す大きさに縮小する（V-5）。"""
    try:
        r = http.get(src, timeout=timeout, cache=False, max_bytes=MAX_BYTES)   # 画像は大きいので覚えない
    except http.ResponseTooLarge:
        return FetchedImage(False, "大きすぎる")
    except http.BlockedDestination:
        return FetchedImage(False, "公開されていない宛先のため取得しない")
    except httpx.HTTPError as e:
        return FetchedImage(False, f"取得できない（{type(e).__name__}）")
    except Exception as e:  # noqa: BLE001 - 1 枚の想定外の失敗で、並行して取っている他の画像と調査を止めない
        return FetchedImage(False, f"取得できない（{type(e).__name__}）")
    if r.status_code != 200:
        return FetchedImage(False, f"取得できない（HTTP {r.status_code}）")
    if not r.headers.get("content-type", "").startswith("image/"):
        return FetchedImage(False, "画像ではない")
    if len(r.content) > MAX_BYTES:
        return FetchedImage(False, "大きすぎる")
    try:
        img = Image.open(io.BytesIO(r.content))
        img.load()
    except Exception:  # noqa: BLE001 - 壊れた画像・未対応の形式（SVG など）
        return FetchedImage(False, "画像として読めない")
    width, height = img.size
    if min(width, height) < MIN_SIDE:
        return FetchedImage(False, f"小さすぎる（{width}×{height}）", width=width, height=height)
    img = img.convert("RGB")
    img.thumbnail((VLM_MAX_SIDE, VLM_MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    return FetchedImage(True, data_url=data_url, width=width, height=height)


def page_images(html: str, base_url: str, limit: int = 10) -> list[dict]:
    """ページ内の画像を、alt・キャプションと一緒に取り出す（L4）。"""
    try:
        doc = lxml_html.fromstring(html)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for img in doc.iter("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        src = urljoin(base_url, src)
        lower = src.lower()
        path = urlparse(lower).path.split(";", 1)[0]   # 「.gif;jsessionid=...」のような後ろの付け足しを除く
        if path.endswith((".svg", ".gif")) or any(w in path.rsplit("/", 1)[-1] for w in _SKIP_WORDS):
            continue
        try:
            w, h = int(img.get("width") or 0), int(img.get("height") or 0)
        except ValueError:
            w = h = 0
        if (w and w < MIN_SIDE) or (h and h < MIN_SIDE):
            continue
        figure = next((a for a in img.iterancestors() if a.tag == "figure"), None)
        caption = ""
        if figure is not None:
            cap = figure.find(".//figcaption")
            caption = cap.text_content().strip() if cap is not None else ""
        out.append({"src": src, "alt": (img.get("alt") or "").strip(), "caption": caption[:200]})
        if len(out) >= limit:
            break
    return out


def is_wikimedia_file(src: str) -> bool:
    return (urlparse(src).hostname or "").endswith("upload.wikimedia.org")


def image_key(src: str) -> str:
    """重複を見分けるための鍵。URL の ? 以降は無視し、Wikimedia はサイズ違いのサムネイルを同じ画像とみなす。"""
    parsed = urlparse(src)
    if is_wikimedia_file(src):
        name = unquote(parsed.path.rsplit("/", 1)[-1])
        return "wikimedia:" + re.sub(r"^\d+px-", "", name).lower()
    # 「/D%C3%BCrst/a.jpg」と「/Dürst/a.jpg」は同じ画像（パーセント符号化の有無だけの違い）
    return f"{parsed.hostname}{unquote(parsed.path)}"


def commons_license(src: str, timeout: float = 30.0) -> str:
    """Wikimedia の画像の URL から、Commons の利用条件を取る。"""
    name = image_key(src).removeprefix("wikimedia:")
    infos = _imageinfo({"action": "query", "titles": "File:" + name}, timeout)
    return infos[0]["license"] if infos else ""


def _imageinfo(params: dict, timeout: float) -> list[dict]:
    r = http.get(COMMONS_API, params={**params, "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
                                       "iiurlwidth": 1024, "format": "json"}, timeout=timeout)
    r.raise_for_status()
    pages = r.json().get("query", {}).get("pages", {})
    out = []
    for p in pages.values():
        info = (p.get("imageinfo") or [{}])[0]
        if not info.get("mime", "").startswith(("image/jpeg", "image/png", "image/webp")):
            continue
        meta = info.get("extmetadata", {})
        out.append({
            "title": p.get("title", ""),
            "src": info.get("thumburl") or info.get("url", ""),
            "page_url": info.get("descriptionurl", ""),
            "license": meta.get("LicenseShortName", {}).get("value", ""),
            "description": _strip_tags(meta.get("ImageDescription", {}).get("value", ""))[:200],
        })
    return out


def _strip_tags(text: str) -> str:
    if not text:
        return ""
    try:
        return lxml_html.fromstring(f"<div>{text}</div>").text_content().strip()
    except Exception:  # noqa: BLE001
        return text


def wikidata_image(entity_id: str, label: str, timeout: float = 30.0) -> list[ImageCandidate]:
    """Wikidata の代表画像（P18）（L2）。"""
    r = http.get(WIKIDATA_API, params={"action": "wbgetclaims", "entity": entity_id, "property": "P18",
                                        "format": "json"}, timeout=timeout)
    r.raise_for_status()
    files = [c["mainsnak"]["datavalue"]["value"] for c in r.json().get("claims", {}).get("P18", [])
             if c.get("mainsnak", {}).get("datavalue")]
    if not files:
        return []
    infos = _imageinfo({"action": "query", "titles": "|".join("File:" + f for f in files[:3])}, timeout)
    return [ImageCandidate(src=i["src"], page_url=i["page_url"], stage="L2-Wikidata", title=i["title"],
                           caption=f"Wikidata の項目「{label}」（{entity_id}）の代表画像。{i['description']}",
                           license=i["license"]) for i in infos]


def commons_search(query: str, limit: int = 5, timeout: float = 30.0) -> list[ImageCandidate]:
    """Wikimedia Commons のファイル検索（L2）。"""
    infos = _imageinfo({"action": "query", "generator": "search", "gsrsearch": query, "gsrnamespace": 6,
                        "gsrlimit": limit}, timeout)
    return [ImageCandidate(src=i["src"], page_url=i["page_url"], stage="L2-Commons", title=i["title"],
                           caption=i["description"], license=i["license"]) for i in infos]


def commons_category(name: str, limit: int = 5, timeout: float = 30.0) -> list[ImageCandidate]:
    """Wikimedia Commons のカテゴリから画像を探す（L2）。

    ファイル名に名前がない画像（「File:Olympus pen ee3.jpg」など）は、ファイル検索では見つからないことがある。
    対象の名前のカテゴリ（「Category:Olympus Pen EE-3」）にある画像は、対象を写したものとして分類されている。
    カテゴリ名が対象の名前と一致するかは呼び出し側（mentions）で確かめる。ここでは候補を返すだけ。
    """
    r = http.get(COMMONS_API, params={"action": "query", "list": "search", "srsearch": name, "srnamespace": 14,
                                       "srlimit": 3, "format": "json"}, timeout=timeout)
    r.raise_for_status()
    out: list[ImageCandidate] = []
    for hit in r.json().get("query", {}).get("search", []):
        cat = hit["title"]
        infos = _imageinfo({"action": "query", "generator": "categorymembers", "gcmtitle": cat, "gcmtype": "file",
                            "gcmlimit": limit}, timeout)
        out += [ImageCandidate(src=i["src"], page_url=i["page_url"], stage="L2-Commonsカテゴリ", title=i["title"],
                               caption=f"{cat} に分類された画像。{i['description']}"[:300], license=i["license"],
                               extra={"category": cat.removeprefix("Category:")}) for i in infos]
    return out


def searxng_images(base_url: str, query: str, limit: int = 8, timeout: float = 60.0) -> list[ImageCandidate]:
    """SearXNG の画像検索（L1）。Tier は画像ではなく掲載ページの URL で決める（設計書 G-3）。"""
    from research_agent.tools.searxng import search_json   # 検索の間隔・やり直しは Web 検索と共通

    out = []
    for x in search_json(base_url, {"q": query, "categories": "images"}, timeout).get("results", []):
        if x.get("img_src") and x.get("url"):
            out.append(ImageCandidate(src=x["img_src"], page_url=x["url"], stage="L1-画像検索",
                                      title=x.get("title", ""), caption=(x.get("content") or "")[:200]))
        if len(out) >= limit:
            break
    return out


# L5 で使う関係（設計書 8.1: 所在地・上位の分類・関連するもの）。対象そのものの写真がないときに出す
RELATIONS = {"P179": "シリーズ", "P361": "上位のもの", "P171": "上位の分類群", "P276": "所在地", "P131": "所在する地域"}


def related_images(entity_id: str, limit: int = 3, timeout: float = 30.0) -> list[ImageCandidate]:
    """周辺の画像（L5）。対象と関係のある項目（シリーズ・上位の分類・所在地など）の代表画像。

    対象そのものではないので、判定は「参考画像」にする（呼び出し側）。extra に関係と項目の名前を入れる。
    """
    from research_agent.tools import wikimedia

    c = wikimedia.claims(entity_id, timeout)
    related = [(qid, rel) for prop, rel in RELATIONS.items() for qid in wikimedia.claim_values(c, prop)][:limit]
    if not related:
        return []
    labels = wikimedia.get_entities([q for q, _ in related], timeout)
    out: list[ImageCandidate] = []
    for qid, rel in related:
        files = wikimedia.claim_values(wikimedia.claims(qid, timeout), "P18")[:1]
        if not files or qid not in labels:
            continue
        for i in _imageinfo({"action": "query", "titles": "File:" + files[0]}, timeout):
            name = labels[qid].label
            out.append(ImageCandidate(src=i["src"], page_url=i["page_url"], stage="L5-周辺", title=i["title"],
                                      caption=f"「{name}」（{rel}）の代表画像。{i['description']}"[:300],
                                      license=i["license"], extra={"related": name, "relation": rel, "id": qid}))
    return out
