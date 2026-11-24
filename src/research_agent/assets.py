"""テレメトリー画面に出すサイトのアイコンと画像のサムネイル（docs/telemetry_design.md 2.1）。

画面から外部のサイトに直接取りに行かず、サーバーが取得して runs/.assets/ に保存したものを配る。
- アイコン: サイトのページの <link rel="icon">、なければ /favicon.ico。32px の PNG にする。取れなければ空（画面は頭文字）
- 画像: その調査のイベントに出てきた画像だけを配る（何でも取りに行く中継にしない）。利用条件で載せない画像は配らない
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
from pathlib import Path

from lxml import html as lxml_html
from PIL import Image

from research_agent.agent.imaging import needs_permission
from research_agent.tools import http, images

# 公開のサイトの名前だけ（IP アドレスや、点のない内部の名前には取りに行かない）
_HOST = re.compile(r"^(?=.*[a-z])[a-z0-9-]+(\.[a-z0-9-]+)+$")


def _cache(dir_: Path, name: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    return dir_ / name


def favicon(host: str, dir_: Path) -> bytes:
    """サイトのアイコン（PNG）。取れなければ b""（取れなかったことも覚えて、何度も取りに行かない）。"""
    host = host.lower().strip()
    if not _HOST.match(host):
        return b""
    path = _cache(dir_, f"favicon-{host}.png")
    if path.exists():
        return path.read_bytes()
    data = b""
    try:
        hrefs = []
        page = http.get(f"https://{host}/", timeout=8)
        if page.status_code == 200 and "html" in page.headers.get("content-type", ""):
            doc = lxml_html.fromstring(page.content)
            hrefs = [ln.get("href") for ln in doc.iter("link")
                     if "icon" in (ln.get("rel") or "").lower() and ln.get("href")]
        for href in hrefs[:3] + ["/favicon.ico"]:
            r = http.get(str(page.url.join(href)) if hrefs else f"https://{host}/favicon.ico", timeout=8, cache=False)
            if r.status_code == 200 and not r.headers.get("content-type", "").startswith("text"):
                data = _to_png(r.content)
                if data:
                    break
    except Exception:  # noqa: BLE001 - アイコンが取れなくても画面は頭文字で出す
        data = b""
    path.write_bytes(data)
    return data


def _to_png(raw: bytes, size: int = 32) -> bytes:
    try:
        img = Image.open(io.BytesIO(raw))
        if img.format == "ICO":
            img = max((img.ico.getimage(s) for s in img.ico.sizes()), key=lambda i: i.size[0])
        img = img.convert("RGBA")
        img.thumbnail((size, size))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - 読めない形式（SVG など）
        return b""


def image(src: str, events: list[dict], dir_: Path) -> bytes:
    """調査のイベントに出てきた画像の縮小版（JPEG）。出てこない・載せない画像は b""。"""
    allowed = {e.get("src") for e in events if e.get("type") == "image.fetched" and e.get("ok")}
    withheld = {w.get("src") for e in events if e.get("type") == "image.final" for w in e.get("withheld", [])}
    # 利用に許可が必要な画像は、調査の画面でも出さない（報告に載せないのと同じ扱い）
    withheld |= {e.get("src") for e in events if e.get("type") in ("image.fetched", "image.done")
                 and needs_permission(e.get("license", ""))}
    if src not in allowed or src in withheld:
        return b""
    path = _cache(dir_, "img-" + hashlib.sha1(src.encode()).hexdigest() + ".jpg")
    if path.exists():
        return path.read_bytes()
    fetched = images.fetch_image(src)
    if not fetched.ok:
        return b""
    data = base64.b64decode(fetched.data_url.split(",", 1)[1])
    path.write_bytes(data)
    return data
