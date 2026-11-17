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

from my_llm_app.config import USER_AGENT

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_HEADERS = {"User-Agent": USER_AGENT}

MIN_SIDE = 100            # これより小さい画像はアイコンとみなして使わない（V-5）。研究者の顔写真は 120 px 程度のことがある
MAX_BYTES = 8_000_000
VLM_MAX_SIDE = 768        # LLM に渡すときに縮小する大きさ（入力トークンを抑える）
_SKIP_WORDS = ("icon", "icn", "logo", "sprite", "banner", "bnr", "button", "btn", "spacer", "blank", "avatar",
               "emoji", "arrow")


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
        r = httpx.get(src, headers=_HEADERS, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
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
    return f"{parsed.hostname}{parsed.path}"


def commons_license(src: str, timeout: float = 30.0) -> str:
    """Wikimedia の画像の URL から、Commons の利用条件を取る。"""
    name = image_key(src).removeprefix("wikimedia:")
    infos = _imageinfo({"action": "query", "titles": "File:" + name}, timeout)
    return infos[0]["license"] if infos else ""


def _imageinfo(params: dict, timeout: float) -> list[dict]:
    r = httpx.get(COMMONS_API, params={**params, "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
                                       "iiurlwidth": 1024, "format": "json"}, headers=_HEADERS, timeout=timeout)
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
    r = httpx.get(WIKIDATA_API, params={"action": "wbgetclaims", "entity": entity_id, "property": "P18",
                                        "format": "json"}, headers=_HEADERS, timeout=timeout)
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


def searxng_images(base_url: str, query: str, limit: int = 8, timeout: float = 60.0) -> list[ImageCandidate]:
    """SearXNG の画像検索（L1）。Tier は画像ではなく掲載ページの URL で決める（設計書 G-3）。"""
    r = httpx.get(f"{base_url}/search", params={"q": query, "format": "json", "categories": "images"},
                  headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    out = []
    for x in r.json().get("results", []):
        if x.get("img_src") and x.get("url"):
            out.append(ImageCandidate(src=x["img_src"], page_url=x["url"], stage="L1-画像検索",
                                      title=x.get("title", ""), caption=(x.get("content") or "")[:200]))
        if len(out) >= limit:
            break
    return out
