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
