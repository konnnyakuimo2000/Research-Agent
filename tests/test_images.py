import io
from unittest.mock import MagicMock, patch

from PIL import Image

from my_llm_app.agent import imaging
from my_llm_app.agent.guard import check_decision
from my_llm_app.agent.nodes import Nodes, images_section
from my_llm_app.config import Settings
from my_llm_app.tools import images
from my_llm_app.tools.images import ImageCandidate, fetch_image, page_images

SETTINGS = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")


def test_wants_images():
    assert imaging.wants_images("マーキュリーについて調べて、画像もつけて")
    assert imaging.wants_images("当時の写真がほしい")
    assert not imaging.wants_images("富士山について調べて")
    assert not imaging.wants_images("国立国会図書館について調べて")      # 「図」だけでは画像の依頼としない


def test_judge_table():
    j = imaging.judge
    assert j(2, True, "high", "consistent") == imaging.CONFIRMED
    assert j(1, True, "high", "unknown") == imaging.CONFIRMED
    assert j(4, True, "high", "consistent") == imaging.PROBABLE      # 文脈は確かだが掲載元が一般サイト
    assert j(4, True, "high", "unknown") == imaging.REJECTED         # 一般サイトで、写っているものが確かめられない
    assert j(2, True, "mid", "consistent") == imaging.PROBABLE       # 掲載元は確かだが文脈が弱い
    assert j(4, True, "mid", "consistent") == imaging.REJECTED
    assert j(1, True, "high", "contradict") == imaging.REJECTED      # 見た目が事実と矛盾
    assert j(1, True, "low", "consistent") == imaging.REJECTED
    assert j(1, False, "high", "consistent") == imaging.REJECTED     # 取得できない・小さすぎる
    assert j(5, True, "high", "consistent") == imaging.REJECTED      # Tier 5


def test_rank_puts_wikidata_first_and_drops_tier5_and_duplicates():
    c = [
        ImageCandidate("https://img/blog.jpg", "https://blog.example.com/p", "L1-画像検索", title="富士山の写真"),
        ImageCandidate("https://img/pin.jpg", "https://www.pinterest.jp/pin/1", "L1-画像検索", title="富士山"),
        ImageCandidate("https://img/gov.jpg", "https://www.jma.go.jp/fuji", "L4-ページ内", title="気象庁", caption="富士山"),
        ImageCandidate("https://img/wd.jpg", "https://commons.wikimedia.org/wiki/File:x.jpg", "L2-Wikidata"),
        ImageCandidate("https://img/blog.jpg", "https://blog.example.com/p", "L1-画像検索"),
    ]
    assert [x.src for x in imaging.rank(c, ["富士山"])] == ["https://img/wd.jpg", "https://img/gov.jpg",
                                                            "https://img/blog.jpg"]


def test_page_images_keeps_captioned_images_and_skips_icons():
    html = """<html><body>
      <img src="/static/logo.png" alt="ロゴ">
      <img src="/img/small.jpg" width="50" height="50">
      <figure><img src="/img/fuji.jpg" alt="富士山" width="800"><figcaption>精進湖から見た富士山</figcaption></figure>
      <img src="data:image/png;base64,xxx">
    </body></html>"""
    out = page_images(html, "https://example.go.jp/page/")
    assert out == [{"src": "https://example.go.jp/img/fuji.jpg", "alt": "富士山", "caption": "精進湖から見た富士山"}]


def _image_response(size):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="JPEG")
    resp = MagicMock(status_code=200, content=buf.getvalue(), headers={"content-type": "image/jpeg"})
    return resp


def test_fetch_image_checks_size_and_shrinks():
    with patch("my_llm_app.tools.images.httpx.get", return_value=_image_response((1600, 1200))):
        f = fetch_image("https://x/a.jpg")
    assert f.ok and (f.width, f.height) == (1600, 1200) and f.data_url.startswith("data:image/jpeg;base64,")
    with patch("my_llm_app.tools.images.httpx.get", return_value=_image_response((120, 90))):
        assert fetch_image("https://x/b.jpg").reason.startswith("小さすぎる")


class FakeVLM:
    """画像の URL に応じて判定を返す。"""
    def chat_json(self, system, user, schema, temperature=0.2, image_data_url=None):
        assert image_data_url                             # 画像を添えて呼んでいる
        if "wikidata" in user or "Wikidata" in user:
            return {"context": "high", "visual": "consistent", "description": "富士山", "reason": "代表画像"}
        return {"context": "low", "visual": "contradict", "description": "北斎の浮世絵", "reason": "別の物"}


def test_find_images_collects_checks_and_judges():
    state = {"request": "富士山について調べて。写真も", "target": {"id": "Q39231", "label": "富士山",
             "description": "山"}, "page_images": [], "verified": []}
    wd = ImageCandidate("https://up/wd.jpg", "https://commons.wikimedia.org/wiki/File:a.jpg", "L2-Wikidata",
                        title="File:a.jpg", caption="Wikidata の項目「富士山」の代表画像", license="CC BY-SA 3.0")
    cm = ImageCandidate("https://up/wave.jpg", "https://commons.wikimedia.org/wiki/File:b.jpg", "L2-Commons",
                        title="File:wave.jpg", caption="浮世絵")
    with patch.object(images, "wikidata_image", return_value=[wd]), \
         patch.object(images, "commons_search", return_value=[cm]), \
         patch.object(images, "searxng_images", side_effect=RuntimeError("down")), \
         patch.object(images, "fetch_image", return_value=images.FetchedImage(True, data_url="data:x", width=800,
                                                                              height=600)):
        out = Nodes(SETTINGS, FakeVLM()).find_images(state)
    assert out["images_checked"] is True
    assert [(i["src"], i["level"]) for i in out["images"]] == [("https://up/wd.jpg", imaging.CONFIRMED)]
    assert [r["level"] for r in out["image_log"]] == [imaging.CONFIRMED, imaging.REJECTED]
    assert out["image_counts"] == {"L2-Wikidata": 1, "L2-Commons": 1, "L1-画像検索": 0, "L4-ページ内": 0}
    assert any("L1-画像検索" in f for f in out["failures"])      # 使えなかった方法は申告する


def test_images_section_found_and_not_found():
    found = images_section({"images_checked": True, "images": [
        {"src": "https://up/wd.jpg", "page_url": "https://commons.wikimedia.org/wiki/File:a.jpg", "tier": 2,
         "title": "File:a.jpg", "license": "CC BY-SA 3.0", "level": imaging.CONFIRMED, "description": "富士山"}]})
    assert "![富士山](https://up/wd.jpg)" in found and "ライセンス: CC BY-SA 3.0" in found
    none = images_section({"images_checked": True, "images": [], "image_counts": {"L2-Commons": 3, "L1-画像検索": 5},
                           "image_log": [{"reason": "別の物"}, {"reason": "小さすぎる（100×80）"}]})
    assert "見つかりませんでした" in none
    assert "L2-Commons 3 件、L1-画像検索 5 件 の候補から 2 件を確かめ、採用 0 件" in none
    assert "別の物／小さすぎる（100×80）" in none


def test_guard_finds_images_before_responding_only_when_asked():
    target = {"id": "Q1", "label": "富士山", "description": "山", "sitelinks": 146}
    base = {"request": "富士山の写真", "target": target, "target_summary": "概要", "candidates": [target],
            "searched": ["富士山"], "steps": 4, "reading_list": ["u1"], "pages_read": [{"url": "u1"}],
            "verified": [{"text": "t", "quote": "q", "aspect": "x", "source": "u1"}], "conflicts_checked_at": 1}
    assert check_decision({**base, "wants_images": True}, {"action": "respond", "reason": ""})["action"] == "find_images"
    assert check_decision({**base, "wants_images": True, "images_checked": True},
                          {"action": "respond", "reason": ""})["action"] == "respond"
    assert check_decision(base, {"action": "respond", "reason": ""})["action"] == "respond"


def test_image_key_merges_thumbnail_sizes_and_query_strings():
    from my_llm_app.tools.images import image_key
    a = "https://upload.wikimedia.org/wikipedia/commons/thumb/9/93/Mercury_Records.JPG/250px-Mercury_Records.JPG?utm=x"
    b = "https://upload.wikimedia.org/wikipedia/commons/9/93/Mercury_Records.JPG"
    assert image_key(a) == image_key(b)
    assert image_key("https://example.com/a.jpg?x=1") == image_key("https://example.com/a.jpg")
    assert image_key("https://example.com/a.jpg") != image_key("https://other.example.com/a.jpg")
