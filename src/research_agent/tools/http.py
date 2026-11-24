"""外部サービスへの GET をまとめる（設計書 6.6 G-4, G-5、実装計画フェーズ 6「呼び出しの堅牢化」）。

- 利用制限（429）・一時的な失敗（5xx・接続できない）は、間隔をあけて最大 MAX_RETRIES 回やり直す。
  Retry-After が MAX_WAIT 秒より長いときは待たずに、その応答を返す（調査の時間を使い切らないように）
- 同じ URL・引数への GET は CACHE_TTL 秒のあいだ覚えておき、取りに行かない（同じ調査・続けての調査で同じページを読む）
- 同じサービスへの間隔（min_interval）を守る。SearXNG の後ろの検索エンジンは、続けて検索すると止められる（G-4）
- User-Agent はこのアプリのもの（Wikimedia などの API の決まり）。なりすましはしない

取得の経路は 2 つに分ける（レビュー 1）。
- 公開取得（public=True。既定）: 検索結果や HTML から得た URL のページ・画像・公開 API。HTTP(S) だけ。
  転送は自分で 1 つずつたどり、そのたびに宛先を確かめる。実際に TCP でつなぐときにも、つなぐ IP が公開アドレス
  （ipaddress の is_global）であることを確かめ、確かめたその IP につなぐ（DNS の答えを途中ですり替えられても、
  内部のサービスにはつながらない）。環境変数のプロキシ設定は使わない（trust_env=False）。
- 内部取得（public=False）: 設定済みの SearXNG・LLM サーバーなど。宛先の制限をしない。
  逆に、設定済みの SearXNG・LLM サーバー・モデルの窓口（.env の SEARXNG_URL・VL_BASE_URL・MODEL_GATEWAY_URL）の名前と IP アドレスは、
  公開取得では宛先として拒む。研究室の LLM サーバーは大学の公開アドレス上にあるので、公開アドレスかどうか
  だけでは防げない（検索結果のふりをした URL から LLM サーバーに取りに行けてしまう）。
受け取る大きさの上限（max_bytes）は、受け取りながら数え、超えたら残りを読まずにやめる（レビュー 2）。
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
from collections import OrderedDict
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

from research_agent.config import USER_AGENT

HEADERS = {"User-Agent": USER_AGENT}
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 2
BASE_WAIT = 1.5          # やり直しの前に待つ秒数（1 回ごとに 2 倍）
MAX_WAIT = 10.0          # これより長く待てと言われたら、やり直さない
CACHE_TTL = 3600.0
CACHE_MAX = 256
CACHE_MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
_REDIRECT_STATUS = {301, 302, 303, 307, 308}
# 本文から取り除くヘッダー（受け取った後の本文は、展開済みでもう分割されていない）
_DROP_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}

sleep = time.sleep       # テストで差し替える

_cache: OrderedDict[str, tuple[float, httpx.Response]] = OrderedDict()
_cache_lock = threading.Lock()
_last_call: dict[str, float] = {}
_host_locks: dict[str, threading.Lock] = {}
_hosts_lock = threading.Lock()
stats = {"retries": 0, "rate_limited": 0, "cache_hits": 0}


class BlockedDestination(httpx.TransportError):
    """公開されていない宛先（内部のアドレス・HTTP(S) 以外）なので取りに行かない。"""


class ResponseTooLarge(httpx.HTTPError):
    """応答が上限（max_bytes）を超えたので、残りを読まずにやめた。"""


# ---- 宛先の確かめ ----

def _configured_hosts() -> set[str]:
    hosts = set()
    for name in ("VL_BASE_URL", "SEARXNG_URL", "MODEL_GATEWAY_URL"):
        host = urlparse(os.getenv(name, "")).hostname
        if host:
            hosts.add(host.lower())
    return hosts


INTERNAL_HOSTS: set[str] = _configured_hosts()   # 公開取得では拒む、設定済みの内部サービス（テストで差し替える）
_internal_ips: dict[str, set[str]] = {}


def _internal_addresses() -> set[str]:
    """設定済みの内部サービスの IP アドレス（引けたものだけ。名前ごとに覚えておく）。"""
    out: set[str] = set()
    for host in INTERNAL_HOSTS:
        if host not in _internal_ips:
            try:
                _internal_ips[host] = set(resolve(host, 443))
            except OSError:
                _internal_ips[host] = set()
        out |= _internal_ips[host]
    return out


def is_public_ip(ip: str) -> bool:
    """公開アドレスか（プライベート・ループバック・リンクローカル・共有・予約済み・マルチキャストは公開ではない）。"""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def resolve(host: str, port: int) -> list[str]:
    """名前を IP アドレスに引く（テストで差し替える）。"""
    return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))


def public_addresses(host: str, port: int) -> list[str]:
    """host の IP アドレス。1 つでも公開でないものがあれば BlockedDestination（混ぜて返す名前も信じない）。"""
    try:
        addrs = resolve(host, port)
    except OSError as e:
        raise httpx.ConnectError(f"名前を引けない: {host}") from e
    if not addrs or not all(is_public_ip(a) for a in addrs):
        raise BlockedDestination(f"公開されていない宛先: {host} → {', '.join(addrs) or '（なし）'}")
    if host.lower() in INTERNAL_HOSTS or set(addrs) & _internal_addresses():
        raise BlockedDestination(f"設定済みの内部サービス（LLM・SearXNG）: {host}")
    return addrs


def check_url(url: str) -> None:
    """公開取得してよい URL か（HTTP(S) で、名前が公開アドレスだけを指す）。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise BlockedDestination(f"HTTP(S) ではない宛先: {url[:80]}")
    public_addresses(u.hostname, u.port or (443 if u.scheme == "https" else 80))


class PublicOnlyBackend(httpcore.SyncBackend):
    """TCP でつなぐ瞬間に、つなぐ IP が公開アドレスかを確かめ、確かめたその IP につなぐ。"""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addrs = public_addresses(host, port)
        last: Exception | None = None
        for addr in addrs:
            try:
                return super().connect_tcp(addr, port, timeout, local_address, socket_options)
            except httpcore.ConnectError as e:
                last = e
        raise last or httpcore.ConnectError(f"接続できない: {host}")


def _public_client() -> httpx.Client:
    transport = httpx.HTTPTransport(retries=0)
    transport._pool._network_backend = PublicOnlyBackend()   # httpx に差し替えの口がないため、接続の部品を入れ替える
    return httpx.Client(transport=transport, trust_env=False, follow_redirects=False)


public_client = _public_client()            # テストで模擬トランスポートの Client に差し替える
internal_client = httpx.Client(follow_redirects=True)


# ---- 覚えておく応答 ----

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
        _cache.pop(_key(url, params) + " internal", None)   # 内部取得（SearXNG など）で覚えた応答も


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


# ---- 取得 ----

def _receive(client: httpx.Client, url: str, params: dict | None, headers: dict, timeout: float,
             max_bytes: int | None) -> httpx.Response:
    """1 回の GET。本文は受け取りながら数え、max_bytes を超えたら残りを読まずにやめる。"""
    with client.stream("GET", url, params=params, headers=headers, timeout=timeout) as r:
        if r.status_code in _REDIRECT_STATUS and r.headers.get("location"):
            body = b""                                     # 転送の応答の本文は読まない
        else:
            chunks, total = [], 0
            for chunk in r.iter_bytes():                   # 展開した後の大きさで数える
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ResponseTooLarge(f"{max_bytes:,} バイトを超えた: {url[:80]}")
                chunks.append(chunk)
            body = b"".join(chunks)
        # 見出しは受け取ったバイトのまま渡す。文字に直した形で渡すと、httpx が ASCII で書き戻そうとして、
        # 日本語の入った見出し（ファイル名の Content-Disposition など）で UnicodeEncodeError になり、
        # 調査ごと止まっていた（2026-11-24 の「はやぶさ」の調査で 2 回）
        out_headers = [(k, v) for k, v in r.headers.raw if k.decode("latin-1").lower() not in _DROP_HEADERS]
        return httpx.Response(r.status_code, headers=out_headers, content=body, request=r.request)


def _get_once(url: str, params: dict | None, headers: dict, timeout: float, public: bool,
              max_bytes: int | None) -> httpx.Response:
    if not public:
        return _receive(internal_client, url, params, headers, timeout, max_bytes)
    # 公開取得: 転送は 1 つずつたどり、そのたびに宛先を確かめる（つなぐときにも PublicOnlyBackend が確かめる）
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url)
        r = _receive(public_client, url, params, headers, timeout, max_bytes)
        if r.status_code not in _REDIRECT_STATUS or not r.headers.get("location"):
            return r
        url, params = urljoin(str(r.request.url), r.headers["location"]), None
    raise httpx.TooManyRedirects(f"転送が {MAX_REDIRECTS} 回を超えた", request=r.request)


def get(url: str, *, params: dict | None = None, headers: dict | None = None, timeout: float = 30.0,
        cache: bool = True, min_interval: float = 0.0, public: bool = True,
        max_bytes: int | None = None) -> httpx.Response:
    """GET。利用制限・一時的な失敗はやり直し、成功した応答は覚えておく。

    public=True（既定）は公開取得（宛先を確かめる）。設定済みの SearXNG・LLM には public=False を使う。
    max_bytes を超える応答は ResponseTooLarge（途中までの応答は覚えない。覚えた応答にも上限を当てる）。
    応答の url は、転送をたどった後の最終の URL。
    やり直しても失敗した応答（429 など）はそのまま返す。接続できない状態が続けば、その例外を投げる。
    """
    key = _key(url, params) + ("" if public else " internal")
    if cache and (hit := _cached(key)) is not None:
        if max_bytes is not None and len(hit.content) > max_bytes:
            raise ResponseTooLarge(f"{max_bytes:,} バイトを超えた: {url[:80]}")
        return hit
    host = urlparse(url).hostname or ""
    r: httpx.Response | None = None
    for attempt in range(MAX_RETRIES + 1):
        if min_interval:
            _space(host, min_interval)
        try:
            r = _get_once(url, params, headers or HEADERS, timeout, public, max_bytes)
        except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
            # 待ち時間切れ（Timeout）と、公開でない宛先はやり直さない
            if attempt == MAX_RETRIES or isinstance(e, BlockedDestination):
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
