"""再レビュー（.claude/review-fixes.md）の 6: ページ内の画像は、実際の掲載元（転送先）で評価する。

Tier 1 の URL から Tier 4 のページへ転送されたとき、本文は転送先を使うのに、画像の掲載元には転送元が残り、
画像だけ Tier 1 として評価されていた。
"""

from __future__ import annotations

from unittest.mock import patch

from research_agent.agent import prompts
from research_agent.agent.nodes import Nodes, _image_lines, image_event
from research_agent.config import Settings
from research_agent.tiers import page_tier
from research_agent.tools import fetch
from research_agent.tools.images import ImageCandidate

SETTINGS = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")
ASKED, FINAL = "https://www.mext.go.jp/old/tower", "https://blog.example.com/tower"   # Tier 1 → Tier 4
TEXT = "東京タワーの高さは333メートルである。" * 10


class LLM:
    def chat_json(self, system, user, schema, temperature=0.2):
        return {"facts": [{"text": "高さは 333 m", "quote": "東京タワーの高さは333メートルである", "aspect": "高さ",
                           "about": prompts.ABOUT_TARGET}], "other_entity": ""}


def test_page_images_carry_the_final_page():
    page = fetch.Page(ASKED, title="東京タワー", text=TEXT, ok=True, final_url=FINAL,
                      images=[{"src": "https://blog.example.com/t.jpg", "alt": "東京タワー", "caption": ""}])
    state = {"request": "東京タワー", "target": {"id": "", "label": "東京タワー", "description": ""},
             "aspects": [{"name": "高さ", "words": ""}], "subject_type": "場所・建造物", "databases_checked": True,
             "reading_list": [ASKED]}
    with patch("research_agent.agent.nodes.fetch.fetch_page", return_value=page):
        out = Nodes(SETTINGS, LLM()).read_pages(state)
    image = out["page_images"][0]
    assert image["page_url"] == FINAL and image["requested_url"] == ASKED          # 元の URL は履歴として残す
    full = {**state, **out}
    assert page_tier(ASKED, full) == 1 and page_tier(FINAL, full) == 4
    c = ImageCandidate(src=image["src"], page_url=image["page_url"], stage="L4-ページ内", title=image["title"])
    assert image_event(c, full)["tier"] == 4                                        # 前は転送元の Tier 1
    line = _image_lines({"src": c.src, "page_url": c.page_url, "title": "東京タワー", "level": "おそらく正しい",
                         "description": "塔", "license": ""}, "東京タワー")[1]
    assert f"({FINAL})" in line and ASKED not in line                               # 報告の掲載元も転送先
