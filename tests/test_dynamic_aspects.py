from unittest.mock import patch

from research_agent.agent import coverage as cov
from research_agent.agent.nodes import Nodes
from research_agent.agent.progress import describe
from research_agent.tools.fetch import Page


def test_llm_aspects_are_cleaned():
    raw = [{"name": "発売年", "words": "発売"}, {"name": "発売年", "words": "x"},      # 重複
           {"name": "", "words": "x"}, {"name": "その他", "words": "x"},               # 空・予約名
           {"name": "あ" * 21, "words": "x"},                                         # 長すぎる
           {"name": "レンズ", "words": ""}, {"name": "外観の特徴", "words": "外観 デザイン"}]
    aspects, origin = cov.make_aspects(raw, "製品・機械")
    assert origin == "LLM"
    assert aspects == [{"name": "発売年", "words": "発売"}, {"name": "レンズ", "words": "レンズ"},
                       {"name": "外観の特徴", "words": "外観 デザイン"}]


def test_too_few_or_broken_aspects_fall_back_to_checklist():
    for raw in (None, [], [{"name": "発売年", "words": "発売"}], ["文字列"]):
        aspects, origin = cov.make_aspects(raw, "人物")
        assert origin == "チェックリスト" and aspects == cov.aspects_for("人物")


def test_search_words_drop_guessed_numbers():
    raw = [{"name": "発売年", "words": "発売, 1966年"}, {"name": "仕様", "words": "仕様、シャッター 露出計 レンズ"},
           {"name": "外観", "words": "1973"}]
    aspects, _ = cov.make_aspects(raw, "製品・機械")
    assert [a["words"] for a in aspects] == ["発売", "仕様 シャッター 露出計", "外観"]


def test_aspects_are_capped():
    raw = [{"name": f"観点{i}", "words": "w"} for i in range(12)]
    assert len(cov.make_aspects(raw, "人物")[0]) == cov.MAX_ASPECTS


def test_type_guides_show_example_aspects():
    guides = cov.type_guides()
    assert "氏名・別名" in guides["人物"]["お手本の観点"] and guides["人物"]["説明"]


class AspectLLM:
    def chat_json(self, system, user, schema, temperature=0.2):
        if "aspects" in schema["properties"]:
            return {"subject_type": "存在しない種類", "aspects": [
                {"name": "発売年", "words": "発売"}, {"name": "レンズ", "words": "レンズ"},
                {"name": "外観の特徴", "words": "外観"}]}
        return {"facts": []}


def test_read_pages_uses_llm_aspects_and_shows_them():
    state = {"request": "EE-3 を外観の写真付きで", "target": {"id": "", "label": "EE-3", "description": "カメラ"},
             "reading_list": ["https://a.example/1"]}
    with patch("research_agent.agent.nodes.fetch.fetch_page",
               side_effect=lambda u: Page(u, title="EE-3", text="EE-3 は 1973 年発売。", ok=True)):
        out = Nodes(settings=None, llm=AspectLLM()).read_pages(state)
    assert out["subject_type"] == cov.OTHER_TYPE                       # 選択肢にない種類は「その他」に
    assert [a["name"] for a in out["aspects"]] == ["発売年", "レンズ", "外観の特徴"]
    assert [r["aspect"] for r in out["coverage"]] == ["発売年", "レンズ", "外観の特徴"]
    assert "LLMが作成" in describe("read_pages", out) and "外観の特徴" in describe("read_pages", out)
