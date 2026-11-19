from my_llm_app.agent import coverage as cov
from my_llm_app.agent import query_plan as qp
from my_llm_app.agent.names import name_variants
from my_llm_app.tools.searxng import WebResult

LAB = "https://www.sw.it.aoyama.ac.jp/D%C3%BCrst/"
TARGET = {"id": "", "label": "DÜRST, Martin J.", "description": "教授", "sitelinks": 0}
ASPECTS = [{"name": "所属・役職", "words": "所属 役職"}, {"name": "経歴", "words": "経歴 学歴"},
           {"name": "業績", "words": "業績 論文"}]


def state(verified=(), **kw):
    verified = list(verified)
    return {"target": TARGET, "aspects": ASPECTS, "verified": verified,
            "coverage": cov.build_coverage(ASPECTS, verified), "searched": ["青山学院大学 マーティン 教授"], **kw}


def test_name_variants_come_from_result_titles():
    titles = ["研究者情報 - DÜRST, Martin J. | 青山学院大学", "Martin J. Dürst - Short CV", "Martin J. Dürst",
              "J DÜRST Martin (Jakob Martin Durst) - 青山学院大学 理工学部", "マーティンギター | 黒澤楽器"]
    v = name_variants(titles, "DÜRST, Martin J.")
    assert v["Martin J. Dürst"] == 2                      # 2 つの題名に出た
    assert "J DÜRST Martin" in v                          # 括弧の中は除く
    assert all("マーティン" not in k for k in v)          # 対象の名前でないもの
    assert "DÜRST, Martin J." not in v                    # 対象の名前そのものは表記ゆれではない


def test_long_product_titles_are_not_names():
    v = name_variants(["Olympus PEN EE-3 Half Frame 35mm Camera With 28mm F/3.5 Lens : Electronics",
                       "OLYMPUS PEN EE-3 | ミーナ", "オリンパス ペン EE-3 の使い方"], "Olympus PEN EE-3")
    assert "オリンパス ペン EE-3 の使い方" in v and "OLYMPUS PEN EE-3" not in v   # 大文字小文字だけの違いは同じ名前
    assert not any("Half Frame" in k for k in v)


def test_novelty_ignores_word_order_and_counts_name_and_words_separately():
    past = ["DÜRST, Martin J. 経歴 学歴"]
    assert qp.novelty("Martin J. Dürst", "経歴 学歴", past) == 0.0     # 語順が違うだけの同じ検索
    assert qp.novelty("DÜRST, Martin J.", "業績 論文", past) == 1.0    # 名前が同じでも観点が違う
    assert qp.novelty("テュールスト", "経歴 学歴", past) == 1.0          # 観点が同じでも名前が違う


def test_missing_aspects_first_then_stop_after_misses():
    s = state()
    best = qp.next_search(s)
    assert best["query"] == "DÜRST, Martin J. 所属 役職" and best["aspect"] == "所属・役職"
    # 3 つの観点とも空振り（新しいページが出ない）が 2 回続いた → 点数が下がり、検索をやめる
    log = [{"query": "x", "aspect": a["name"], "site": "", "new": 0} for a in ASPECTS] * 2
    assert qp.next_search(state(query_log=log)) is None


def test_variants_from_results_are_tried_for_missed_aspects():
    log = [{"query": "DÜRST, Martin J. 経歴 学歴", "aspect": "経歴", "site": "", "new": 0}]
    s = state(searched=["DÜRST, Martin J. 経歴 学歴"], query_log=log, name_variants={"テュールスト マーティン": 3})
    qs = [c["query"] for c in qp.candidates(s)]
    assert "テュールスト マーティン 経歴 学歴" in qs


def test_site_search_on_a_domain_that_gave_facts():
    fact = {"text": "教授", "quote": "q", "aspect": "所属・役職", "source": LAB}
    best = qp.next_search(state(verified=[fact], searched=["DÜRST, Martin J. 経歴 学歴",
                                                           "DÜRST, Martin J. 業績 論文"],
                                query_log=[{"query": "a", "aspect": "経歴", "site": "", "new": 0},
                                           {"query": "b", "aspect": "業績", "site": "", "new": 0}]))
    assert best["query"] == "DÜRST, Martin J. site:sw.it.aoyama.ac.jp" and best["site"] == "sw.it.aoyama.ac.jp"


def test_no_search_when_everything_is_confirmed_or_no_target():
    gov = "https://www.bousai.go.jp/a"
    facts = [{"text": "t", "quote": "q", "aspect": a["name"], "source": gov} for a in ASPECTS]
    assert qp.next_search(state(verified=facts)) is None
    assert qp.next_search({**state(), "target": None}) is None


def test_harvest_records_variants_sites_and_yield():
    results = [WebResult("Martin J. Dürst - 青山学院大学", LAB, "研究室", []),
               WebResult("マーティンギター", "https://kurosawagakki.com/x", "ギター", [])]
    out = qp.harvest({}, results, "DÜRST, Martin J.", {"query": "q", "aspect": "経歴"}, 1)
    assert out["name_variants"] == {"Martin J. Dürst": 1}
    assert out["seen_sites"] == ["sw.it.aoyama.ac.jp"]
    assert out["query_log"] == [{"query": "q", "aspect": "経歴", "site": "", "new": 1}]
