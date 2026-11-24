"""「九大の松田教授について」の調査（2026-11-24）で見つかった問題の直し。

対象が人（松田教授）ではなく、手がかりの九州大学に決まり、大学のページだけを読んで大学の報告を書いた。
1. Wikidata を検索語の最初の語（「九大」）で引いていた → 人を指す依頼では、人の名前で引く
2. 手がかりの言葉で一致した項目（九州大学）が、人の候補・選択肢になった → 人の候補にしない
3. 候補が手がかりに合わなくても、合わない選択肢を並べて聞き返した → 選択肢なしで手がかりを聞く
4. 名前を答えても（「松田泰治」）、答えた後は検索結果から人を決める道を通らなかった → 答えも根拠にして決める
5. 決めた対象と違うことを答えても、前の対象のまま調べ続けた → 対象と集めたものを捨てて決め直す
6. LLM が書いた質問の番号付きの項目が、画面で選択肢のボタンになった → プログラムの選択肢のときだけ
7. 本人のページ 4 件が Brave のサイト名のない URL（「http:///go/v1/…」）で読めなかった → 本当の宛先を取り出す
"""

from __future__ import annotations

from unittest.mock import patch

from research_agent.agent import query_plan
from research_agent.agent.guard import CHOICE_TAIL, build_question, build_confirm_question, has_choices
from research_agent.agent.nodes import Nodes, is_clue_entity
from research_agent.config import Settings
from research_agent.tools import searxng
from research_agent.tools.searxng import WebResult, WebSearchResponse
from research_agent.tools.wikimedia import Entity

SETTINGS = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")
REQUEST = "九大の松田教授について"
KYUDAI = Entity("Q1188786", "九州大学", "福岡県福岡市西区にある国立大学", 36, matched="九大")
TOWN = Entity("Q2", "松田町", "神奈川県の町", 20, matched="松田")
KYUDAI_C = {"id": "Q1188786", "label": "九州大学", "description": "福岡県福岡市西区にある国立大学", "sitelinks": 36}


# ---- 1・2. 人の名前で引き、手がかりの項目を人の候補にしない ----

def test_person_requests_are_split_into_the_name_and_the_clues():
    assert query_plan.request_person(REQUEST) == ("松田", ["九大"])
    assert query_plan.request_person("東京大学の山田教授を調べて") == ("山田", ["東京大学"])
    assert query_plan.request_person("青山学院大学の歴史について") == ("", [])     # 人でない依頼は分けない
    assert query_plan.request_person("富士山について調べて") == ("", [])


def test_clue_entities_are_not_people():
    assert is_clue_entity(KYUDAI, "松田", ["九大"])
    assert not is_clue_entity(TOWN, "松田", ["九大"])                              # 名前で一致したものは残す
    assert not is_clue_entity(Entity("Q3", "松田泰治", "九州大学の教授", 1, matched="松田"), "松田", ["九大"])


class UpdateLLM:
    def __init__(self, **out):
        self.out = {"known": [], "unknown": [], "candidate_ids": [], "target_id": "", "specific_subject": "",
                    "specific_evidence": "", "target_evidence": "", **out}

    def chat_json(self, system, user, schema, temperature=0.2):
        return self.out


def run_search(state, llm, web=WebSearchResponse(results=[]), entities=(KYUDAI, TOWN)):
    looked_up = []

    def search_entities(name, limit=8):
        looked_up.append(name)
        return list(entities)

    with patch("research_agent.agent.nodes.wikimedia.search_entities", side_effect=search_entities), \
         patch("research_agent.agent.nodes.wikimedia.get_entities", side_effect=lambda ids: {e.id: e for e in entities if e.id in ids}), \
         patch("research_agent.agent.nodes.wikimedia.summary_for_entity", return_value=None), \
         patch("research_agent.agent.nodes.wikimedia.official_websites", return_value=[], create=True), \
         patch("research_agent.agent.nodes.searxng.web_search", return_value=web):
        return Nodes(SETTINGS, llm).search(state), looked_up


def test_wikidata_is_looked_up_by_the_person_s_name_and_the_university_is_not_a_candidate():
    state = {"request": REQUEST, "searched": ["九州大学 松田 教授"],
             "decision": {"action": "search", "query": "九大 松田 教授", "names": [], "rephrase": True}}
    out, looked_up = run_search(state, UpdateLLM(candidate_ids=["Q1188786", "Q2"], target_id="Q1188786"))
    assert looked_up == ["松田"]                                           # 前は「九大」で引いていた
    assert [c["label"] for c in out["candidates"]] == ["松田町"]
    assert not out["target"] or out["target"]["label"] != "九州大学"


# ---- 4. 名前を答えたら、その名前の人に決まる ----

def test_a_name_given_as_an_answer_becomes_the_target():
    web = WebSearchResponse(results=[
        WebResult("松田 泰治 (Taiji Mazda) - researchmap", "https://researchmap.jp/read0184815", "", ["brave"]),
        WebResult("九州大学-研究者情報 [松田 泰治 (教授) 工学研究院", "https://hyoka.ofc.kyushu-u.ac.jp/x", "", ["brave"])])
    state = {"request": REQUEST, "searched": ["a", "b"], "clarifications": ["松田泰治"], "candidates": [],
             "decision": {"action": "search", "query": "九州大学 松田泰治 教授", "names": []}}
    llm = UpdateLLM(specific_subject="松田泰治", specific_evidence="九大の松田教授")
    out, _ = run_search(state, llm, web=web, entities=())
    assert out["target"]["label"] == "松田泰治"


# ---- 5. 違うことを答えたら、前の対象を捨てる ----

def ask(state, answer):
    with patch("research_agent.agent.nodes.interrupt", return_value=answer):
        return Nodes(SETTINGS, UpdateLLM()).ask_user({**state, "decision": {"action": "ask_user", "question": "Q?"}})


def test_an_answer_about_someone_else_drops_the_wrong_target():
    state = {"request": REQUEST, "target": KYUDAI_C, "candidates": [KYUDAI_C], "verified": [{"text": "x"}],
             "pages_read": [{"url": "https://www.kyushu-u.ac.jp/"}], "images": [{"src": "campus.jpg"}]}
    out = ask(state, "松田泰治")
    assert out["target"] is None and out["verified"] == [] and out["pages_read"] == [] and out["images"] == []
    assert out["clarifications"] == ["松田泰治"]
    kept = ask(state, "九州大学の工学部の先生です")                         # 対象の名前を含む答えは、前の対象を残す
    assert "target" not in kept


# ---- 3・6. 聞き返しの選択肢 ----

def test_no_choices_when_no_candidate_fits_the_clues():
    q = build_question({"request": REQUEST, "candidates": [KYUDAI_C, {"id": "Q9", "label": "中国共産党第九回全国代表大会",
                                                                         "description": "Party congress in 1969"}]})
    assert "1." not in q and "手がかり" in q and not has_choices(q)


def test_only_program_made_choices_are_buttons():
    town = {"id": "Q2", "label": "松田", "description": "九大の教授", "sitelinks": 1}
    other = {"id": "Q4", "label": "松田", "description": "九大病院の医師", "sitelinks": 1}
    q = build_question({"request": REQUEST, "candidates": [town, other]})
    assert q.endswith(CHOICE_TAIL) and has_choices(q)
    assert has_choices(build_confirm_question({"target": town, "new_ambiguity": "別の松田"}))
    assert not has_choices("より正確に特定するため、以下を教えてください。\n1. 松田教授のお名前\n2. 所属")


# ---- 7. Brave の中継用の URL ----

RELAY = "http:///go/v1/AdEXRSMF"


def test_brave_relays_are_kept_for_resolving():
    data = {"results": [{"title": "松田 泰治", "url": RELAY}, {"title": "ok", "url": "https://www.kyushu-u.ac.jp/x"},
                        {"title": "壊れた", "url": "http:///other"}, {"title": "なし", "url": ""}]}
    assert [r.url for r in searxng.parse_response(data, 10).results] == [RELAY, "https://www.kyushu-u.ac.jp/x"]


def test_the_destination_is_read_from_brave_s_relay_page():
    page = ('<p>The link you followed is sending you to <strong class="host">researchmap.jp</strong>: '
            '<a class="destination svelte-12edz7s" href="https://researchmap.jp/read0184815?a=1&amp;b=2">x</a></p>')
    with patch("research_agent.tools.searxng.http.get", return_value=type("R", (), {"text": page})()) as get:
        assert searxng.resolve_brave(RELAY) == "https://researchmap.jp/read0184815?a=1&b=2"
    assert get.call_args[0][0] == "https://search.brave.com/go/v1/AdEXRSMF"
    with patch("research_agent.tools.searxng.http.get", return_value=type("R", (), {"text": "<html>no link</html>"})()):
        assert searxng.resolve_brave(RELAY) == ""


def test_unresolved_relays_are_dropped():
    out = WebSearchResponse(results=[WebResult("a", RELAY, "", ["brave"]), WebResult("b", RELAY + "x", "", ["brave"])])
    with patch("research_agent.tools.searxng.resolve_brave", side_effect=["https://researchmap.jp/r", ""]):
        assert [r.url for r in searxng.resolve_relays(out).results] == ["https://researchmap.jp/r"]


# ---- 本物の環境での 2 回目に見つかったこと ----

def test_the_llm_s_affiliation_name_is_not_looked_up():
    # LLM は名前に「九州大学」も渡す。略称（九大）と正式名で手がかりと一致しないので、名前の側で絞る
    uni = Entity("Q1188786", "九州大学", "福岡県福岡市西区にある国立大学", 36, matched="九州大学")
    state = {"request": REQUEST, "decision": {"action": "search", "query": "九州大学 松田 教授", "names": ["松田教授", "九州大学"]}}
    out, looked_up = run_search(state, UpdateLLM(candidate_ids=["Q1188786"], target_id="Q1188786",
                                                 target_evidence="九大の松田教授"), entities=(uni,))
    assert looked_up == ["松田教授"]
    state = {**state, "clarifications": ["松田泰治"],
             "decision": {"action": "search", "query": "九州大学 松田泰治 教授", "names": ["松田泰治", "九州大学"]}}
    _, looked_up = run_search(state, UpdateLLM(), entities=())
    assert looked_up == ["松田泰治"]


def test_a_name_the_user_gave_is_used_even_if_no_title_has_it():
    # Brave が応答せず、Bing の結果の題名に本人の名前がなかった。利用者が答えた名前は、題名で確かめずに使う
    web = WebSearchResponse(results=[WebResult("九州のおすすめ観光スポット", "https://tour.example.jp/", "", ["bing"])])
    state = {"request": REQUEST, "searched": ["a"], "clarifications": ["松田泰治"], "candidates": [],
             "decision": {"action": "search", "query": "九州大学 松田泰治 教授", "names": ["松田泰治"]}}
    out, _ = run_search(state, UpdateLLM(specific_subject="松田泰治", specific_evidence="九大の松田教授"), web=web, entities=())
    assert out["target"]["label"] == "松田泰治"
