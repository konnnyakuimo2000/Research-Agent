from unittest.mock import patch

from my_llm_app.agent.nodes import Nodes
from my_llm_app.config import Settings
from my_llm_app.tools.searxng import WebResult, WebSearchResponse
from my_llm_app.tools.wikimedia import Entity

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
    with patch("my_llm_app.agent.nodes.wikimedia.search_entities", return_value=[PLANET]), \
         patch("my_llm_app.agent.nodes.wikimedia.get_entities", return_value={"Q308": PLANET}), \
         patch("my_llm_app.agent.nodes.wikimedia.summary_for_entity", return_value=None), \
         patch("my_llm_app.agent.nodes.searxng.web_search", return_value=WEB):
        out = Nodes(SETTINGS, FakeLLM()).search(state)
    assert [k["source"] for k in out["known"]] == [
        "https://example.com/mercury", "https://www.wikidata.org/wiki/Q308", ""]
    assert out["target"]["id"] == "Q308"
    # 対象が決まっているので、検索結果を深掘りするページの候補にする
    assert out["reading_list"] == ["https://example.com/mercury", "https://example.com/temp"]
