"""テスト用: http.py の取得を模擬トランスポートに差し替える（外部のネットワークにはつながない）。"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from research_agent.tools import http

PUBLIC_IP = "93.184.216.34"


def install(monkeypatch, handler: Callable[[httpx.Request], httpx.Response], ips: dict[str, str] | None = None) -> list:
    """public / internal の取得を handler に向ける。ips は名前 → IP（書いていない名前は公開の IP にする）。

    戻り値は、実際に送られた要求の一覧（数を確かめるため）。
    """
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    monkeypatch.setattr(http, "public_client", httpx.Client(transport=httpx.MockTransport(record), follow_redirects=False))
    monkeypatch.setattr(http, "internal_client", httpx.Client(transport=httpx.MockTransport(record), follow_redirects=True))
    monkeypatch.setattr(http, "resolve", lambda host, port: [(ips or {}).get(host, PUBLIC_IP)])
    monkeypatch.setattr(http, "sleep", lambda s: None)
    http.clear_cache()
    return sent
