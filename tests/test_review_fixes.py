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


# ---- 1. 公開 URL の取得先を制限する ----

def test_public_fetch_allows_public_and_refuses_private_destinations(monkeypatch):
    import httpx
    import pytest

    import http_mock
    from my_llm_app.tools import http
    sent = http_mock.install(monkeypatch, lambda r: httpx.Response(200, text="ok"),
                             ips={"intra.example": "10.0.0.5", "meta.example": "169.254.169.254", "v6.example": "::1"})
    assert http.get("https://pub.example/a").text == "ok"
    for url in ("http://intra.example/admin", "http://meta.example/latest/meta-data", "http://v6.example/",
                "file:///etc/passwd", "ftp://pub.example/x"):
        with pytest.raises(http.BlockedDestination):
            http.get(url)
    assert [str(r.url) for r in sent] == ["https://pub.example/a"]      # 拒否した宛先には 1 回も送っていない


def test_redirects_are_followed_one_by_one_with_the_same_check(monkeypatch):
    import httpx
    import pytest

    import http_mock
    from my_llm_app.tools import http

    def handler(request):
        if request.url.host == "pub.example" and request.url.path == "/to-public":
            return httpx.Response(302, headers={"location": "https://other.example/page"})
        if request.url.host == "pub.example" and request.url.path == "/to-private":
            return httpx.Response(302, headers={"location": "http://intra.example/secret"})
        return httpx.Response(200, text=f"from {request.url.host}")

    sent = http_mock.install(monkeypatch, handler, ips={"intra.example": "192.168.1.10"})
    r = http.get("https://pub.example/to-public")
    assert r.text == "from other.example" and str(r.url) == "https://other.example/page"   # 最終の URL がわかる
    sent.clear()
    with pytest.raises(http.BlockedDestination):
        http.get("https://pub.example/to-private")
    assert [r.url.host for r in sent] == ["pub.example"]                # 転送先（内部）には送っていない


def test_connection_is_made_to_the_checked_address_only(monkeypatch):
    import httpcore
    import pytest

    from my_llm_app.tools import http
    monkeypatch.setattr(http, "resolve", lambda host, port: {"pub.example": ["93.184.216.34"],
                                                             "mixed.example": ["93.184.216.34", "10.1.2.3"]}[host])
    connected = []
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", lambda self, host, port, *a, **k: connected.append(host) or "stream")
    backend = http.PublicOnlyBackend()
    assert backend.connect_tcp("pub.example", 443) == "stream"
    assert connected == ["93.184.216.34"]                               # 名前を引き直さず、確かめた IP につなぐ
    with pytest.raises(http.BlockedDestination):
        backend.connect_tcp("mixed.example", 443)                       # 内部のアドレスが混じる名前は信じない
    assert http.is_public_ip("8.8.8.8") and not http.is_public_ip("127.0.0.1")
    assert not http.is_public_ip("100.64.0.1") and not http.is_public_ip("::ffff:10.0.0.1")


def test_internal_services_and_proxy_settings(monkeypatch):
    import httpx

    import http_mock
    from my_llm_app.tools import http
    http_mock.install(monkeypatch, lambda r: httpx.Response(200, json={"results": []}), ips={"searxng": "172.18.0.3"})
    assert http.get("http://searxng:8080/search", public=False).status_code == 200   # 設定済みの内部サービスは従来どおり
    assert http._public_client().trust_env is False                     # 環境変数のプロキシ設定で迂回しない


# ---- 2. HTML と画像のサイズ上限を受信中に当てる ----

def test_size_limit_is_applied_while_receiving(monkeypatch):
    import httpx
    import pytest

    import http_mock
    from my_llm_app.tools import http
    pulled = []

    def chunks():
        for i in range(10):
            pulled.append(i)
            yield b"x" * 1000

    http_mock.install(monkeypatch, lambda r: httpx.Response(200, content=chunks()))   # Content-Length なし
    assert len(http.get("https://pub.example/exact", max_bytes=10_000).content) == 10_000   # ちょうどは通る
    pulled.clear()
    with pytest.raises(http.ResponseTooLarge):
        http.get("https://pub.example/big", max_bytes=2_500)
    assert len(pulled) == 3                                             # 超えた時点でやめ、残りは読まない
    assert not any("big" in k for k in http._cache)                     # 途中までの応答は覚えない
    with pytest.raises(http.ResponseTooLarge):
        http.get("https://pub.example/exact", max_bytes=5_000)           # 覚えた応答にも呼び出し側の上限が効く


def test_too_large_page_and_image_are_one_candidate_failures(monkeypatch):
    import httpx

    import http_mock
    from my_llm_app.tools import fetch, images
    monkeypatch.setattr(fetch, "MAX_BYTES", 1000)
    monkeypatch.setattr(images, "MAX_BYTES", 1000)
    http_mock.install(monkeypatch, lambda r: httpx.Response(200, headers={"content-type": "text/html"},
                                                            content=iter([b"<p>" + b"a" * 600, b"b" * 600])))
    assert fetch.fetch_page("https://pub.example/huge").reason == "大きすぎる"
    assert images.fetch_image("https://pub.example/huge.jpg").reason == "大きすぎる"


# ---- 3. 本文の発行元と Tier を転送先に合わせる ----

ARTICLE = "<html><head><title>水星 - Wikipedia</title></head><body><article><p>" + "水星は太陽系で最も内側を公転する惑星である。" * 8 + "</p></article></body></html>"


def _redirecting(monkeypatch, target):
    import httpx

    import http_mock

    def handler(request):
        if request.url.host == "blog.example.com":
            return httpx.Response(301, headers={"location": target})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=ARTICLE)
    return http_mock.install(monkeypatch, handler)


def test_redirected_page_is_cited_and_tiered_as_its_final_url(monkeypatch):
    from my_llm_app.agent import prompts
    from my_llm_app.agent.nodes import Nodes
    from my_llm_app.config import Settings
    final = "https://ja.wikipedia.org/wiki/%E6%B0%B4%E6%98%9F"     # Location は符号化した形で届く
    sent = _redirecting(monkeypatch, final)

    class LLM:
        def chat_json(self, system, user, schema, temperature=0.2, image_data_url=None):
            return {"facts": [{"text": "最も内側を公転する", "quote": "水星は太陽系で最も内側を公転する惑星である",
                               "aspect": "特徴", "about": prompts.ABOUT_TARGET}],
                    "other_entity": "", "features": [], "new_terms": [], "page_tier": {"adjust": "そのまま", "reason": ""}}

    settings = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")
    state = {"request": "水星", "target": TARGET, "aspects": [{"name": "特徴", "words": "特徴"}], "subject_type": "自然物",
             "databases_checked": True, "reading_list": ["https://blog.example.com/mercury"]}
    out = Nodes(settings, LLM()).read_pages(state)
    assert [f["source"] for f in out["verified"]] == [final]            # 出典は本文の発行元
    assert out["coverage"][0]["best_tier"] == 2                         # Tier も転送先（Wikipedia）で付く
    assert out["pages_read"][0]["final_url"] == final
    # 同じ転送元が再び候補に入っても、読み直さない
    sent.clear()
    again = Nodes(settings, LLM()).read_pages({**state, **out, "reading_list": ["https://blog.example.com/mercury", final]})
    assert sent == [] and again["pages_read"] == out["pages_read"]


def test_redirect_to_an_excluded_site_is_not_used(monkeypatch):
    from my_llm_app.tools import fetch
    _redirecting(monkeypatch, "https://www.pinterest.jp/pin/1")
    page = fetch.fetch_page("https://blog.example.com/mercury")
    assert not page.ok and "転送先" in page.reason and page.final_url == "https://www.pinterest.jp/pin/1"
