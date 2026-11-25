"""検索結果から読むページを決める関数（page_choice.choose_pages。副作用なし）。全体アーキテクチャの分析 3。
検索ノード全体の動き（ページの順・件数・イベント）は、既存の検索のテストで変わらないことを確かめている。"""

from __future__ import annotations

import copy
from types import SimpleNamespace

from research_agent.agent.page_choice import choose_pages, relevant_results


def web(url, title, snippet=""):
    return SimpleNamespace(url=url, title=title, snippet=snippet)


RESULTS = [web("https://www.tokyotower.co.jp/about/", "東京タワーについて"),
           web("https://example.com/osaka", "通天閣の高さ"),                      # 名前がない → 読まない
           web("https://ja.wikipedia.org/wiki/東京タワー?utm_source=x", "東京タワー - Wikipedia"),
           web("https://example.com/tower.pdf", "東京タワーの資料")]               # 読めない（PDF） → 入れない


def test_relevant_results_need_the_name():
    assert [w.url for w in relevant_results(RESULTS, ["東京タワー"])] == [RESULTS[0].url, RESULTS[2].url, RESULTS[3].url]
    assert len(relevant_results(RESULTS, [])) == 4


def test_pages_are_chosen_without_touching_the_state():
    state = {"pages_read": [{"url": "https://ja.wikipedia.org/wiki/東京タワー"}],      # 表記違いで既読
             "reading_list": [], "priority_urls": ["https://old.example/a"]}
    before = copy.deepcopy(state)
    found = {"summary_url": "https://ja.wikipedia.org/wiki/東京タワー_(summary)", "official_urls": ["https://www.tokyotower.co.jp/"]}
    choice = choose_pages(state, found, RESULTS, {"aspect": "高さ"}, ["東京タワー"])
    assert state == before                                                       # 副作用なし
    assert choice.urls[:2] == [found["summary_url"], "https://www.tokyotower.co.jp/"]   # 概要・公式が先
    assert "https://example.com/osaka" not in choice.urls and "https://example.com/tower.pdf" not in choice.urls
    # 観点を狙った検索: 未読の関連ページを先に読む。概要の記事は、さらにその前
    assert choice.priority[0] == found["summary_url"] and "https://www.tokyotower.co.jp/about/" in choice.priority
    assert not any("wiki/東京タワー?" in u for u in choice.priority)               # 既読（表記違い）は入れない
    assert choice.new_relevant == 2                                               # 既読の 1 つを除いた、新しい関連ページ
    plain = choose_pages({**state, "summary_url": "x"}, {}, RESULTS, {}, ["東京タワー"])
    assert plain.priority is None and plain.urls == [RESULTS[0].url, RESULTS[2].url]   # 何も狙っていなければ先に読む一覧は変えない
