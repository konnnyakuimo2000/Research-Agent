"""テスト共通の設定。外部サービスを呼ぶ処理は、テストで明示しない限り空の結果にする（本物のネットワークに出ない）。"""

from unittest.mock import patch

import pytest

from my_llm_app.tools import http


@pytest.fixture(autouse=True)
def no_external_lookups():
    http.clear_cache()
    with patch("my_llm_app.agent.nodes.databases.lookup", return_value=([], [], [])), \
         patch("my_llm_app.agent.nodes.wikimedia.official_sites", return_value=[]), \
         patch("my_llm_app.agent.nodes.images.related_images", return_value=[]):
        yield
    http.clear_cache()


@pytest.fixture(autouse=True)
def no_configured_internal_hosts(monkeypatch):
    """.env の LLM・SearXNG を、公開取得で拒む内部のサービスとして扱わない（偽の名前引きと混ざると、
    テストの順番によって結果が変わるため）。内部の扱いを確かめるテストは自分で設定する。"""
    monkeypatch.setattr(http, "INTERNAL_HOSTS", set())
    monkeypatch.setattr(http, "_internal_ips", {})
