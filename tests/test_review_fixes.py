"""レビュー指摘（docs/claude_review_fixes.md）の回帰テスト。外部サービスは使わない。"""

from my_llm_app.agent.guard import check_decision
from my_llm_app.agent.names import mentions
from my_llm_app.agent.nodes import confirm_target
from my_llm_app.agent.state import MAX_STEPS
from my_llm_app.tiers import UrlNumbers, by_tier, canonical

TARGET = {"id": "Q1", "label": "水星", "description": "惑星", "sitelinks": 100}


# ---- 6. 対象を訂正したら旧対象の画像を消す ----

def test_correcting_the_target_clears_old_images_but_confirming_keeps_them():
    state = {"request": "マーキュリーの写真", "target": TARGET, "wants_images": True, "images_checked": True,
             "images": [{"src": "https://up/old.jpg", "level": "確認済み"}], "image_log": [{"src": "https://up/old.jpg"}],
             "image_counts": {"L2-Wikidata": 1}, "clarifications": []}
    changed = confirm_target(state, "歌手のほう")
    assert changed["images"] == [] and changed["images_checked"] is False
    assert changed["image_log"] == [] and changed["image_counts"] == {}
    kept = confirm_target(state, "はい")
    assert "images" not in kept and "images_checked" not in kept     # 元の対象でよければ画像はそのまま


# ---- 7. ステップの上限では応答で確定する ----

def test_step_limit_forces_respond_even_when_pages_are_unread():
    base = {"request": "水星", "target": TARGET, "target_summary": "概要", "candidates": [TARGET], "searched": ["水星"],
            "reading_list": ["https://a.example/1", "https://a.example/2"], "pages_read": [], "verified": []}
    for action in ("respond", "search"):
        d = check_decision({**base, "steps": MAX_STEPS}, {"action": action, "reason": "", "query": "水星 大気"})
        assert d["action"] == "respond", action
    below = check_decision({**base, "steps": MAX_STEPS - 1}, {"action": "respond", "reason": ""})
    assert below["action"] == "read_pages"                              # 上限より前なら、読むページがあれば読む


# ---- 8. 型番の部分一致で別製品を採用しない ----

def test_model_numbers_need_a_boundary():
    assert not mentions("Example EE-30 review", ["EE-3"])
    assert not mentions("Olympus Pen EE-30", ["オリンパス ペン EE-3"])
    assert not mentions("XEE-3 adapter", ["EE-3"])
    assert mentions("OLYMPUS PEN EE-3", ["Olympus PEN EE-3"])
    assert mentions("オリンパスペンEE-3の作例", ["オリンパス ペン EE-3"])
    assert mentions("ＯＬＹＭＰＵＳ　ＰＥＮ　ＥＥ－３", ["Olympus PEN EE-3"])     # 全角でも同じ


def test_engraved_text_check_uses_the_same_boundary():
    from my_llm_app.agent.nodes import Nodes
    from my_llm_app.tools.images import FetchedImage, ImageCandidate

    class VLM:
        def __init__(self, text):
            self.text = text

        def chat_json(self, system, user, schema, temperature=0.2, image_data_url=None):
            return {"context": "low", "visual": "consistent", "visible_text": self.text, "features_seen": [],
                    "description": "カメラ", "reason": ""}

    state = {"request": "EE-3", "target": {"id": "", "label": "Olympus PEN EE-3", "description": "カメラ"}}
    c = ImageCandidate(src="https://shop.example.com/a.jpg", page_url="https://shop.example.com/pen", stage="L4-ページ内")
    f = FetchedImage(ok=True, data_url="data:,", width=400, height=300)
    assert Nodes(None, VLM("OLYMPUS PEN EE-30"))._check_image(state, c, f)["text_match"] is False
    assert Nodes(None, VLM("OLYMPUS PEN EE-3"))._check_image(state, c, f)["text_match"] is True


# ---- 9. 異なる資料の URL と出典番号を区別する ----

def test_encoded_query_values_are_not_turned_into_separators():
    a, b = "https://x.example/s?q=a%26b%3Dc", "https://x.example/s?q=a&b=c"
    assert canonical(a) != canonical(b)
    n = UrlNumbers([a, b])
    assert n[a] != n[b] and len(by_tier([a, b])) == 2


def test_encoded_slash_in_path_differs_from_a_real_slash():
    assert canonical("https://x.example/a%2Fb") != canonical("https://x.example/a/b")
    assert canonical("https://x.example/a%2fb") == canonical("https://x.example/a%2Fb")


def test_unicode_spelling_and_tracking_parameters_still_merge():
    assert canonical("https://www.sw.it.aoyama.ac.jp/D%C3%BCrst/") == canonical("https://www.sw.it.aoyama.ac.jp/Dürst/")
    assert canonical("https://x.example/p?id=1&utm_source=a") == canonical("https://x.example/p?id=1")
    assert canonical("https://x.example/s?q=%E6%B0%B4%E6%98%9F") == canonical("https://x.example/s?q=水星")


# ---- 10. LLM のやり直しを 1 か所で管理する ----

def _llm_with(handler):
    import httpx

    from my_llm_app.config import Settings
    from my_llm_app.llm import LLM
    settings = Settings(llm_base_url="http://llm.test/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")
    return LLM(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _ok_body():
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "はい"}}]}


def test_llm_sends_at_most_four_times_on_repeated_transient_errors(monkeypatch):
    import httpx
    import pytest
    from openai import InternalServerError

    from my_llm_app import llm as llm_module
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)
    sent = []

    def always_503(request):
        sent.append(request)
        return httpx.Response(503, json={"error": "busy"})

    llm = _llm_with(always_503)
    with pytest.raises(InternalServerError):
        llm.chat_text("s", "u")
    assert len(sent) == 4                                # SDK のやり直しが重ならない（以前は最大 12 回）
    assert llm.waited >= 10 + 30 + 60                    # やり直す前に待った時間を数えている


def test_llm_stops_retrying_after_a_success_and_retries_429(monkeypatch):
    import httpx

    from my_llm_app import llm as llm_module
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)
    replies = [httpx.Response(429, json={"error": "rate"}), httpx.Response(200, json=_ok_body())]
    sent = []

    def handler(request):
        sent.append(request)
        return replies[min(len(sent) - 1, 1)]

    assert _llm_with(handler).chat_text("s", "u") == "はい"
    assert len(sent) == 2


def test_llm_does_not_retry_client_errors(monkeypatch):
    import httpx
    import pytest
    from openai import BadRequestError

    from my_llm_app import llm as llm_module
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)
    sent = []

    def bad(request):
        sent.append(request)
        return httpx.Response(400, json={"error": "bad"})

    with pytest.raises(BadRequestError):
        _llm_with(bad).chat_text("s", "u")
    assert len(sent) == 1


# ---- 11. 1 ページの不正な LLM 出力を、そのページの失敗に留める ----

def test_one_page_with_broken_llm_output_does_not_lose_other_pages():
    from unittest.mock import patch

    from my_llm_app.agent import prompts
    from my_llm_app.agent.nodes import Nodes
    from my_llm_app.config import Settings
    from my_llm_app.llm import LLMOutputError
    from my_llm_app.tools.fetch import Page

    good_text = "水星は太陽系で最も内側を公転する惑星である。" * 5
    pages = {"https://bad.example/1": Page("https://bad.example/1", title="壊れる", text="何か" * 80, ok=True),
             "https://good.example/2": Page("https://good.example/2", title="水星", text=good_text, ok=True)}

    class LLM:
        def chat_json(self, system, user, schema, temperature=0.2, image_data_url=None):
            if "壊れる" in user:
                raise LLMOutputError("LLM の出力が JSON になりませんでした")
            return {"facts": [{"text": "最も内側を公転する", "quote": "水星は太陽系で最も内側を公転する惑星である",
                               "aspect": "特徴", "about": prompts.ABOUT_TARGET}],
                    "other_entity": "", "features": [], "new_terms": [], "page_tier": {"adjust": "そのまま", "reason": ""}}

    state = {"request": "水星", "target": {"id": "Q308", "label": "水星", "description": "惑星"},
             "aspects": [{"name": "特徴", "words": "特徴"}], "subject_type": "自然物", "databases_checked": True,
             "reading_list": list(pages)}
    settings = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")
    with patch("my_llm_app.agent.nodes.fetch.fetch_page", side_effect=lambda u: pages[u]):
        out = Nodes(settings, LLM()).read_pages(state)
    by_url = {p["url"]: p for p in out["pages_read"]}
    assert by_url["https://bad.example/1"]["ok"] is False
    assert "JSON" in by_url["https://bad.example/1"]["reason"]
    assert by_url["https://good.example/2"]["verified"] == 1
    assert [f["source"] for f in out["verified"]] == ["https://good.example/2"]
    traced = {p["url"]: p for p in out["trace"][0]["pages"]}
    assert "JSON" in traced["https://bad.example/1"]["reason"]          # 実行記録にも理由が残る
