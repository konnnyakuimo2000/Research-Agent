"""「はやぶさについて調べて」が 2 回とも、画像を探す段階の UnicodeEncodeError で止まった（2026-11-24）ことの直し。

取得の部品が応答を作り直すとき、見出しを文字に直した形で渡していたので、日本語の入った見出し
（ファイル名の Content-Disposition など）を httpx が ASCII で書き戻せずに失敗した。並行して取っている画像の
1 枚の失敗が、そのまま調査全体を止めていた。
"""

from __future__ import annotations

from unittest.mock import patch

import httpx

from research_agent.tools import fetch, http, images

NAME = 'inline; filename="JS_Hayabusa（PG-824）.jpg"'.encode()


def client(headers):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=headers, content=b"x")))


def test_non_ascii_headers_pass_through():
    r = http._receive(client([(b"content-type", b"image/jpeg"), (b"content-disposition", NAME)]),
                      "https://upload.example.org/a.jpg", None, {}, 10, None)
    assert r.status_code == 200 and r.content == b"x"
    assert (b"content-disposition", NAME) in r.headers.raw              # 受け取ったバイトのまま
    assert "（PG-824）" in r.headers["content-disposition"]


def test_a_redirect_to_a_japanese_path_still_has_its_location():
    loc = "/wiki/はやぶさ".encode()
    r = http._receive(httpx.Client(transport=httpx.MockTransport(lambda q: httpx.Response(302, headers=[(b"location", loc)]))),
                      "https://ja.example.org/x", None, {}, 10, None)
    assert r.status_code == 302 and r.headers["location"] == "/wiki/はやぶさ"


def test_one_unexpected_failure_does_not_stop_the_run():
    with patch("research_agent.tools.images.http.get", side_effect=UnicodeEncodeError("ascii", "（", 0, 1, "x")):
        out = images.fetch_image("https://upload.example.org/a.jpg")
    assert not out.ok and "UnicodeEncodeError" in out.reason
    with patch("research_agent.tools.fetch.http.get", side_effect=RuntimeError("boom")):
        page = fetch.fetch_page("https://ja.example.org/x")
    assert not page.ok and "RuntimeError" in page.reason
