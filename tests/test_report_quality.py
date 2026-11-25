"""報告が簡素すぎる・デバッグ用の記述が多い問題の直し（2026-11-25、「はやぶさ」の調査）。

報告の見た目（Tier・検索エンジン名・画像の項目の一覧を書かない）は test_coverage・test_images・test_phase3_6・
test_search_node で確かめる。ここでは、材料（確かめた事実）が少なかった原因の直しを確かめる。
1. Shift_JIS の古いページ（ISAS の技術解説）を UTF-8 として読み、文字化けで事実が 0 件だった
2. 対象の Wikipedia の記事が、Tier の順に並べた読む一覧の後ろに回り、最後まで読まれなかった
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import httpx

from research_agent.agent import prompts
from research_agent.agent.nodes import Nodes
from research_agent.config import Settings
from research_agent.tools import fetch
from research_agent.tools.searxng import WebResult, WebSearchResponse
from research_agent.tools.wikimedia import Entity

SETTINGS = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")
HTML = '<html><head><meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS"><title>技術的特徴</title></head>' \
       '<body><p>「ミューゼス（MUSES）」とは工学実験探査機の略です。</p></body></html>'


def response(body: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": content_type}, content=body)


def test_the_meta_charset_is_used_when_the_header_has_none():
    r = response(HTML.encode("cp932"), "text/html")
    assert "ミューゼス（MUSES）" in fetch.html_text(r)


def test_the_header_charset_wins_and_utf8_is_the_default():
    assert "ミューゼス" in fetch.html_text(response(HTML.encode("cp932"), "text/html; charset=Shift_JIS"))
    plain = "<p>はやぶさ</p>"
    assert fetch.html_text(response(plain.encode("utf-8"), "text/html")) == plain
    assert fetch.html_text(response(plain.encode("cp932"), "text/html")) == plain     # 名乗りがなくても読める


class LLM:
    def chat_json(self, system, user, schema, temperature=0.2):
        return {"known": [], "unknown": [], "candidate_ids": ["Q1"], "target_id": "Q1", "specific_subject": "",
                "specific_evidence": "", "target_evidence": "はやぶさ"}


def test_the_target_s_wikipedia_article_is_read_first():
    probe = Entity("Q1", "はやぶさ", "小惑星探査機", 80)
    wiki = "https://ja.wikipedia.org/wiki/はやぶさ_(探査機)"
    web = WebSearchResponse(results=[WebResult("はやぶさ2 | JAXA", "https://www.hayabusa2.jaxa.jp/", "はやぶさ", [])])
    state = {"request": "はやぶさについて調べて", "tier1_searched": True,
             "decision": {"action": "search", "query": "はやぶさ", "names": ["はやぶさ"]}}
    with patch("research_agent.agent.nodes.wikimedia.search_entities", return_value=[probe]), \
         patch("research_agent.agent.nodes.wikimedia.get_entities", return_value={"Q1": probe}), \
         patch("research_agent.agent.nodes.wikimedia.summary_for_entity",
               return_value=SimpleNamespace(url=wiki, title="はやぶさ (探査機)", extract="はやぶさは小惑星探査機。")), \
         patch("research_agent.agent.nodes.searxng.web_search", return_value=web):
        out = Nodes(SETTINGS, LLM()).search(state)
    assert out["priority_urls"][0] == wiki


def test_the_report_writer_is_asked_for_an_overview_and_connected_prose():
    assert "2〜3 文でまとめた段落" in prompts.RESPOND_SYSTEM
    assert "つないだ読みやすい文章" in prompts.RESPOND_SYSTEM


def test_bare_aspect_lines_become_headings():
    from research_agent.agent.nodes import aspect_headings
    body = "概要の段落。\n\n正式名・型番\n正式名は MUSES-C [1]。\n**経歴**\n文。\n経歴：\n### 業績\n文。\n経歴の文。"
    out = aspect_headings(body, ["正式名・型番", "経歴", "業績"]).splitlines()
    assert out[2] == "### 正式名・型番" and out[4] == "### 経歴" and out[6] == "### 経歴"
    assert out[7] == "### 業績" and out[9] == "経歴の文。"              # 観点名だけの行でなければ直さない
