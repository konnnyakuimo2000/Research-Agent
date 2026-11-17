from unittest.mock import patch

from my_llm_app.agent.guard import check_decision
from my_llm_app.agent.nodes import Nodes
from my_llm_app.agent.quotes import quote_in_text
from my_llm_app.tools.fetch import Page, is_skipped_domain

TEXT = "水星は太陽系の惑星の中で最も内側を公転している。昼の表面温度は約４３０℃に達する。"


def test_quote_matches_ignoring_width_and_spaces():
    assert quote_in_text("昼の表面温度は約430℃に達する", TEXT)            # 全角数字との違いを無視
    assert quote_in_text("水星は太陽系の惑星の中で\n最も内側を公転", TEXT)    # 改行の違いを無視


def test_quote_rejects_paraphrase_and_too_short():
    assert not quote_in_text("昼の気温は430度ほどになる", TEXT)   # 言い換えは認めない
    assert not quote_in_text("水星は", TEXT)                       # 短すぎる引用は認めない


def test_skipped_domains():
    assert is_skipped_domain("https://www.pinterest.jp/pin/123")
    assert not is_skipped_domain("https://ja.wikipedia.org/wiki/水星")


class FakeLLM:
    def chat_json(self, system, user, schema, temperature=0.2):
        return {"facts": [
            {"text": "最も内側を公転する", "quote": "太陽系の惑星の中で最も内側を公転している"},   # 本文にある
            {"text": "昼は約430℃", "quote": "昼の表面温度は約430℃に達する"},                   # 本文にある（幅違い）
            {"text": "直径は4,880km", "quote": "直径は4,880kmである"},                           # 本文にない → 捨てる
        ]}


def fake_fetch(url):
    if "broken" in url:
        return Page(url, reason="取得できない（HTTP 404）")
    return Page(url, title="水星とは", text=TEXT, ok=True)


def test_read_pages_keeps_only_verified_quotes_and_records_failures():
    state = {"request": "水星について", "target": {"id": "Q308", "label": "水星", "description": "惑星"},
             "reading_list": ["https://a.example/1", "https://broken.example/2"]}
    with patch("my_llm_app.agent.nodes.fetch.fetch_page", side_effect=fake_fetch):
        out = Nodes(settings=None, llm=FakeLLM()).read_pages(state)
    assert [f["text"] for f in out["verified"]] == ["最も内側を公転する", "昼は約430℃"]
    assert all(f["source"] == "https://a.example/1" for f in out["verified"])
    assert out["pages_read"] == [
        {"url": "https://a.example/1", "ok": True, "reason": "", "extracted": 3, "verified": 2},
        {"url": "https://broken.example/2", "ok": False, "reason": "取得できない（HTTP 404）",
         "extracted": 0, "verified": 0},
    ]
    assert out["trace"][0]["pages"][0]["dropped"][0]["text"] == "直径は4,880km"


TARGET = {"id": "Q308", "label": "水星", "description": "惑星", "sitelinks": 274}


def researched(**kw):
    return {"request": "水星について", "target": TARGET, "target_summary": "概要", "searched": ["水星"],
            "candidates": [TARGET], "steps": 3, "reading_list": ["u1", "u2"], **kw}


def test_respond_before_reading_pages_becomes_read_pages():
    d = check_decision(researched(), {"action": "respond", "reason": ""})
    assert d["action"] == "read_pages"


def test_respond_allowed_after_reading_with_verified_facts():
    state = researched(pages_read=[{"url": "u1"}], verified=[{"text": "t", "quote": "q", "source": "u1"}],
                       conflicts_checked=True)
    assert check_decision(state, {"action": "respond", "reason": ""})["action"] == "respond"


def test_reads_more_when_nothing_verified_yet():
    state = researched(pages_read=[{"url": "u1"}], verified=[])
    assert check_decision(state, {"action": "respond", "reason": ""})["action"] == "read_pages"


def test_respond_allowed_when_all_pages_read_even_without_facts():
    state = researched(pages_read=[{"url": "u1"}, {"url": "u2"}], verified=[])
    assert check_decision(state, {"action": "respond", "reason": ""})["action"] == "respond"
