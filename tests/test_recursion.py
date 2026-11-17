import time

from my_llm_app.agent import coverage as cov
from my_llm_app.agent.guard import check_decision
from my_llm_app.agent.nodes import confirm_target, follow_links
from my_llm_app.tools.fetch import Page, page_links

GOV = "https://www.bousai.go.jp/a"
TARGET = {"id": "Q1", "label": "地震A", "description": "2004年の地震", "sitelinks": 80}


def fact(aspect, source=GOV, text="t"):
    return {"text": text, "quote": "q", "aspect": aspect, "source": source}


def state(**kw):
    aspects = cov.aspects_for("出来事")
    verified = kw.pop("verified", [fact(a["name"]) for a in aspects])   # 既定ではすべての観点が確認済み
    base = {"request": "地震Aについて", "target": TARGET, "target_summary": "概要", "candidates": [TARGET],
            "searched": ["地震A"], "steps": 4, "aspects": aspects, "verified": verified,
            "coverage": cov.build_coverage(aspects, verified), "reading_list": ["u1"], "pages_read": [{"url": "u1"}],
            "conflicts_checked_at": len(verified)}
    return {**base, **kw}


def decide(s):
    return check_decision(s, {"action": "respond", "reason": ""})


def test_weak_aspect_is_searched_again():
    # R-6: 出典が Tier 4 の 1 サイトだけの観点は、その観点で検索し直す
    aspects = cov.aspects_for("出来事")
    verified = [fact(a["name"]) for a in aspects if a["name"] != "規模"] + [fact("規模", "https://blog.example.com/x")]
    d = decide(state(verified=verified))
    assert d["action"] == "search" and d["aspect"] == "規模"
    assert "弱い" in d["guard"]


def test_conflict_is_researched_once():
    conflict = {"aspect": "被害・影響", "topic": "死者数", "facts": []}
    d = decide(state(conflicts=[conflict]))
    assert d["action"] == "search" and d["query"] == "地震A 死者数" and d["conflict_topic"] == "死者数"
    assert decide(state(conflicts=[conflict], conflict_searched=["死者数"]))["action"] == "respond"


def test_priority_pages_are_read_even_when_coverage_is_full():
    d = decide(state(reading_list=["u1", "u2"], priority_urls=["u2"]))
    assert d["action"] == "read_pages" and "狙って探した" in d["guard"]


def test_converged_reading_stops():
    aspects = cov.aspects_for("出来事")
    verified = [fact("日時")]                                            # まだ埋まらない観点がある
    s = state(verified=verified, reading_list=["u1", "u2", "u3"], stale_reads=2, aspect_searched=["a", "b", "c"])
    assert decide(s)["action"] == "respond"                             # 新しい事実が 2 回続けて出ない → 読まない


def test_time_limit_forces_respond():
    s = state(reading_list=["u1", "u2"], priority_urls=["u2"], deadline=time.time() - 1)
    assert decide(s)["action"] == "respond"
    assert check_decision(s, {"action": "search", "reason": "", "query": "x"})["action"] == "respond"


def test_new_ambiguity_asks_for_confirmation():
    s = state(new_ambiguity="同名の2012年の地震", assumptions=["一般に…"])
    d = decide(s)
    assert d["action"] == "ask_user" and d["confirm_target"] is True
    assert "同名の2012年の地震" in d["question"] and "地震A（2004年の地震）" in d["question"]


def test_confirm_target_yes_and_no():
    s = state(assumptions=["一般に…"])
    yes = confirm_target(s, "1")
    assert yes["assumptions"] == [] and yes["ambiguity_asked"] is True and "target" not in yes
    no = confirm_target(s, "2012年のほうです")
    assert no["target"] is None and no["verified"] == [] and no["clarifications"] == ["2012年のほうです"]


def test_page_links_marks_reference_sections():
    html = """<html><body>
      <a href="/wiki/other">内部リンク</a>
      <ol class="references"><li><a href="https://www.jma.go.jp/report.html">気象庁の報告</a></li></ol>
      <a href="https://example.com/x">地震A の解説</a>
    </body></html>"""
    links = page_links(html, "https://ja.wikipedia.org/wiki/x")
    refs = {l["href"]: l["in_refs"] for l in links}
    assert refs["https://www.jma.go.jp/report.html"] is True
    assert refs["https://example.com/x"] is False


def test_follow_links_picks_high_tier_refs_on_other_sites():
    page = Page("https://ja.wikipedia.org/wiki/x", ok=True, links=[
        {"href": "https://ja.wikipedia.org/wiki/y", "text": "地震A", "in_refs": False},       # 同じサイト
        {"href": "https://www.jma.go.jp/report.html#top", "text": "報告", "in_refs": True},    # 出典欄・Tier 1
        {"href": "https://blog.example.com/a", "text": "地震A", "in_refs": True},             # Tier 4
        {"href": "https://www.nikkei.com/地震A", "text": "記事", "in_refs": False},           # URL に名前・Tier 2
        {"href": "https://www.mlit.go.jp/x", "text": "国交省", "in_refs": False},             # 関係が読み取れない
    ])
    assert follow_links(page, "地震A", known=set()) == ["https://www.jma.go.jp/report.html",
                                                        "https://www.nikkei.com/地震A"]


def test_unreadable_formats_are_not_queued():
    from my_llm_app.tools.fetch import is_unreadable
    assert is_unreadable("https://www.mlit.go.jp/a/44.pdf")
    assert is_unreadable("https://x.go.jp/a/B.PDF")
    assert not is_unreadable("https://www.bousai.go.jp/a.htm")
    assert is_unreadable("https://www.jstage.jst.go.jp/article/kaigan/76/2/76_I_331/_pdf/-char/ja")
    assert not is_unreadable("https://www.jstage.jst.go.jp/article/kaigan/76/2/76_I_331/_article/-char/ja")


def test_page_budget_counts_only_readable_pages():
    # 12 ページを試したが読めたのは 7 ページ → まだ読める
    tried = [{"url": f"u{i}", "ok": i < 7} for i in range(12)]
    s = state(verified=[fact("日時")], reading_list=[p["url"] for p in tried] + ["u12"], pages_read=tried)
    assert decide(s)["action"] == "read_pages"


def test_priority_pages_get_extra_budget_and_searches_stop_without_budget():
    full = [{"url": f"u{i}", "ok": True} for i in range(12)]          # 通常の枠（12）を使い切った
    s = state(reading_list=[p["url"] for p in full] + ["p1"], pages_read=full, priority_urls=["p1"])
    assert decide(s)["action"] == "read_pages"                        # 狙ったページは追加の枠で読む
    used_up = [{"url": f"u{i}", "ok": True} for i in range(16)]       # 追加の枠も使い切った
    conflict = {"aspect": "被害・影響", "topic": "死者数", "facts": []}
    s = state(reading_list=[p["url"] for p in used_up], pages_read=used_up, conflicts=[conflict])
    assert decide(s)["action"] == "respond"                           # 読めないなら確かめ直す検索はしない
