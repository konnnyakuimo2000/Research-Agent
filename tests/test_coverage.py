from research_agent.agent import coverage as cov
from research_agent.agent.guard import check_decision
from research_agent.agent.nodes import Nodes

GOV = "https://www.bousai.go.jp/a"          # Tier 1
WIKI = "https://ja.wikipedia.org/wiki/a"    # Tier 2
BLOG1 = "https://blog.example.com/a"        # Tier 4
BLOG2 = "https://other.example.net/b"       # Tier 4


def test_checklist_is_loaded_per_type():
    assert "出来事" in cov.type_names()
    assert [a["name"] for a in cov.aspects_for("出来事")][:2] == ["日時", "場所"]
    assert cov.aspects_for("存在しない種類") == cov.aspects_for("その他")


def test_status_rules():
    assert cov.status_of([]) == cov.MISSING
    assert cov.status_of([GOV]) == cov.CONFIRMED                 # Tier 1 は 1 つでよい
    assert cov.status_of([WIKI]) == cov.WEAK                     # Tier 2 が 1 つだけ
    assert cov.status_of([WIKI, BLOG1]) == cov.CONFIRMED         # 独立した 2 サイト、うち 1 つが Tier 2
    assert cov.status_of([BLOG1, BLOG2]) == cov.WEAK             # Tier 4 だけ
    assert cov.status_of([WIKI, "https://en.wikipedia.org/x"]) == cov.CONFIRMED  # 言語版違いは別サイトとして数える


def test_build_coverage_and_gaps_text():
    aspects = cov.aspects_for("出来事")
    verified = [{"text": "t", "quote": "q", "aspect": "日時", "source": GOV},
                {"text": "t", "quote": "q", "aspect": "場所", "source": BLOG1}]
    rows = {r["aspect"]: r for r in cov.build_coverage(aspects, verified)}
    assert rows["日時"]["status"] == cov.CONFIRMED and rows["日時"]["best_tier"] == 1
    assert rows["場所"]["status"] == cov.WEAK
    assert rows["規模"]["status"] == cov.MISSING
    text = cov.gaps_text(list(rows.values()))
    assert "- 規模: 信頼できる出典が見つかりませんでした" in text
    assert "場所" not in text and "日時" not in text              # 出典がある観点は「見つからなかった」に入れない
    weak = cov.weak_text(list(rows.values()))
    assert weak == "- 場所: 1 つのサイトにしか書かれておらず、ほかの出典では確かめられていません"   # Tier は書かない


def test_weak_text_for_many_low_tier_sites():
    aspects = cov.aspects_for("出来事")
    verified = [{"text": "t", "quote": "q", "aspect": "場所", "source": u}
                for u in ("https://blog.example.com/a", "https://shop.example.net/b")]
    weak = cov.weak_text(cov.build_coverage(aspects, verified))
    assert weak == "- 場所: 販売店や個人のサイトなどにしか書かれておらず、公的機関・メーカー・百科事典では確かめられていません"


TARGET = {"id": "Q1", "label": "地震A", "description": "2004年の地震", "sitelinks": 80}


def state(**kw):
    aspects = cov.aspects_for("出来事")
    base = {"request": "地震Aについて", "target": TARGET, "target_summary": "概要", "candidates": [TARGET],
            "searched": ["地震A"], "steps": 4, "aspects": aspects,
            "coverage": cov.build_coverage(aspects, kw.get("verified", []))}
    return {**base, **kw}


def test_reads_more_pages_while_aspects_are_unfilled():
    s = state(reading_list=["u1", "u2"], pages_read=[{"url": "u1"}],
              verified=[{"text": "t", "quote": "q", "aspect": "日時", "source": GOV, }])
    d = check_decision(s, {"action": "respond", "reason": ""})
    assert d["action"] == "read_pages"
    assert "埋まっていない" in d["guard"]


def test_searches_for_an_unfilled_aspect_when_no_pages_are_left():
    s = state(reading_list=["u1"], pages_read=[{"url": "u1"}],
              verified=[{"text": "t", "quote": "q", "aspect": "日時", "source": GOV}])
    d = check_decision(s, {"action": "respond", "reason": ""})
    assert d["action"] == "search"
    assert d["query"] == "地震A 場所" and d["aspect"] == "場所"


def test_aspect_search_limit():
    s = state(reading_list=["u1"], pages_read=[{"url": "u1"}], aspect_searched=["場所", "規模", "経緯・原因"],
              conflicts_checked_at=1,
              verified=[{"text": "t", "quote": "q", "aspect": "日時", "source": GOV}])
    assert check_decision(s, {"action": "respond", "reason": ""})["action"] == "respond"


def test_respond_appends_gaps_from_coverage():
    class TextLLM:
        def chat_text(self, system, user, temperature=0.5):
            return "本文"
    s = state(verified=[{"text": "t", "quote": "q", "aspect": "日時", "source": GOV}])
    answer = Nodes(None, TextLLM()).respond(s)["answer"]
    assert "### 確かめきれなかったこと" in answer
    assert "- 規模: 信頼できる出典が見つかりませんでした" in answer
