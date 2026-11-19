"""外部サービスへの GET をまとめる（設計書 6.6 G-4, G-5、実装計画フェーズ 6「呼び出しの堅牢化」）。

- 利用制限（429）・一時的な失敗（5xx・接続できない）は、間隔をあけて最大 MAX_RETRIES 回やり直す。
  Retry-After が MAX_WAIT 秒より長いときは待たずに、その応答を返す（調査の時間を使い切らないように）
- 同じ URL・引数への GET は CACHE_TTL 秒のあいだ覚えておき、取りに行かない（同じ調査・続けての調査で同じページを読む）
- 同じサービスへの間隔（min_interval）を守る。SearXNG の後ろの検索エンジンは、続けて検索すると止められる（G-4）
- User-Agent はこのアプリのもの（Wikimedia などの API の決まり）。なりすましはしない
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from my_llm_app.config import USER_AGENT

HEADERS = {"User-Agent": USER_AGENT}
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 2
BASE_WAIT = 1.5          # やり直しの前に待つ秒数（1 回ごとに 2 倍）
MAX_WAIT = 10.0          # これより長く待てと言われたら、やり直さない
CACHE_TTL = 3600.0
CACHE_MAX = 256
CACHE_MAX_BYTES = 2_000_000

sleep = time.sleep       # テストで差し替える

_cache: OrderedDict[str, tuple[float, httpx.Response]] = OrderedDict()
_cache_lock = threading.Lock()
_last_call: dict[str, float] = {}
_host_locks: dict[str, threading.Lock] = {}
_hosts_lock = threading.Lock()
stats = {"retries": 0, "rate_limited": 0, "cache_hits": 0}


def _key(url: str, params: dict | None) -> str:
    return url + "?" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str)


def _cached(key: str) -> httpx.Response | None:
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        if time.monotonic() - hit[0] > CACHE_TTL:
            del _cache[key]
            return None
        _cache.move_to_end(key)
        stats["cache_hits"] += 1
        return hit[1]


def _store(key: str, r: httpx.Response) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), r)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def forget(url: str, params: dict | None = None) -> None:
    """覚えた応答を捨てる（中身が使えなかった応答を、次も使わないように）。"""
    with _cache_lock:
        _cache.pop(_key(url, params), None)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def retry_after(r: httpx.Response) -> float | None:
    """Retry-After（秒数、または日時）を秒数にする。なければ None。"""
    value = (r.headers.get("retry-after") or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _space(host: str, interval: float) -> None:
    """同じサービスへの呼び出しの間隔を interval 秒以上あける。"""
    with _hosts_lock:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        wait = _last_call.get(host, 0.0) + interval - time.monotonic()
        if wait > 0:
            sleep(wait)
        _last_call[host] = time.monotonic()


def get(url: str, *, params: dict | None = None, headers: dict | None = None, timeout: float = 30.0,
        follow_redirects: bool = True, cache: bool = True, min_interval: float = 0.0) -> httpx.Response:
    """GET。利用制限・一時的な失敗はやり直し、成功した応答は覚えておく。

    やり直しても失敗した応答（429 など）はそのまま返す（呼び出し側が理由を記録する）。
    接続できない状態がやり直しても続けば、その例外を投げる。
    """
    key = _key(url, params)
    if cache and (hit := _cached(key)) is not None:
        return hit
    host = urlparse(url).hostname or ""
    r: httpx.Response | None = None
    for attempt in range(MAX_RETRIES + 1):
        if min_interval:
            _space(host, min_interval)
        try:
            r = httpx.get(url, params=params, headers=headers or HEADERS, timeout=timeout,
                          follow_redirects=follow_redirects)
        except (httpx.ConnectError, httpx.RemoteProtocolError):
            # 待ち時間切れ（Timeout）はやり直さない。同じだけ待つことになり、調査の時間を使うだけ
            if attempt == MAX_RETRIES:
                raise
            stats["retries"] += 1
            sleep(BASE_WAIT * 2 ** attempt)
            continue
        if r.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            if r.status_code == 429:
                stats["rate_limited"] += 1
            wait = retry_after(r)
            wait = BASE_WAIT * 2 ** attempt if wait is None else wait
            if wait > MAX_WAIT:
                return r
            stats["retries"] += 1
            sleep(wait)
            continue
        break
    assert r is not None
    if cache and r.status_code == 200 and len(r.content) <= CACHE_MAX_BYTES:
        _store(key, r)
    return r
