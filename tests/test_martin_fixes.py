"""「青山学院大学の情報テクノロジー学科のマーティン先生」の調査（2026-11-21）で見つかった 4 つの弱点の直し。

1. 対象が決まらないとき、聞き返す前に依頼文の言葉で 1 回検索し直す（検索結果は検索エンジンと言葉で揺れる）
2. 候補が依頼文の手がかりに合わない（名前だけで引いた米国の市など）ときは、そのまま聞き返さない
3. 名前が決まった後、LLM が書き写した名前の綴りの誤り（Dürrst）を直してから検索する
4. 文脈の弱い画像を、写っているものを確かめられないまま「おそらく正しい」にしない
"""

from research_agent.agent import imaging, query_plan
from research_agent.agent.guard import candidates_fit, check_decision, free_searches
from research_agent.agent.names import fix_spelling

REQUEST = "青山学院大学の情報テクノロジー学科のマーティン先生について調べて"
OFF_TARGET = [  # 2026-11-21 の調査で実際に出た候補（名前だけで Wikidata を引いたもの）
    {"id": "Q1", "label": "マーティン", "description": "city in Weakley County, Tennessee, USA"},
    {"id": "Q2", "label": "青山学院大学", "description": "東京都渋谷区にある私立大学"},
    {"id": "Q3", "label": "マーティン", "description": "名前"},
]


def stuck(**kw):
    return {"request": REQUEST, "searched": ["青山学院大学 情報テクノロジー学科 マーティン 先生"],
            "candidates": OFF_TARGET, "target": None, "questions_asked": 0, "steps": 2, **kw}


# ---- 1・2. 聞き返す前に、依頼文の言葉で検索し直す ----

def test_request_query_keeps_the_clues_and_says_teacher_plainly():
    assert query_plan.request_parts(REQUEST) == ["青山学院大学", "情報テクノロジー学科", "マーティン"]
    assert query_plan.request_query(REQUEST) == "青山学院大学 情報テクノロジー学科 マーティン 教員"
    assert query_plan.request_query("東京大学の山田教授を調べて") == "東京大学 山田 教授"
    assert query_plan.request_query("マーキュリーについて調べて") == "マーキュリー"


def test_off_target_candidates_do_not_fit_the_request():
    assert not candidates_fit(stuck())
    fits = stuck(candidates=OFF_TARGET + [{"id": "Q4", "label": "マーティン・デュルスト",
                                           "description": "青山学院大学の教授"}])
    assert candidates_fit(fits)
    # 手がかりのない依頼は、どれのことか聞くしかない
    assert candidates_fit({"request": "マーキュリーについて調べて",
                           "candidates": [{"label": "水星", "description": "惑星"}]})


def test_asking_with_off_target_candidates_becomes_one_rephrased_search():
    for action in ("ask_user", "respond"):
        d = check_decision(stuck(), {"action": action, "reason": "教員が特定できない"})
        assert d["action"] == "search" and d["rephrase"] is True
        assert d["query"] == "青山学院大学 情報テクノロジー学科 マーティン 教員"
    # 1 回検索し直しても決まらなければ、今までどおり聞き返す
    again = stuck(searched=["a", "青山学院大学 情報テクノロジー学科 マーティン 教員"],
                  rephrase_searched=["青山学院大学 情報テクノロジー学科 マーティン 教員"])
    assert check_decision(again, {"action": "ask_user", "reason": ""})["action"] == "ask_user"


def test_rephrase_is_not_used_when_the_question_is_reasonable():
    mercury = {"request": "マーキュリーについて調べて", "searched": ["マーキュリー"], "questions_asked": 0, "steps": 2,
               "candidates": [{"id": "Q308", "label": "水星", "description": "太陽系の惑星"},
                              {"id": "Q925", "label": "水銀", "description": "元素"}], "target": None}
    assert check_decision(mercury, {"action": "respond", "reason": ""})["action"] == "ask_user"
    answered = stuck(clarifications=["上にありません。教員のマーティン先生です"])
    assert check_decision(answered, {"action": "search", "reason": "", "query": "x"}).get("rephrase") is None


def test_rephrased_searches_do_not_use_up_the_llm_s_free_searches():
    s = {"searched": ["a", "b", "c"], "rephrase_searched": ["c"]}
    assert free_searches(s) == 2


# ---- 3. 名前の綴り ----

def test_misspelled_names_are_fixed_before_searching():
    q, fixed = fix_spelling("Martin J. Dürrst Aoyama Gakuin University professor", ["DÜRST, Martin J.", "Martin J. Dürst"])
    assert q == "Martin J. Dürst Aoyama Gakuin University professor" and fixed == ["Dürrst→Dürst"]
    assert fix_spelling("Martian research Martin", ["Martin J. Dürst"])[1] == []   # 似ているだけの別の語は直さない
    assert fix_spelling("青山学院大学 マーティン", ["Martin J. Dürst"])[1] == []


def test_the_guard_fixes_the_llm_s_query_once_the_name_is_known():
    state = {"request": REQUEST, "searched": ["x"], "steps": 3, "questions_asked": 0,
             "target": {"id": "", "label": "DÜRST, Martin J.", "description": ""},
             "name_variants": {"Martin J. Dürst": 3}}
    d = check_decision(state, {"action": "search", "reason": "", "query": "Martin J. Dürrst 経歴"})
    assert d["action"] == "search" and d["query"] == "Martin J. Dürst 経歴" and "Dürrst→Dürst" in d["guard"]


# ---- 4. 画像 ----

def test_weak_context_images_need_a_consistent_look():
    # 「大学の記事の写真だが、この人物が対象かは不明」（文脈 mid・内容 unknown・Tier 2）は採らない
    assert imaging.judge(2, True, "mid", "unknown") == imaging.REJECTED
    assert imaging.judge(2, True, "mid", "consistent") == imaging.PROBABLE
    assert imaging.judge(2, True, "high", "unknown") == imaging.CONFIRMED
