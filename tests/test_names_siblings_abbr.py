"""前からの持ち越し: 「はやぶさ」の調査で「はやぶさ2」のページを読む件と、略称（九大 = 九州大学）を照合しない件。"""

from __future__ import annotations

from types import SimpleNamespace

from research_agent.agent import query_plan
from research_agent.agent.names import abbreviations_in, expand_abbreviation, sibling_only
from research_agent.agent.page_choice import keep_for_target, relevant_results


def web(title, url):
    return SimpleNamespace(title=title, url=url, snippet="")


def test_a_numbered_sibling_is_not_the_subject():
    assert sibling_only("はやぶさ2 - Wikipedia", "はやぶさ")
    assert sibling_only("小惑星探査機「はやぶさ2号」の成果", "はやぶさ")
    assert not sibling_only("はやぶさ (探査機) - Wikipedia", "はやぶさ")
    assert not sibling_only("はやぶさとはやぶさ2の違い", "はやぶさ")            # 本体の名前もある
    assert not sibling_only("東京タワー 333m の秘密", "東京タワー")              # 空白の後の数字は別
    assert not sibling_only("はやぶさ2 - Wikipedia", "はやぶさ2")                # 対象自体が番号付き
    results = [web("はやぶさ (探査機) - Wikipedia", "https://ja.wikipedia.org/wiki/はやぶさ_(探査機)"),
               web("はやぶさ2 - Wikipedia", "https://ja.wikipedia.org/wiki/はやぶさ2")]
    assert [w.url for w in relevant_results(results, ["はやぶさ"])] == [results[0].url]
    titles = {w.url: w.title for w in results}
    assert keep_for_target([w.url for w in results], titles, ["はやぶさ"]) == [results[0].url]


def test_university_abbreviations_are_expanded():
    assert expand_abbreviation("九大") == "九州大学" and expand_abbreviation("富士山") == "富士山"
    assert abbreviations_in("九大の松田教授について") == ["九州大学"]
    assert abbreviations_in("九州大学の松田教授") == []                          # 正式な名前があれば足さない
    assert query_plan.request_person("青学の伊藤先生について") == ("伊藤", ["青学", "青山学院大学"])
    assert query_plan.request_query("九大の松田教授について") == "九州大学 松田 教授"   # 正式な名前で検索する
    assert query_plan.request_query("青学の伊藤先生について") == "青山学院大学 伊藤 教員"   # 大学なので「先生」は「教員」
