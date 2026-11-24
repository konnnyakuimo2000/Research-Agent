"""SearXNG による Web 検索（候補を広く集める入り口。設計書 6.6）。"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from research_agent.tools import http

# 検索と検索の間隔（秒）。続けて検索すると、後ろの検索エンジンがタイムアウトしたり一時停止したりする（G-4）
MIN_INTERVAL = 1.0
# 応答しなかったエンジンがあって結果が 0 件のとき、この秒数をおいて 1 回だけやり直す（G-5）
RETRY_DELAY = 5.0


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str
    engines: list[str]


@dataclass
class WebSearchResponse:
    results: list[WebResult]
    suggestions: list[str] = field(default_factory=list)
    # 応答しなかったエンジン（報告で申告するために記録する。設計書 G-5, G-6）
    unresponsive: list[str] = field(default_factory=list)
    retried: bool = False    # 0 件だったので時間をおいてやり直したか


def _readable(url: str) -> bool:
    """取りに行ける URL か（http・https で、サイト名がある）。"""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.hostname)


# Brave の結果は、サイト名のない中継用の URL（「http:///go/v1/…」）で返ってくることがある（SearXNG が Brave の
# 新しい形に追いついていない）。本当の宛先は、Brave の中継ページ（https://search.brave.com/go/v1/…）の
# 「宛先」のリンクに書いてある。2026-11-24 の松田教授の調査では、本人のページ 4 件がすべてこの形で、読めずに終わった
BRAVE = "https://search.brave.com"
_BRAVE_PATH = "/go/v1/"
_DESTINATION = re.compile(r'<a\s+class="destination[^"]*"\s+href="([^"]+)"')


def _is_brave_relay(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return not u.hostname and u.path.startswith(_BRAVE_PATH)


def resolve_brave(url: str, timeout: float = 8.0) -> str:
    """Brave の中継用の URL から本当の宛先を取り出す（取り出せなければ空）。"""
    u = urlparse(url)
    try:
        r = http.get(BRAVE + u.path + (f"?{u.query}" if u.query else ""), timeout=timeout, max_bytes=500_000)
    except Exception:  # noqa: BLE001 - 取り出せなければ、その結果を使わないだけ
        return ""
    m = _DESTINATION.search(r.text or "")
    dest = html.unescape(m.group(1)) if m else ""
    return dest if _readable(dest) and "brave.com" not in (urlparse(dest).hostname or "") else ""


def parse_response(data: dict, limit: int) -> WebSearchResponse:
    results = [
        WebResult(
            title=r.get("title", ""),
            url=r.get("url", ""),
            snippet=(r.get("content") or "").strip(),
            engines=r.get("engines", []),
        )
        for r in data.get("results", [])[:limit]
        if _readable(r.get("url", "")) or _is_brave_relay(r.get("url", ""))
    ]
    return WebSearchResponse(
        results=results,
        suggestions=data.get("suggestions", []),
        unresponsive=[e[0] if isinstance(e, list) else str(e) for e in data.get("unresponsive_engines", [])],
    )


def search_json(base_url: str, params: dict, timeout: float) -> dict:
    """SearXNG に 1 回問い合わせる。間隔をあけ、利用制限はやり直す（http.get）。"""
    url = f"{base_url}/search"
    # SearXNG は設定済みの内部のサービスなので、公開取得の宛先の制限をかけない
    r = http.get(url, params={**params, "format": "json"}, timeout=timeout, min_interval=MIN_INTERVAL, public=False)
    r.raise_for_status()
    data = r.json()
    if not data.get("results"):
        http.forget(url, {**params, "format": "json"})   # 0 件の応答は覚えない（次は取り直す）
    return data


def resolve_relays(out: WebSearchResponse) -> WebSearchResponse:
    """Brave の中継用の URL を本当の宛先に置き換える。取り出せなかった結果は捨てる（読めないページで枠を使わない）。"""
    kept = []
    for r in out.results:
        if _is_brave_relay(r.url):
            r.url = resolve_brave(r.url)
        if _readable(r.url):
            kept.append(r)
    out.results = kept
    return out


def web_search(base_url: str, query: str, limit: int = 8, timeout: float = 30.0) -> WebSearchResponse:
    params = {"q": query}
    out = resolve_relays(parse_response(search_json(base_url, params, timeout), limit))
    if not out.results and out.unresponsive:
        # 応答しなかったエンジンがあって 0 件 → 一時的な失敗かもしれない。時間をおいて 1 回だけやり直す（G-5）
        http.sleep(RETRY_DELAY)
        again = resolve_relays(parse_response(search_json(base_url, params, timeout), limit))
        again.unresponsive = sorted(set(out.unresponsive) | set(again.unresponsive))
        again.retried = True
        return again
    return out
