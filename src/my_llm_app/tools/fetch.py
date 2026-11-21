"""ページ本文の取得（設計書 6.6: 判断と引用の照合は、必ずここで取得した本文で行う）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from lxml import html as lxml_html

from my_llm_app.tiers import EXCLUDED_TIER, tier_of
from my_llm_app.tools import http
from my_llm_app.tools.images import page_images

MAX_BYTES = 3_000_000


@dataclass
class Page:
    url: str
    title: str = ""
    text: str = ""
    ok: bool = False
    reason: str = ""     # 取得できなかった理由（申告と実行記録のため）
    images: list[dict] = field(default_factory=list)   # ページ内の画像（src・alt・キャプション。L4）
    links: list[dict] = field(default_factory=list)    # ページ内のリンク（href・文字・出典欄の中か。R-2）
    final_url: str = ""  # 転送をたどった後の URL（本文の発行元。出典・Tier・リンクの基準はこちら）

    @property
    def source_url(self) -> str:
        return self.final_url or self.url


_UNREADABLE_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip")


def is_unreadable(url: str) -> bool:
    """本文を読めない形式（PDF など）か。読むページの一覧に入れず、枠を使わないようにする。"""
    path = urlparse(url).path.lower()
    # J-STAGE の PDF は「/_pdf/-char/ja」のように拡張子が付かない
    return path.endswith(_UNREADABLE_SUFFIXES) or "/_pdf" in path


def is_skipped_domain(url: str) -> bool:
    """出典にしないサイト（Tier 5: 転載・量産・まとめ）か。"""
    return tier_of(url) == EXCLUDED_TIER


def fetch_page(url: str, timeout: float = 20.0) -> Page:
    """ページを取得して本文を取り出す。公開の宛先だけ（http.get の公開取得）、HTML は MAX_BYTES まで。

    転送された場合は、最終の URL を本文の発行元とする（final_url）。転送先が出典にしないサイトなら読まない。
    """
    if is_skipped_domain(url):
        return Page(url, reason="出典にしないサイト（転載・まとめ）")
    try:
        r = http.get(url, timeout=timeout, max_bytes=MAX_BYTES)
    except http.ResponseTooLarge:
        return Page(url, reason="大きすぎる")
    except http.BlockedDestination:
        return Page(url, reason="公開されていない宛先のため取得しない")
    except httpx.HTTPError as e:
        return Page(url, reason=f"取得できない（{type(e).__name__}）")
    final = str(r.url)
    if final != url and is_skipped_domain(final):
        return Page(url, reason="転送先が出典にしないサイト（転載・まとめ）", final_url=final)
    if r.status_code != 200:
        return Page(url, reason=f"取得できない（HTTP {r.status_code}）", final_url=final)
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype:
        # PDF などは未対応（本文を読めないものは出典にしない）
        return Page(url, reason=f"未対応の形式（{ctype.split(';')[0] or '不明'}）", final_url=final)

    text = trafilatura.extract(r.text, include_comments=False, include_tables=True, favor_precision=True) or ""
    meta = trafilatura.extract_metadata(r.text)
    title = (meta.title if meta and meta.title else "") or final
    if len(text) < 100:
        return Page(url, title=title, reason="本文が取り出せない", final_url=final)
    # 画像・リンクの相対 URL は、本文の発行元（最終の URL）を基準にする
    return Page(url, title=title, text=text, ok=True, images=page_images(r.text, final),
                links=page_links(r.text, final), final_url=final)


_REF_MARKERS = ("reference", "references", "citation", "cite", "footnote", "出典", "参考文献", "脚注", "注釈")


def page_links(html: str, base_url: str, limit: int = 200) -> list[dict]:
    """ページ内のリンクを取り出す。出典欄（Wikipedia の脚注など）の中のリンクには in_refs を付ける。"""
    try:
        doc = lxml_html.fromstring(html)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for a in doc.iter("a"):
        href = a.get("href") or ""
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        href = urljoin(base_url, href)
        if not href.startswith("http"):
            continue
        in_refs = any(any(m in ((p.get("class") or "") + " " + (p.get("id") or "")).lower() for m in _REF_MARKERS)
                      for p in a.iterancestors())
        out.append({"href": href, "text": a.text_content().strip()[:100], "in_refs": in_refs})
        if len(out) >= limit:
            break
    return out
