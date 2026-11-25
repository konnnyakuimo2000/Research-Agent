"""テスト共通の設定。外部サービスを呼ぶ処理は、テストで明示しない限り空の結果にする（本物のネットワークに出ない）。"""

from unittest.mock import patch

import pytest

from research_agent.tools import http


@pytest.fixture(autouse=True)
def no_external_lookups():
    http.clear_cache()
    with patch("research_agent.agent.nodes.databases.lookup", return_value=([], [], [])), \
         patch("research_agent.agent.nodes.wikimedia.official_sites", return_value=[]), \
         patch("research_agent.agent.nodes.images.related_images", return_value=[]), \
         patch("research_agent.agent.nodes.images.commons_license", return_value=""), \
         patch("research_agent.agent.nodes.wikimedia.summary_for_entity", return_value=None), \
         patch("research_agent.agent.nodes.wikimedia.humans", side_effect=lambda ids: set(ids)):
        # 人かどうか（P31 = Q5）は、明示しない限りすべて人として扱う（人でない項目を外す動きは、そのテストで決める）
        yield
    http.clear_cache()


@pytest.fixture(autouse=True)
def no_configured_internal_hosts(monkeypatch):
    """.env の LLM・SearXNG を、公開取得で拒む内部のサービスとして扱わない（偽の名前引きと混ざると、
    テストの順番によって結果が変わるため）。内部の扱いを確かめるテストは自分で設定する。"""
    monkeypatch.setattr(http, "INTERNAL_HOSTS", set())
    monkeypatch.setattr(http, "_internal_ips", {})


_LOCAL = {"localhost", "127.0.0.1", "::1", "testserver"}


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """テストは本物のネットワークに出ない。外の名前を引こうとしたら、その場で失敗させる。

    前は、つながらない宛先（http://llm.test）の名前引きとやり直しで、1 つのテストが 1 分近くかかり、
    ネットワークの具合によっては全体が止まったように見えていた（2026-11-24）。
    処理の中で失敗を握りつぶしても見逃さないよう、引こうとした名前を覚えておき、テストの終わりに失敗にする。"""
    import socket
    real = socket.getaddrinfo
    attempts: list[str] = []

    def guarded(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host)
        if name not in _LOCAL:
            attempts.append(name)
            raise OSError(f"テストから外の名前を引こうとした: {name}")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)
    yield
    assert not attempts, f"テストが本物のネットワークに出ようとした: {sorted(set(attempts))}"
