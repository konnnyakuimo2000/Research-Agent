"""ページ本文の取得（設計書 6.6: 判断と引用の照合は、必ずここで取得した本文で行う）。"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import trafilatura

from my_llm_app.config import USER_AGENT
from my_llm_app.tiers import EXCLUDED_TIER, tier_of
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


def is_skipped_domain(url: str) -> bool:
    """出典にしないサイト（Tier 5: 転載・量産・まとめ）か。"""
    return tier_of(url) == EXCLUDED_TIER


def fetch_page(url: str, timeout: float = 20.0) -> Page:
    if is_skipped_domain(url):
        return Page(url, reason="出典にしないサイト（転載・まとめ）")
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        return Page(url, reason=f"取得できない（{type(e).__name__}）")
    if r.status_code != 200:
        return Page(url, reason=f"取得できない（HTTP {r.status_code}）")
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype:
        # PDF などは未対応（本文を読めないものは出典にしない）
        return Page(url, reason=f"未対応の形式（{ctype.split(';')[0] or '不明'}）")
    if len(r.content) > MAX_BYTES:
        return Page(url, reason="大きすぎる")

    text = trafilatura.extract(r.text, include_comments=False, include_tables=True, favor_precision=True) or ""
    meta = trafilatura.extract_metadata(r.text)
    title = (meta.title if meta and meta.title else "") or url
    if len(text) < 100:
        return Page(url, title=title, reason="本文が取り出せない")
    return Page(url, title=title, text=text, ok=True, images=page_images(r.text, str(r.url)))
