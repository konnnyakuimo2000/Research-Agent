"""フェーズ 3（転載・ページ単位の Tier）、フェーズ 4（見分ける特徴・V-4）、フェーズ 6（L3・L5・堅牢化）のテスト。"""

from unittest.mock import MagicMock, patch

import httpx

from research_agent.agent import coverage as cov
from research_agent.agent import imaging, reprints
from research_agent.agent.nodes import Nodes, images_section, page_meta, sources_text
from research_agent.config import Settings
from research_agent.tiers import adjusted_tier, page_tier
from research_agent.tools import databases, http, images, searxng
from research_agent.tools.databases import lookup as real_lookup   # conftest が差し替える前の本物
from research_agent.tools.images import FetchedImage, ImageCandidate

import http_mock  # noqa: E402  （tests/http_mock.py）

SETTINGS = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://sx")


# ---- フェーズ 6: 呼び出しの堅牢化（429・やり直し・キャッシュ・間隔）

def test_http_retries_rate_limits_with_retry_after_and_caches(monkeypatch):
    replies = [httpx.Response(429, headers={"retry-after": "2"}), httpx.Response(200, json={"ok": 1})]
    sent = http_mock.install(monkeypatch, lambda r: replies[min(len(sent) - 1, 1)])
    waits = []
    monkeypatch.setattr(http, "sleep", waits.append)
    assert http.get("https://api.example/x", params={"q": "a"}).json() == {"ok": 1}
    assert http.get("https://api.example/x", params={"q": "a"}).json() == {"ok": 1}   # 2 回目は覚えた応答
    assert waits == [2.0] and len(sent) == 2


def test_http_gives_up_when_told_to_wait_too_long_and_retries_connect_errors(monkeypatch):
    sent = http_mock.install(monkeypatch, lambda r: httpx.Response(429, headers={"retry-after": "3600"}))
    assert http.get("https://api.example/slow").status_code == 429       # 1 時間は待たない
    assert len(sent) == 1

    def flaky(request):
        if len(sent) == 1:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={"ok": 2})
    sent = http_mock.install(monkeypatch, flaky)
    assert http.get("https://api.example/flaky").json() == {"ok": 2}


def test_web_search_retries_once_when_engines_did_not_answer(monkeypatch):
    empty = {"results": [], "unresponsive_engines": [["google", "timeout"]]}
    full = {"results": [{"title": "t", "url": "https://a.jp/", "content": "", "engines": ["bing"]}],
            "unresponsive_engines": [["brave", "too many requests"]]}
    sent = http_mock.install(monkeypatch, lambda r: httpx.Response(200, json=empty if len(sent) == 1 else full))
    waits = []
    monkeypatch.setattr(http, "sleep", waits.append)
    out = searxng.web_search("http://sx", "水星")
    assert [w.url for w in out.results] == ["https://a.jp/"]
    assert out.retried and out.unresponsive == ["brave", "google"]   # 1 回目に応答しなかったエンジンも申告する
    assert searxng.RETRY_DELAY in waits


# ---- フェーズ 3: 転載の見分けとページ単位の Tier

ARTICLE = ("水星は太陽系の惑星の中で最も太陽に近い軌道を回る惑星である。表面には多数のクレーターがあり、"
           "大気はほとんど存在しない。昼の温度は約430度に達し、夜は約マイナス180度まで下がる。"
           "公転周期は約88日で、自転周期は約59日である。探査機マリナー10号とメッセンジャーが観測した。") * 2


def test_near_identical_pages_are_detected_as_reprints():
    original = reprints.signature(ARTICLE)
    mirror = reprints.signature("【転載】" + ARTICLE + "このページはウィキペディアの記事を元にしています。")
    other = reprints.signature("富士山は静岡県と山梨県にまたがる活火山で、標高は3776メートル。日本の最高峰である。" * 5)
    assert reprints.similarity(original, mirror) >= reprints.THRESHOLD
    assert reprints.find_original(mirror, {"https://ja.wikipedia.org/wiki/水星": original}) \
        == "https://ja.wikipedia.org/wiki/水星"
    assert reprints.find_original(other, {"https://ja.wikipedia.org/wiki/水星": original}) is None
    assert reprints.signature("短い") == []                      # 短い本文は比べない


def test_reprints_do_not_count_as_independent_sources():
    aspects = [{"name": "特徴", "words": "特徴"}]
    facts = [{"text": "a", "quote": "q", "aspect": "特徴", "source": "https://ja.wikipedia.org/wiki/水星"},
             {"text": "b", "quote": "q", "aspect": "特徴", "source": "https://mirror.example.com/mercury"}]
    assert cov.build_coverage(aspects, facts)[0]["status"] == cov.CONFIRMED       # 別のサイト 2 つ（うち Tier 2）
    state = {"reprints": {"https://mirror.example.com/mercury": "https://ja.wikipedia.org/wiki/水星"}}
    row = cov.build_coverage(aspects, facts, state)[0]
    assert row["status"] == cov.WEAK and row["independent"] == 1                    # 写しは数えない


def test_page_tier_official_sites_and_evaluation_exception():
    state = {"official_hosts": ["olympus.co.jp"], "page_tiers": {"https://blog.example.com/a": 3}}
    assert page_tier("https://www.olympus.co.jp/pen/", state) == 1                # メーカー公式は Tier 1
    assert page_tier("https://www.olympus.co.jp/pen/", state, "評価・評判") == 4    # 評価の観点では Tier 4
    assert page_tier("https://blog.example.com/a", state) == 3                     # LLM の判定で 1 段上げた
    assert page_tier("https://blog.example.com/a") == 4


def test_tier_adjustment_is_limited_to_one_step_in_the_allowed_range():
    assert adjusted_tier(4, "上げる") == 3 and adjusted_tier(3, "上げる") == 2
    assert adjusted_tier(2, "上げる") == 2                       # Tier 1〜2 は上げない
    assert adjusted_tier(1, "下げる") == 2 and adjusted_tier(4, "下げる") == 4   # Tier 3〜4 は下げない
    assert adjusted_tier(4, "そのまま") == 4


def test_page_meta_checks_features_terms_and_tier_reason():
    text = "オリンパス ペン EE-3 は1973年発売。別名 PEN EE3。シャッターボタンの横に赤いマークがある。" * 5
    out = {"features": [{"text": "赤いマーク", "quote": "シャッターボタンの横に赤いマークがある"},
                        {"text": "黒いボディ", "quote": "ボディは黒い"}],          # 本文にない引用は捨てる
           "new_terms": ["PEN EE3", "ペンイーイースリー"],                           # 本文にない表記は捨てる
           "page_tier": {"adjust": "上げる", "reason": "取扱説明書を出典に明記"}}
    meta = page_meta(out, text, "https://blog.example.com/ee3", 4)
    assert [f["text"] for f in meta["features"]] == ["赤いマーク"]
    assert meta["new_terms"] == ["PEN EE3"]
    assert meta["tier"] == 3 and meta["signature"]
    no_reason = page_meta({**out, "page_tier": {"adjust": "上げる", "reason": ""}}, text, "https://b/x", 4)
    assert no_reason["tier"] == 4                               # 理由のない調整は認めない


def test_sources_text_marks_reprints_and_adjusted_tiers():
    sources = [{"title": "Wikipedia", "url": "https://ja.wikipedia.org/wiki/水星", "tier": 2},
               {"title": "写し", "url": "https://mirror.example.com/m", "tier": 4},
               {"title": "愛好会", "url": "https://club.example.org/m", "tier": 3}]
    state = {"reprints": {"https://mirror.example.com/m": "https://ja.wikipedia.org/wiki/水星"},
             "page_tiers": {"https://club.example.org/m": 3}}
    text = sources_text(state, sources)
    assert text.splitlines() == ["[1] Wikipedia — https://ja.wikipedia.org/wiki/水星",
                                 "[2] 写し — https://mirror.example.com/m（[1] とほぼ同じ文章）",
                                 "[3] 愛好会 — https://club.example.org/m"]          # Tier は書かない


# ---- フェーズ 6: 分野別データベース（L3）

def test_orcid_keeps_only_records_with_the_same_name():
    data = {"expanded-result": [
        {"orcid-id": "0000-0001", "given-names": "Martin", "family-names": "Dürst", "credit-name": "Martin J. Dürst",
         "other-name": ["Martin Duerst"], "institution-name": ["Aoyama Gakuin University"]},
        {"orcid-id": "0000-0002", "given-names": "Nathanaël G.", "family-names": "Dürst", "credit-name": None,
         "other-name": [], "institution-name": ["University of Geneva"]}]}
    with patch.object(databases, "_json", return_value=data):
        recs = databases.orcid("Martin J. Dürst")
    assert [r.url for r in recs] == ["https://orcid.org/0000-0001"]
    assert "所属機関: Aoyama Gakuin University" in recs[0].text


def test_japan_search_drops_sibling_models_and_keeps_rights():
    data = {"list": [
        {"id": "a-1", "common": {"title": "コンパクトカメラ　オリンパスペンS", "provider": "博物館",
                                 "thumbnailUrl": ["https://img/s.jpg"], "contentsRightsType": "cc0"}},
        {"id": "a-2", "common": {"title": "オリンパスペンEE-3", "provider": "博物館",
                                 "thumbnailUrl": ["https://img/ee3.jpg"], "contentsRightsType": "ccby"}}]}
    with patch.object(databases, "_json", return_value=data):
        recs = databases.japan_search("オリンパス ペン EE-3")
    assert [r.url for r in recs] == ["https://jpsearch.go.jp/item/a-2"]
    assert recs[0].images[0]["license"] == "CC BY"


def test_lookup_reports_databases_that_failed():
    with patch.dict(databases.FUNCTIONS, {"orcid": MagicMock(side_effect=httpx.ConnectError("x")),
                                          "ndl_creator": MagicMock(return_value=[])}):
        recs, failed, used = real_lookup("人物", "山田太郎")
    assert recs == [] and failed == ["ORCID（ConnectError）"]
    assert used == ["ORCID", "国立国会図書館サーチ（著者）"]
    assert "researchmap.jp" in databases.sites_for("人物")               # API が使えないものは site: で狙う
    assert all(d["name"] != "researchmap" for d in databases.databases_for("人物"))


class RecordLLM:
    def chat_json(self, system, user, schema, temperature=0.2):
        if "subject_type" in schema["properties"]:
            return {"subject_type": "人物", "aspects": [{"name": "所属・役職", "words": "所属"},
                                                       {"name": "経歴", "words": "経歴"},
                                                       {"name": "業績", "words": "業績"}]}
        return {"facts": [{"text": "青山学院大学に所属", "quote": "所属機関: Aoyama Gakuin University",
                           "aspect": "所属・役職", "about": "対象そのもの"}],
                "other_entity": "", "features": [], "new_terms": [],
                "page_tier": {"adjust": "そのまま", "reason": ""}}


def unreadable(url):
    from research_agent.tools.fetch import Page
    return Page(url, reason="取得できない")


def test_read_pages_reads_database_records_first_without_fetching():
    rec = databases.Record("https://orcid.org/0000-0001", "ORCID: Martin J. Dürst",
                           "ORCID の研究者情報\n氏名: Martin J. Dürst\n所属機関: Aoyama Gakuin University", "ORCID")
    state = {"request": "Dürst 先生", "target": {"id": "", "label": "Martin J. Dürst", "description": "教授"},
             "reading_list": ["https://www.example.ac.jp/durst"]}
    with patch("research_agent.agent.nodes.databases.lookup", return_value=([rec], ["国立国会図書館サーチ（著者）（ReadTimeout）"],
                                                                         ["ORCID", "国立国会図書館サーチ（著者）"])), \
         patch("research_agent.agent.nodes.fetch.fetch_page", side_effect=lambda u: unreadable(u)) as fetch_page:
        out = Nodes(SETTINGS, RecordLLM()).read_pages(state)
    assert [c.args[0] for c in fetch_page.call_args_list] == ["https://www.example.ac.jp/durst"]   # 記録は取りに行かない
    assert out["pages_read"][0]["url"] == "https://orcid.org/0000-0001" and out["pages_read"][0]["verified"] == 1
    assert out["verified"][0]["source"] == "https://orcid.org/0000-0001"
    assert "researchmap.jp" in out["seen_sites"]
    assert any("国立国会図書館サーチ" in x for x in out["failures"])           # 使えなかったデータベースは申告する


# ---- フェーズ 4 / 6: 画像（V-4、L3、L5）

def test_rank_records_the_same_image_on_other_sites():
    a = ImageCandidate("https://img.example/x.jpg", "https://a.example.jp/p", "L4-ページ内")
    b = ImageCandidate("https://img.example/x.jpg?w=300", "https://b.example.org/q", "L1-画像検索")
    ranked = imaging.rank([a, b], ["x"])
    assert len(ranked) == 1 and ranked[0].extra["also_on"] == ["https://b.example.org/q"]


class RejectVLM:
    def chat_json(self, system, user, schema, temperature=0.2, image_data_url=None):
        return {"context": "low", "visual": "unknown", "visible_text": "", "features_seen": [],
                "description": "別の物", "reason": "別の物"}


def test_find_images_falls_back_to_reference_images_from_related_items():
    state = {"request": "ニホンカモシカの写真", "target": {"id": "Q1", "label": "テスト種", "description": "動物"},
             "db_images": [{"src": "https://static.inat/1.jpg", "page_url": "https://www.inaturalist.org/taxa/1",
                            "title": "iNaturalist", "caption": "代表写真", "license": "CC BY", "database": "iNaturalist"}]}
    rel = ImageCandidate("https://up/genus.jpg", "https://commons.wikimedia.org/wiki/File:g.jpg", "L5-周辺",
                         title="File:g.jpg", license="CC BY-SA 4.0",
                         extra={"related": "カモシカ属", "relation": "上位の分類群"})
    ok = FetchedImage(True, data_url="data:x", width=800, height=600)
    with patch.object(images, "wikidata_image", return_value=[]), \
         patch.object(images, "commons_category", return_value=[]), \
         patch.object(images, "commons_search", return_value=[]), \
         patch.object(images, "searxng_images", return_value=[]), \
         patch.object(images, "fetch_image", return_value=ok), \
         patch("research_agent.agent.nodes.images.related_images", return_value=[rel]):
        out = Nodes(SETTINGS, RejectVLM()).find_images(state)
    assert out["image_counts"]["L3-分野別データベース"] == 1
    assert [(i["src"], i["level"]) for i in out["images"]] == [("https://up/genus.jpg", imaging.REFERENCE)]
    text = images_section({**state, **out})
    assert text.index("見つかりませんでした") < text.index("参考として")       # まず見つからなかったことを書く
    assert "※ テスト種そのものではなく、関係のある「カモシカ属」の画像です。" in text


def test_image_prompt_gets_distinguishing_features():
    from research_agent.agent import prompts
    state = {"target": {"label": "EE-3", "description": "カメラ"}, "features": [{"text": "赤いマーク", "source": "u"}]}
    assert "赤いマーク" in prompts.image_user(state, "t", "c", "https://p")


def test_images_needing_permission_are_shown_only_without_free_ones():
    free = {"src": "https://up/a.jpg", "level": imaging.CONFIRMED, "license": "CC BY 2.0"}
    locked = {"src": "https://static.inat/1.jpg", "level": imaging.CONFIRMED, "license": "ALL RIGHTS RESERVED"}
    assert imaging.choose_shown([free, locked]) == [free]                  # 自由に使える画像があれば載せない
    assert "withheld" in locked
    only = {"src": "https://static.inat/2.jpg", "level": imaging.CONFIRMED, "license": "all rights reserved"}
    assert imaging.choose_shown([only]) == [only]                          # ほかになければ載せる
    text = images_section({"request": "写真", "target": {"label": "x"}, "images_checked": True, "images": [
        {**only, "page_url": "https://www.inaturalist.org/taxa/1", "tier": 2, "title": "iNaturalist"}]})
    assert "利用には権利者の許可が必要です" in text
    assert not imaging.needs_permission("") and imaging.needs_permission("著作権あり（教育目的の利用可）")


def test_report_lists_gbif_when_it_was_looked_up_after_inaturalist():
    inat = databases.Record("https://www.inaturalist.org/taxa/1", "iNaturalist: x", "学名: Capricornis crispus\n和名: x",
                            "iNaturalist", scientific_name="Capricornis crispus")
    with patch.dict(databases.FUNCTIONS, {"inaturalist": MagicMock(return_value=[inat])}), \
         patch.object(databases, "gbif", return_value=[]):
        _, _, used = real_lookup("生物", "x")
    assert used == ["iNaturalist", "GBIF"]


def test_gbif_uses_the_scientific_name_field_not_the_display_text():
    # 表示用の見出しを変えても、同じ学名で GBIF を引く。学名が空なら引かない（レビュー 12）
    renamed = databases.Record("https://www.inaturalist.org/taxa/1", "iNaturalist: x", "Scientific name: Capricornis crispus",
                               "iNaturalist", scientific_name="Capricornis crispus")
    with patch.dict(databases.FUNCTIONS, {"inaturalist": MagicMock(return_value=[renamed])}), \
         patch.object(databases, "gbif", return_value=[]) as gbif:
        _, _, used = real_lookup("生物", "x")
    gbif.assert_called_once_with("Capricornis crispus")
    assert used == ["iNaturalist", "GBIF"]
    nameless = databases.Record("https://www.inaturalist.org/taxa/2", "iNaturalist: y", "学名: Foo bar", "iNaturalist")
    with patch.dict(databases.FUNCTIONS, {"inaturalist": MagicMock(return_value=[nameless])}), \
         patch.object(databases, "gbif", return_value=[]) as gbif:
        _, _, used = real_lookup("生物", "y")
    gbif.assert_not_called()
    assert used == ["iNaturalist"]
