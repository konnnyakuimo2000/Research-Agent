"""SearXNG による Web 検索（候補を広く集める入り口。設計書 6.6）。"""

from __future__ import annotations

from dataclasses import dataclass, field

from my_llm_app.tools import http

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


def parse_response(data: dict, limit: int) -> WebSearchResponse:
    results = [
        WebResult(
            title=r.get("title", ""),
            url=r.get("url", ""),
            snippet=(r.get("content") or "").strip(),
            engines=r.get("engines", []),
        )
        for r in data.get("results", [])[:limit]
        if r.get("url")
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


def web_search(base_url: str, query: str, limit: int = 8, timeout: float = 30.0) -> WebSearchResponse:
    params = {"q": query}
    out = parse_response(search_json(base_url, params, timeout), limit)
    if not out.results and out.unresponsive:
        # 応答しなかったエンジンがあって 0 件 → 一時的な失敗かもしれない。時間をおいて 1 回だけやり直す（G-5）
        http.sleep(RETRY_DELAY)
        again = parse_response(search_json(base_url, params, timeout), limit)
        again.unresponsive = sorted(set(out.unresponsive) | set(again.unresponsive))
        again.retried = True
        return again
    return out
