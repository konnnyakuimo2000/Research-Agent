from unittest.mock import patch

from my_llm_app.agent.nodes import Nodes
from my_llm_app.config import Settings
from my_llm_app.tiers import by_tier, tier_of
from my_llm_app.tools.searxng import WebResult, WebSearchResponse
from my_llm_app.tools.wikimedia import Entity


def test_tier_of():
    assert tier_of("https://www.bousai.go.jp/kaigirep/a.htm") == 1      # ドメインの種類（go.jp）
    assert tier_of("https://www.city.gotemba.lg.jp/museum/") == 1        # 自治体（lg.jp）
    assert tier_of("https://mio.isas.jaxa.jp/mercury/") == 1             # 一覧（jaxa.jp）のサブドメイン
    assert tier_of("https://ja.wikipedia.org/wiki/水星") == 2
    assert tier_of("https://www.pinterest.jp/pin/1") == 5
    assert tier_of("https://ja.postposmo.com/x") == 5
    assert tier_of("https://starwalk.space/ja/news") == 4                # 一覧にない → 既定値
    assert tier_of("https://notgo.jp.example.com/") == 4                 # 途中に go.jp があっても一致させない


def test_by_tier_orders_and_excludes_tier5():
    urls = ["https://blog.example.com/a", "https://ja.postposmo.com/x", "https://ja.wikipedia.org/wiki/a",
            "https://www.mlit.go.jp/b", "https://blog.example.com/a"]
    assert by_tier(urls) == ["https://www.mlit.go.jp/b", "https://ja.wikipedia.org/wiki/a",
                             "https://blog.example.com/a"]


SETTINGS = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")
PLANET = Entity("Q308", "水星", "太陽系第一惑星", 274)


def test_canonical_drops_tracking_parameters_and_session_ids():
    from my_llm_app.tiers import UrlNumbers, by_tier, canonical
    a = "https://one-scene.com/products/135?srsltid=AYCX9vLkou"
    b = "https://one-scene.com/products/135?srsltid=AYCX9vJap6&utm_source=x"
    assert canonical(a) == canonical(b) == canonical("https://one-scene.com/products/135")
    assert canonical("https://x.jp/a?id=1&utm_medium=y") == "https://x.jp/a?id=1"         # 中身を変える引数は残す
    assert canonical("https://x.jp/a?id=1") != canonical("https://x.jp/a?id=2")
    assert canonical("https://r.ac.jp/KgApp/k03/resid/S1;jsessionid=ABC") == "https://r.ac.jp/KgApp/k03/resid/S1"
    assert by_tier([a, b]) == [a]
    n = UrlNumbers(["https://one-scene.com/products/135", "https://x.jp/a"])
    assert n[b] == 1 and b in n and "https://other.jp/" not in n


class FakeLLM:
    def chat_json(self, system, user, schema, temperature=0.2):
        return {"known": [], "unknown": [], "candidate_ids": ["Q308"], "target_id": "Q308", "target_evidence": ""}


def fake_search(base_url, query, limit=8):
    if "site:go.jp" in query:
        return WebSearchResponse([WebResult("国立天文台: 水星", "https://www.nao.go.jp/mercury", "", []),
                                  # 対象の名前が題名・抜粋にない Tier 1 のページは読まない
                                  WebResult("防衛省の装備品", "https://www.mod.go.jp/equipment", "", [])])
    if "site:ac.jp" in query:
        return WebSearchResponse([WebResult("大学", "https://www.example.ac.jp/mercury", "水星の観測", [])])
    return WebSearchResponse([WebResult("ブログ: 水星の話", "https://blog.example.com/m", "", []),
                              WebResult("まとめ", "https://ja.postposmo.com/m", "", [])])


def test_search_adds_tier1_pages_and_reads_from_the_top_tier():
    state = {"request": "水星について", "decision": {"action": "search", "query": "水星", "names": ["水星"]}}
    queries = []

    def recording_search(*args, **kwargs):
        queries.append(args[1])
        return fake_search(*args, **kwargs)

    with patch("my_llm_app.agent.nodes.wikimedia.search_entities", return_value=[PLANET]), \
         patch("my_llm_app.agent.nodes.wikimedia.get_entities", return_value={"Q308": PLANET}), \
         patch("my_llm_app.agent.nodes.wikimedia.summary_for_entity", return_value=None), \
         patch("my_llm_app.agent.nodes.searxng.web_search", side_effect=recording_search):
        out = Nodes(SETTINGS, FakeLLM()).search(state)
    assert queries == ["水星", "水星 site:go.jp", "水星 site:ac.jp"]
    assert out["reading_list"] == ["https://www.nao.go.jp/mercury", "https://www.example.ac.jp/mercury",
                                   "https://blog.example.com/m"]          # Tier 1 が先、Tier 5 は入らない
    assert out["tier1_searched"] is True
