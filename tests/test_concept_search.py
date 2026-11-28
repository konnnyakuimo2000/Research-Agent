"""概念の言葉の調査（2026-11-28「エージェントメモリについて」）。

関係のあるページを見つけたのに 1 枚も読まず、画像だけの報告になった:
1. 題名が「AI エージェント**の**メモリ」で、続け書きの「エージェントメモリ」と一致しなかった
2. 百科事典（Wikidata）に同じ項目がなく、利用者が「一般的な概念」と答えても対象が決まらなかった
3. 事実が 0 件なのに画像を探して載せた
4. 対象を含むページが 1 件もない検索の後、別の表記で探し直さなかった
"""

from __future__ import annotations

from types import SimpleNamespace

from research_agent.agent.names import mentions_parts
from research_agent.agent.page_choice import relevant_results

NAMES = ["エージェントメモリ", "Agent Memory"]


def web(title, url="https://example.org/x", snippet=""):
    return SimpleNamespace(title=title, url=url, snippet=snippet)


def test_compound_concepts_match_when_their_parts_appear():
    assert mentions_parts("AI エージェントのメモリ管理入門", NAMES)
    assert mentions_parts("AI agent memory explained", NAMES)
    assert not mentions_parts("メモリ増設ガイド", NAMES) and not mentions_parts("OpenAI | 研究と実用化", NAMES)
    assert not mentions_parts("EE-30 の説明", ["EE-3"])                 # 型番は分けない
    results = [web("AI エージェントのメモリ管理入門", "https://a.example/1"), web("OpenAI | 研究と実用化", "https://b.example/2")]
    assert relevant_results(results, NAMES) == []                       # 人や物の名前と同じ、文字どおりの一致
    assert [w.url for w in relevant_results(results, NAMES, loose=True)] == ["https://a.example/1"]


# ---- 2. 百科事典に同じ項目がない概念は、依頼の言葉を対象にする ----

from unittest.mock import patch  # noqa: E402

from research_agent.agent import nodes as nodes_mod  # noqa: E402
from research_agent.agent.nodes import Nodes  # noqa: E402
from research_agent.config import Settings  # noqa: E402
from research_agent.tools import searxng, wikimedia  # noqa: E402

PRODUCT = wikimedia.Entity("Q140639670", "AIエージェントメモリソフトウェア", "software that stores persistent memory for AI agents", 0, "エージェントメモリ")
PLANET = wikimedia.Entity("Q308", "水星", "太陽系の惑星", 200, "マーキュリー")
SINGER = wikimedia.Entity("Q15869", "フレディ・マーキュリー", "イギリスの歌手", 150, "マーキュリー")
PAGES = [searxng.WebResult("AI エージェントのメモリ管理入門", "https://a.example/memory", "短期記憶と長期記憶", ["bing"]),
         searxng.WebResult("OpenAI | 研究と実用化", "https://openai.com/ja-JP/", "", ["bing"])]


class ChooseNothingLLM:
    """検索の結果の整理: 候補は挙げるが、対象は選ばない（2026-11-28 の LLM と同じ）。"""

    def __init__(self, ids):
        self.ids = ids

    def chat_json(self, system, user, schema, **kw):
        return {"known": [], "unknown": [], "candidate_ids": self.ids, "target_id": "", "target_evidence": "",
                "specific_subject": "", "specific_evidence": ""}


def search(state, entities, names):
    n = Nodes(Settings(llm_base_url="http://llm.test/v1", llm_model="m", llm_api_key="k", searxng_url="http://s.test"),
              ChooseNothingLLM([e.id for e in entities]))
    state = {"candidates": [], "searched": [], "pages": {}, "tier1_searched": True, **state,
             "decision": {"action": "search", "query": f"{names[0]} 仕組み", "names": names}}
    with patch.object(nodes_mod.wikimedia, "search_entities", return_value=entities), \
            patch.object(nodes_mod.wikimedia, "get_entities", side_effect=lambda ids: {e.id: e for e in entities if e.id in ids}), \
            patch.object(nodes_mod.wikimedia, "summary_for_entity", return_value=None), \
            patch.object(nodes_mod.wikimedia, "official_sites", return_value=[]), \
            patch.object(nodes_mod.searxng, "web_search", return_value=searxng.WebSearchResponse(results=PAGES)):
        return n.search(state)


def test_a_concept_without_its_own_entry_becomes_the_subject_and_its_pages_are_read():
    out = search({"request": "エージェントメモリについて"}, [PRODUCT], NAMES)
    assert out["target"]["label"] == "エージェントメモリ" and out["target"]["id"] == ""
    assert any("依頼の言葉そのものを対象として調べた" in a for a in out["assumptions"])
    assert out["reading_list"] == ["https://a.example/memory"]                    # 「AI エージェントのメモリ」を読む


def test_a_general_answer_settles_the_subject_but_competing_entries_are_still_asked():
    asked = search({"request": "マーキュリーについて"}, [PLANET, SINGER], ["マーキュリー", "Mercury"])
    assert asked["target"] is None                                                 # 水星か歌手か: 聞き返す
    general = search({"request": "マーキュリーについて", "clarifications": ["一般的な概念"]}, [PLANET, SINGER], ["マーキュリー"])
    assert general["target"]["label"] == "マーキュリー" and "一般的な概念" in general["target"]["description"]


# ---- 3. 事実が 0 件なら画像を探さず、試したことと見つからなかった理由をプログラムが書く ----

from research_agent.agent.guard import check_decision  # noqa: E402


class NoReportLLM:
    def chat_text(self, *a, **k):
        raise AssertionError("事実がないのに LLM に報告を書かせた")


def test_no_facts_means_no_image_search_and_a_plain_not_found_report():
    state = {"request": "エージェントメモリについて", "target": {"id": "", "label": "エージェントメモリ", "description": "", "sitelinks": 0},
             "wants_images": True, "settings": {}, "steps": 5, "searched": ["エージェントメモリ 定義", "AI agent memory"],
             "pages_read": [], "reading_list": [], "verified": [], "coverage": [], "aspects": [], "candidates": []}
    assert check_decision(state, {"action": "respond"})["action"] == "respond"              # 画像を探さない
    asks = {**state, "request": "エージェントメモリの画像", "verified": []}
    assert check_decision(asks, {"action": "respond"})["action"] == "find_images"          # 画像・写真を頼まれたら探す
    n = Nodes(Settings(llm_base_url="http://llm.test/v1", llm_model="m", llm_api_key="k", searxng_url="http://s.test"), NoReportLLM())
    with patch.object(nodes_mod.telemetry, "emitter", return_value=lambda *a, **k: None):
        out = n.respond(state)
    answer = out["answer"]
    assert answer.startswith("「エージェントメモリ」について、出典で確かめられた事実は見つかりませんでした。")
    assert "- 検索: AI agent memory" in answer and "対象の名前を含むページがなかった" in answer
    assert "### 画像" not in answer


# ---- 4. 対象の名前を含むページが 1 件もなければ、別の表記で 1 回だけ探し直す ----

def test_no_relevant_page_means_one_retry_with_another_spelling():
    state = {"request": "エージェントメモリについて", "target": {"id": "", "label": "エージェントメモリ", "description": "", "sitelinks": 0},
             "settings": {}, "steps": 5, "searched": ["エージェントメモリ 定義"], "pages_read": [], "reading_list": [],
             "verified": [], "coverage": [], "aspects": [], "candidates": [], "no_relevant_names": NAMES}
    d = check_decision(state, {"action": "respond"})
    assert (d["action"], d["query"], d.get("retry")) == ("search", '"Agent Memory"', True)   # 英語の表記を先に
    once = {**state, "searched": state["searched"] + ['"Agent Memory"'], "retry_searched": ['"Agent Memory"']}
    assert check_decision(once, {"action": "respond"})["action"] == "respond"                  # 1 回だけ
    found = {**state, "no_relevant_names": []}
    assert check_decision(found, {"action": "respond"})["action"] == "respond"                 # 当たりがあれば打たない


def test_the_search_node_remembers_when_nothing_relevant_came_back():
    out = search({"request": "エージェントメモリについて"}, [PRODUCT], ["量子もつれ"])
    assert out["no_relevant_names"] == ["量子もつれ"]
    hit = search({"request": "エージェントメモリについて"}, [PRODUCT], NAMES)
    assert hit["no_relevant_names"] == []
