"""例 B（マイナーな人物）・例 C（型番の違いが重要）で見つかった問題の修正。"""

from __future__ import annotations

from unittest.mock import patch

from my_llm_app.agent import coverage as cov
from my_llm_app.agent import imaging
from my_llm_app.agent import prompts
from my_llm_app.agent.guard import check_decision
from my_llm_app.agent.nodes import Nodes, drop_sentences_with, mentions, unsupported_names
from my_llm_app.config import Settings
from my_llm_app.tiers import by_tier
from my_llm_app.tools import images
from my_llm_app.tools.images import ImageCandidate
from my_llm_app.tools.searxng import WebResult, WebSearchResponse
from my_llm_app.tools.wikimedia import Entity

SETTINGS = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")


# ---- 1. 材料にない名前を書かない（B1）

def test_unsupported_names_finds_made_up_names():
    materials = '{"特定できた対象": "マーティン・デュルスト", "出典のタイトル": ["Martin J. Dürst"]}'
    body = "マーティン・デュルスト（Martin Tullberg）は教授である。Martin J. Dürst の研究室。"
    assert unsupported_names(body, materials) == ["Tullberg"]
    assert unsupported_names("マーティン・テュールストは教授。", materials) == ["マーティン・テュールスト"]


def test_drop_sentences_with_keeps_headings_and_other_sentences():
    body = "### 所属\n教授である [1]。英語名は Martin Tullberg である。\n### 経歴\n研究員だった [2]。"
    out = drop_sentences_with(body, ["Tullberg"])
    assert "Tullberg" not in out and "教授である [1]。" in out and "### 経歴" in out and "研究員だった" in out


class NamingLLM:
    """1 回目は材料にない英語名を書き、2 回目は直す。"""
    def __init__(self, fixed: bool):
        self.calls, self.fixed = [], fixed

    def chat_text(self, system, user, temperature=0.5):
        self.calls.append(user)
        if len(self.calls) == 1 or not self.fixed:
            return "### 所属・役職\n教授である [1]。英語名は Martin Tullberg である。"
        return "### 所属・役職\n教授である [1]。"


def test_respond_retries_then_drops_made_up_names():
    state = {"request": "マーティン先生", "target": {"id": "", "label": "マーティン", "description": "", "sitelinks": 0}}
    llm = NamingLLM(fixed=True)
    out = Nodes(SETTINGS, llm).respond(state)
    assert len(llm.calls) == 2 and "Tullberg" in llm.calls[1] and "Tullberg" not in out["answer"]
    llm = NamingLLM(fixed=False)
    out = Nodes(SETTINGS, llm).respond(state)
    assert "Tullberg" not in out["answer"] and "教授である" in out["answer"]   # 書き直しても残る文は除く


def test_respond_writes_infeasible_parts_itself():
    class LLM:
        def chat_text(self, system, user, temperature=0.5):
            return "本文 [1]。\n\n### 調べられなかったこと\n- まだわからないことの一覧"
    out = Nodes(SETTINGS, LLM()).respond({"request": "x", "feasibility": {"infeasible_parts": ["私的な写真"]}})
    assert "まだわからないこと" not in out["answer"]                 # LLM の節は除く
    assert "### 調べられなかったこと（公開情報では調べられないため）\n- 私的な写真" in out["answer"]


# ---- 2. 対象そのものについての事実だけを使う（C1）

class ExtractLLM:
    def chat_json(self, system, user, schema, temperature=0.2):
        return {"facts": [
            {"text": "EE-3は1973年発売", "quote": "ペンEE-3は1973年に発売された", "aspect": "発売年",
             "about": prompts.ABOUT_TARGET},
            {"text": "ペンEEは1961年発売", "quote": "初代のペンEEは1961年に発売された", "aspect": "発売年",
             "about": "別のもの"},
        ], "other_entity": ""}


def test_facts_about_sibling_models_are_dropped():
    page = type("P", (), {"ok": True, "url": "https://x.jp/pen", "title": "ペン",
                          "text": "初代のペンEEは1961年に発売された。ペンEE-3は1973年に発売された。"})()
    state = {"request": "ペン EE-3", "aspects": cov.aspects_for("製品・機械")}
    with patch("my_llm_app.agent.nodes.fetch.fetch_page", return_value=page):
        _, facts, dropped, _ = Nodes(SETTINGS, ExtractLLM())._read_one(state, page.url)
    assert [f["text"] for f in facts] == ["EE-3は1973年発売"]
    assert dropped[0]["dropped"] == "対象そのものについての記述ではない"


# ---- 3. 画像は調べ直しより先に探す。LLM には画像を探させない（C2, C3）

TARGET = {"id": "", "label": "ペン EE-3", "description": "カメラの", "sitelinks": 0}
FACT = {"text": "t", "quote": "q", "aspect": "発売年", "source": "u1"}


def base_state(**kw):
    aspects = cov.aspects_for("製品・機械")
    s = {"request": "カメラのペン EE-3 の外観の写真", "target": TARGET, "candidates": [TARGET], "searched": ["a"],
         "steps": 4, "aspects": aspects, "verified": [FACT], "coverage": cov.build_coverage(aspects, [FACT]),
         "reading_list": ["u1", "u2"], "pages_read": [{"url": "u1", "ok": True}], "conflicts_checked_at": 1,
         "wants_images": True}
    return {**s, **kw}


def test_images_come_before_further_research():
    # 観点が埋まっていなくても、最初の読み込みの後は画像を先に探す
    assert check_decision(base_state(), {"action": "respond", "reason": ""})["action"] == "find_images"
    after = check_decision(base_state(images_checked=True), {"action": "respond", "reason": ""})
    assert after["action"] == "read_pages"


def test_time_over_still_finds_images_once():
    late = [{"node": "read_pages", "seconds": 700}]
    assert check_decision(base_state(trace=late), {"action": "search", "query": "x", "reason": ""})["action"] \
        == "find_images"
    assert check_decision(base_state(trace=late, images_checked=True),
                          {"action": "search", "query": "x", "reason": ""})["action"] == "respond"


def test_llm_image_searches_are_redirected():
    d = check_decision(base_state(images_checked=True), {"action": "search", "query": "ペン EE-3 写真 jpg", "reason": ""})
    assert d["action"] != "search"                    # 応答の前に必要な調査（ここでは追加の読み込み）に移る


def test_free_searches_are_limited():
    s = base_state(images_checked=True, searched=["a", "b", "c"], reading_list=["u1"])
    d = check_decision(s, {"action": "search", "query": "ペン EE-3 詳細", "reason": ""})
    assert d["action"] != "search" or d.get("aspect")   # LLM の検索ではなく、プログラムが決める観点の検索
    s = base_state(images_checked=True, searched=["a", "b"], reading_list=["u1"])
    assert check_decision(s, {"action": "search", "query": "ペン EE-3 詳細", "reason": ""})["action"] == "search"


# ---- 4. 取れない・小さすぎる画像は、確かめる回数に数えない（B4）

class CountingVLM:
    def __init__(self):
        self.calls = 0

    def chat_json(self, system, user, schema, temperature=0.2, image_data_url=None):
        self.calls += 1
        return {"context": "high", "visual": "consistent", "description": "教授の写真", "reason": "名前あり"}


def test_small_images_do_not_use_up_checks():
    icons = [ImageCandidate(f"https://u.ac.jp/i{i}.png", "https://u.ac.jp/p", "L4-ページ内") for i in range(8)]
    photo = ImageCandidate("https://u.ac.jp/photo.jpg", "https://u.ac.jp/p", "L1-画像検索", title="マーティン教授")

    def fake_fetch(src):
        if "photo" in src:
            return images.FetchedImage(True, data_url="data:x", width=800, height=600)
        return images.FetchedImage(False, reason="小さすぎる（14×9）")

    state = {"request": "マーティン先生の写真", "target": {"id": "", "label": "マーティン", "description": ""},
             "page_images": [], "verified": []}
    llm = CountingVLM()
    with patch.object(images, "commons_search", return_value=[]), \
         patch.object(images, "searxng_images", return_value=icons + [photo]), \
         patch.object(images, "fetch_image", side_effect=fake_fetch):
        out = Nodes(SETTINGS, llm).find_images(state)
    assert llm.calls == 1                                         # LLM で確かめたのは使える 1 枚だけ
    assert [i["src"] for i in out["images"]] == ["https://u.ac.jp/photo.jpg"]
    assert sum(r["level"] == imaging.REJECTED for r in out["image_log"]) == 8


def test_page_images_skip_icons_with_session_ids():
    html = ('<img src="/img/icn_08.gif;jsessionid=81"><img src="/img/btn_head01.jpg">'
            '<img src="/assets/lazyload-e60e.png"><img src="/photo/prof.jpg" alt="教授">')
    assert [i["src"] for i in images.page_images(html, "https://u.ac.jp/")] == ["https://u.ac.jp/photo/prof.jpg"]


# ---- 5. 依頼文の条件で対象が決まるときは聞き返さない（B2, C5）

class SpecificLLM:
    def __init__(self, subject, evidence):
        self.subject, self.evidence = subject, evidence

    def chat_json(self, system, user, schema, temperature=0.2):
        return {"known": [], "unknown": [], "candidate_ids": ["Q1", "Q2"], "target_id": "", "target_evidence": "",
                "specific_subject": self.subject, "specific_evidence": self.evidence}


CITY = Entity("Q1", "マーティン", "テネシー州の市", 30, matched="マーティン")
NAME = Entity("Q2", "マーティン", "男性の名前", 40, matched="マーティン")
WEB = WebSearchResponse([WebResult("Martin J. Dürst", "https://www.sw.it.aoyama.ac.jp/D%C3%BCrst/",
                                   "情報テクノロジー学科 教授", [])])


def run_search(llm, request):
    state = {"request": request, "decision": {"action": "search", "query": "q", "names": ["マーティン"]}}
    with patch("my_llm_app.agent.nodes.wikimedia.search_entities", return_value=[CITY, NAME]), \
         patch("my_llm_app.agent.nodes.wikimedia.get_entities", return_value={"Q1": CITY, "Q2": NAME}), \
         patch("my_llm_app.agent.nodes.wikimedia.summary_for_entity", return_value=None), \
         patch("my_llm_app.agent.nodes.searxng.web_search", return_value=WEB):
        return Nodes(SETTINGS, llm).search(state)


def test_specific_request_sets_target_without_asking():
    request = "青山学院大学の情報テクノロジー学科のマーティン先生について"
    out = run_search(SpecificLLM("Martin J. Dürst", "青山学院大学の情報テクノロジー学科の"), request)
    assert out["target"]["label"] == "Martin J. Dürst" and out["candidates"] == [out["target"]]
    assert out["assumptions"] == []                              # 依頼文で決まったので前提ではない
    assert check_decision({**out, "request": request, "steps": 1}, {"action": "respond", "reason": ""})["action"] \
        != "ask_user"


def test_specific_subject_must_be_written_in_results():
    # 検索結果にない表記（LLM が作ったカタカナの読み）では対象を決めない
    request = "青山学院大学の情報テクノロジー学科のマーティン先生について"
    out = run_search(SpecificLLM("マーティン・ヤーコプ・デュルスト", "青山学院大学の情報テクノロジー学科の"), request)
    assert out["target"] is None                                    # 書き直しても検索結果にない → 決めない
    assert any("specific_subject rejected" in e for e in out["trace"][0]["errors"])


def test_specific_subject_is_rewritten_from_results():
    class FixLLM(SpecificLLM):
        def chat_json(self, system, user, schema, temperature=0.2):
            if "specific_subject" in schema["properties"] and len(schema["properties"]) == 1:
                return {"specific_subject": "Martin J. Dürst"}        # 検索結果の題名から写した
            return super().chat_json(system, user, schema, temperature)
    request = "青山学院大学の情報テクノロジー学科のマーティン先生について"
    out = run_search(FixLLM("マーティン・ヤーコプ・デュルスト", "青山学院大学の情報テクノロジー学科の"), request)
    assert out["target"]["label"] == "Martin J. Dürst"


def test_unspecific_request_still_asks():
    # 依頼文に絞り込む条件がない（根拠の語句が決まり文句だけ）→ 対象を決めない
    out = run_search(SpecificLLM("マーティン", "について"), "マーティンについて調べて")
    assert out["target"] is None and len(out["candidates"]) == 2


# ---- 6. 関係ない Tier 1 のページ・URL の表記ゆれ（C4, B5）

def test_mentions_matches_names_and_model_numbers():
    assert mentions("OLYMPUS PEN EE-3 の使い方", ["オリンパス ペン EE-3"])
    assert mentions("Martin J. Dürst - 研究室", ["マーティン", "Martin J. Dürst"])
    assert not mentions("陸上自衛隊の装備品", ["オリンパス ペン EE-3"])


def test_mentions_matches_reordered_latin_names_but_not_siblings():
    # 大学の表記「DÜRST, Martin Jakob」と、授業ページの「Martin J. Dürst」
    assert mentions("アルゴリズムとデータ構造 第1回 Martin J. Dürst", ["DÜRST, Martin Jakob"])
    assert not mentions("Martin Luther King の生涯", ["DÜRST, Martin Jakob"])       # 1 語だけ
    assert not mentions("Martinez Dürstenfeld", ["DÜRST, Martin Jakob"])            # 語の一部
    assert not mentions("OLYMPUS PEN EE-2 の使い方", ["OLYMPUS PEN EE-3"])          # 兄弟機種


def test_by_tier_merges_url_spelling_variants():
    urls = ["https://www.sw.it.aoyama.ac.jp/D%C3%BCrst/", "https://www.sw.it.aoyama.ac.jp/D%c3%bcrst/",
            "https://www.sw.it.aoyama.ac.jp/D%C3%BCrst/#top"]
    assert by_tier(urls) == ["https://www.sw.it.aoyama.ac.jp/D%C3%BCrst/"]


def test_image_rank_matches_names_across_spellings():
    other = ImageCandidate("https://up/ee2.jpg", "https://ja.wikipedia.org/wiki/x", "L4-ページ内", caption="ペン EE-2")
    mine = ImageCandidate("https://up/Olympus_pen_ee3.jpg", "https://commons.wikimedia.org/wiki/File:Olympus_pen_ee3.jpg",
                          "L2-Commons", title="File:Olympus pen ee3.jpg")
    assert imaging.rank([other, mine], ["Olympus PEN EE-3"])[0] is mine


def test_unrelated_results_are_not_read_once_target_is_known():
    web = WebSearchResponse([WebResult("Martin J. Dürst の研究室", "https://www.sw.it.aoyama.ac.jp/D%C3%BCrst/", "", []),
                             WebResult("洋服の青山 公式通販", "https://www.y-aoyama.jp/ec/shop/", "スーツ", [])])
    target = {"id": "", "label": "Martin J. Dürst", "description": "教授", "sitelinks": 0}
    state = {"request": "青山学院大学のマーティン先生", "target": target, "candidates": [target], "tier1_searched": True,
             "decision": {"action": "search", "query": "マーティン 経歴", "names": ["Martin J. Dürst"],
                          "aspect": "経歴"}}

    class LLM:
        def chat_json(self, system, user, schema, temperature=0.2):
            return {"known": [], "unknown": [], "candidate_ids": [], "target_id": "", "target_evidence": "",
                    "specific_subject": "", "specific_evidence": ""}
    with patch("my_llm_app.agent.nodes.wikimedia.search_entities", return_value=[]), \
         patch("my_llm_app.agent.nodes.wikimedia.get_entities", return_value={}), \
         patch("my_llm_app.agent.nodes.searxng.web_search", return_value=web):
        out = Nodes(SETTINGS, LLM()).search(state)
    assert out["reading_list"] == ["https://www.sw.it.aoyama.ac.jp/D%C3%BCrst/"]
    assert out["priority_urls"] == ["https://www.sw.it.aoyama.ac.jp/D%C3%BCrst/"]
