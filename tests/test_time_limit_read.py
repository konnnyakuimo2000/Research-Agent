"""時間の上限で、1 ページも読まずに報告しない（2026-11-25「松田教授について」）。

時間の上限 420 秒（すばやく）から報告の分 300 秒を引くと、調べられるのは 120 秒。遅い LLM では検索 2 回で使い切り、
本人のページを 8 件見つけたのに 1 件も読まず「確認できた事実がありません」とだけ答えていた。
- 報告のために残した時間には読み込み 1 回が入っているので、まだ 1 ページも読んでいなければ、先頭を 1 回だけ読む
- 時間の上限でやめたときは、読めなかったページの数を報告の「確かめきれなかったこと」に書く
"""

from __future__ import annotations

from research_agent.agent.guard import check_decision
from research_agent.agent.report import SourceTable, assemble

TARGET = {"id": "Q40066157", "label": "松田浩一", "description": "研究者", "sitelinks": 0}
PAGES = ["https://www.k.u-tokyo.ac.jp/gsfs/faculty/koichi_matsuda/", "https://nrid.nii.ac.jp/ja/nrid/1000090401257/"]


def state(**extra):
    return {"request": "松田教授について", "target": TARGET, "settings": {"max_seconds": 420}, "steps": 5,
            "trace": [{"node": "search", "seconds": 70.0}, {"node": "search", "seconds": 60.0}],   # 130 秒 > 120 秒
            "reading_list": PAGES, "priority_urls": [], "pages_read": [], "searched": ["松田浩一"],
            "candidates": [TARGET], "verified": [], "coverage": [], "aspects": [], **extra}


def test_one_read_happens_before_responding_when_nothing_was_read():
    d = check_decision(state(), {"action": "respond"})
    assert d["action"] == "read_pages" and "1 ページも読んでいない" in d["guard"]
    # 読んだ後（失敗も含む）は、時間の上限どおりに応答する（読み直しを繰り返さない）
    read = state(pages_read=[{"url": PAGES[0], "final_url": PAGES[0], "ok": False}])
    assert check_decision(read, {"action": "search", "query": "x"})["action"] != "read_pages"


def test_nothing_to_read_still_responds():
    d = check_decision(state(reading_list=[]), {"action": "search", "query": "x"})
    assert d["action"] in ("respond", "find_images")


def test_the_report_says_why_research_stopped():
    s = state(stop_note="時間の上限（420 秒。報告をまとめる時間を含む）に達したため、見つけたページのうち 2 件を読めなかった")
    text = assemble(s, "確認できた事実がありません。", SourceTable([]))
    assert "### 確かめきれなかったこと\n- 時間の上限（420 秒。報告をまとめる時間を含む）に達したため、見つけたページのうち 2 件を読めなかった" in text
