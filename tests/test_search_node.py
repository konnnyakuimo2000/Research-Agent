from unittest.mock import patch

from research_agent.agent.nodes import Nodes
from research_agent.config import Settings
from research_agent.tools.searxng import WebResult, WebSearchResponse
from research_agent.tools.wikimedia import Entity

SETTINGS = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")
PLANET = Entity("Q308", "水星", "太陽系第一惑星", 274)
WEB = WebSearchResponse(results=[
    WebResult("水星とは", "https://example.com/mercury", "水星は…", ["brave"]),
    WebResult("水星の温度", "https://example.com/temp", "昼は…", ["brave"]),
])


class FakeLLM:
    def chat_json(self, system, user, schema, temperature=0.2):
        assert "https://example.com" not in user      # URL は LLM に渡さない（W1 などの参照で渡す）
        return {
            "known": [
                {"text": "水星は最も内側の惑星", "source": "W1"},
                {"text": "Wikidata の説明", "source": "Q308"},
                {"text": "存在しない参照", "source": "W9"},
            ],
            "unknown": [],
            "candidate_ids": ["Q308"],
            "target_id": "Q308",
        }


def test_search_maps_short_refs_back_to_urls():
    state = {"request": "太陽系の惑星の水星について調べて",
             "decision": {"action": "search", "query": "水星", "names": ["水星"]}}
    with patch("research_agent.agent.nodes.wikimedia.search_entities", return_value=[PLANET]), \
         patch("research_agent.agent.nodes.wikimedia.get_entities", return_value={"Q308": PLANET}), \
         patch("research_agent.agent.nodes.wikimedia.summary_for_entity", return_value=None), \
         patch("research_agent.agent.nodes.searxng.web_search", return_value=WEB):
        out = Nodes(SETTINGS, FakeLLM()).search(state)
    assert [k["source"] for k in out["known"]] == [
        "https://example.com/mercury", "https://www.wikidata.org/wiki/Q308", ""]
    assert out["target"]["id"] == "Q308"
    # 対象が決まっているので、検索結果を深掘りするページの候補にする
    assert out["reading_list"] == ["https://example.com/mercury", "https://example.com/temp"]


def test_search_failure_is_recorded_for_the_report():
    import httpx
    state = {"request": "太陽系の惑星の水星について調べて",
             "decision": {"action": "search", "query": "水星", "names": ["水星"]}}
    with patch("research_agent.agent.nodes.wikimedia.search_entities", return_value=[PLANET]), \
         patch("research_agent.agent.nodes.wikimedia.get_entities", return_value={"Q308": PLANET}), \
         patch("research_agent.agent.nodes.wikimedia.summary_for_entity", return_value=None), \
         patch("research_agent.agent.nodes.searxng.web_search", side_effect=httpx.ConnectError("refused")):
        out = Nodes(SETTINGS, FakeLLM()).search(state)
    assert out["failures"] == ["Web 検索（SearXNG）に接続できなかった（ConnectError）"]
    assert out["web_failures_in_row"] == 1


def test_respond_reports_failures():
    class TextLLM:
        def chat_text(self, system, user, temperature=0.5):
            return "本文"
    out = Nodes(SETTINGS, TextLLM()).respond({"request": "水星", "failures": ["Web 検索（SearXNG）に接続できなかった"],
                                              "unresponsive": ["brave", "duckduckgo"], "databases_used": ["ORCID"]})
    # 検索が使えなかったことは 1 文で知らせる。道具の名前（SearXNG・検索エンジン・データベース）は書かない
    assert "※ 検索の一部が使えなかったため、調べきれていない可能性があります。" in out["answer"]
    for word in ("SearXNG", "brave", "duckduckgo", "ORCID", "Tier"):
        assert word not in out["answer"]
    # 画像の候補集めなど、検索以外の失敗は報告に書かない（実行の記録に残る）
    quiet = Nodes(SETTINGS, TextLLM()).respond({"request": "水星", "failures": ["画像の候補集め（L2-Wikidata）に失敗した（ConnectError）"]})
    assert "※" not in quiet["answer"] and "L2-Wikidata" not in quiet["answer"]


def test_exact_name_matches_are_added_to_candidates():
    # LLM が「マーキュリー」表記の項目だけを選んでも、英語名 Mercury で一致した水星などは候補に入れる
    ford = Entity("Q613883", "マーキュリー", "自動車ブランド", 60, matched="Mercury")
    planet = Entity("Q308", "水星", "太陽系第一惑星", 274, matched="Mercury")
    prefix = Entity("Q165745", "マーキュリー・レコード", "レーベル", 40, matched="Mercury Records")

    class LLMPicksOnlyFord:
        def chat_json(self, system, user, schema, temperature=0.2):
            return {"known": [], "unknown": [], "candidate_ids": ["Q613883"], "target_id": "", "target_evidence": ""}

    state = {"request": "マーキュリーについて調べて",
             "decision": {"action": "search", "query": "マーキュリー", "names": ["マーキュリー", "Mercury"]}}
    info = {e.id: e for e in (ford, planet, prefix)}
    with patch("research_agent.agent.nodes.wikimedia.search_entities", return_value=[ford, planet, prefix]), \
         patch("research_agent.agent.nodes.wikimedia.get_entities",
               side_effect=lambda ids: {i: info[i] for i in ids}), \
         patch("research_agent.agent.nodes.searxng.web_search", return_value=WEB):
        out = Nodes(SETTINGS, LLMPicksOnlyFord()).search(state)
    assert [c["id"] for c in out["candidates"]] == ["Q308", "Q613883"]     # 有名な順。前方一致だけのものは入れない


def test_zero_results_with_unresponsive_engines_count_as_failure():
    state = {"request": "太陽系の惑星の水星について調べて",
             "decision": {"action": "search", "query": "水星", "names": ["水星"]}}
    blocked = WebSearchResponse(results=[], unresponsive=["brave", "duckduckgo"])
    with patch("research_agent.agent.nodes.wikimedia.search_entities", return_value=[PLANET]),          patch("research_agent.agent.nodes.wikimedia.get_entities", return_value={"Q308": PLANET}),          patch("research_agent.agent.nodes.wikimedia.summary_for_entity", return_value=None),          patch("research_agent.agent.nodes.searxng.web_search", return_value=blocked):
        out = Nodes(SETTINGS, FakeLLM()).search(state)
    assert out["web_failures_in_row"] == 1
    assert "結果が 0 件" in out["failures"][0] and "brave, duckduckgo" in out["failures"][0]
