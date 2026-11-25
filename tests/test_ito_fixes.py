"""2026-11-25「青学の伊藤先生について」の調査の直し。

1. 人を指す依頼（敬称・職名の付いた名前）で、人でない Wikidata の項目（小惑星「伊藤」・姓・地名）を候補にも
   対象にもしない。前は小惑星「伊藤」（Wikipedia の記事 24 件）が、記事の多さで対象に選ばれ、小惑星の観点
   （軌道要素・発見）で調べ、本人の教員ページを「別の対象」として捨てていた
2. 答えが対象の名前を含むだけ（対象「伊藤」に「伊藤雄一」）なら、同じ対象とみなさず、対象を決め直す。
   前は「伊藤雄一」と答えても、対象は小惑星のままだった
"""

from __future__ import annotations

import json
from unittest.mock import patch

from langgraph.types import Command

from research_agent.agent import nodes as nodes_mod
from research_agent.agent.nodes import Nodes, names_target
from research_agent.config import Settings
from research_agent.tools import searxng, wikimedia

ASTEROID = wikimedia.Entity("Q979880", "伊藤", "小惑星", 24, "伊藤")
SURNAME = wikimedia.Entity("Q6500676", "伊藤", "日本語の姓 (いとう)", 3, "伊藤")
PLACE = wikimedia.Entity("Q80174474", "伊藤", "愛知県西尾市の地名", 0, "伊藤")
PAINTER = wikimedia.Entity("Q11545199", "歌川芳邨", "江戸時代末期から明治時代にかけての浮世絵師", 2, "伊藤")
WEB = [searxng.WebResult("研究者情報 - 伊藤 雄一 - 青山学院大学", "https://raweb1.jm.aoyama.ac.jp/aguhp/KgApp/k03/resid/S000872",
                         "伊藤 雄一 理工学部 情報テクノロジー学科 教授", ["bing"]),
       searxng.WebResult("研究者情報 - 伊藤 晴祥 - 青山学院大学", "https://raweb1.jm.aoyama.ac.jp/aguhp/KgApp?kyoinId=ymbygeyiggy", "", ["bing"])]


class FakeLLM:
    """検索の結果の整理: 小惑星を対象に選ぶ（前の調査で LLM が返したのと同じ）。"""

    def chat_json(self, system, user, schema, **kw):
        if "candidate_ids" in json.dumps(schema, ensure_ascii=False):
            return {"known": [], "unknown": [], "candidate_ids": ["Q979880", "Q6500676", "Q80174474", "Q11545199"],
                    "target_id": "Q979880", "target_evidence": "", "specific_subject": "", "specific_evidence": ""}
        return {}


def run_search(state):
    n = Nodes(Settings(llm_base_url="http://llm.test/v1", llm_model="m", llm_api_key="k", searxng_url="http://s.test"), FakeLLM())
    with patch.object(nodes_mod.wikimedia, "search_entities", return_value=[ASTEROID, SURNAME, PLACE, PAINTER]), \
            patch.object(nodes_mod.wikimedia, "get_entities",
                         side_effect=lambda ids: {e.id: e for e in (ASTEROID, SURNAME, PLACE, PAINTER) if e.id in ids}), \
            patch.object(nodes_mod.wikimedia, "humans", side_effect=lambda ids: {i for i in ids if i == "Q11545199"}), \
            patch.object(nodes_mod.searxng, "web_search", return_value=searxng.WebSearchResponse(results=WEB)):
        return n.search(state)


def test_a_person_request_does_not_pick_an_asteroid():
    state = {"request": "青学の伊藤先生について", "decision": {"action": "search", "query": "青山学院大学 伊藤 先生 教授"},
             "candidates": [], "searched": [], "pages": {}, "tier1_searched": True}
    out = run_search(state)
    assert out["target"] is None or out["target"]["id"] != "Q979880"          # 小惑星を対象にしない
    assert {c["id"] for c in out["candidates"]} <= {"Q11545199"}              # 人の項目だけが候補に残る
    assert any("non-human entities dropped" in e for e in out["trace"][0]["errors"])


def test_an_answer_that_only_contains_the_target_name_resets_the_target():
    asteroid = {"id": "Q979880", "label": "伊藤", "description": "小惑星", "sitelinks": 24}
    assert names_target("伊藤", asteroid) and names_target("小惑星", asteroid)
    assert not names_target("伊藤雄一", asteroid)                              # もっと絞った名前は、別の対象
    n = Nodes(Settings(llm_base_url="http://llm.test/v1", llm_model="m", llm_api_key="k", searxng_url="http://s.test"),
              type("L", (), {"chat_json": lambda self, *a, **k: {"matched_ids": []}})())
    state = {"request": "青学の伊藤先生について", "target": asteroid, "candidates": [asteroid],
             "decision": {"action": "ask_user", "question": "どの伊藤先生ですか？"}, "verified": [{"text": "x"}],
             "questions_asked": 0}
    with patch.object(nodes_mod, "interrupt", return_value="伊藤雄一"):
        out = n.ask_user(state)
    assert out["target"] is None and out["verified"] == [] and out["clarifications"] == ["伊藤雄一"]
    assert Command  # noqa: B018 - 再開の形（Command）は LangGraph が扱う
