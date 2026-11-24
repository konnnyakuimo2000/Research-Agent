"""人物などの「氏名・別名」がいつまでも埋まらない問題の直し（2026-11-24）。

ページを読む LLM は、どのページにも書いてある名前を事実として出さない。対象についての事実が取れたページの
題名・本文に対象の名前があれば、その箇所を引用にした事実をプログラムが作る（agent/name_facts.py）。
"""

from __future__ import annotations

from unittest.mock import patch

from research_agent.agent import coverage as cov
from research_agent.agent import name_facts as nf
from research_agent.agent import prompts
from research_agent.agent.nodes import Nodes
from research_agent.agent.quotes import quote_in_text
from research_agent.config import Settings

SETTINGS = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")


def test_only_name_only_aspects_are_filled_by_the_program():
    assert nf.name_aspect(["氏名・別名", "経歴"]) == "氏名・別名"
    assert nf.name_aspect(["名称・学名", "分類"]) == "名称・学名"
    assert nf.name_aspect(["名称・別名"]) == "名称・別名"
    assert nf.name_aspect(["氏名・所属", "経歴"]) is None          # 所属は名前ではない
    assert nf.name_aspect(["名称・由来"]) is None                   # 由来は本文を読まないとわからない
    assert nf.name_aspect(["別名"]) is None                         # 頭になる語（氏名・名称など）がない


def test_name_candidates_skip_title_fragments():
    state = {"target": {"label": "チャールズ・ダーウィン"},
             "name_variants": {"Charles Darwin": 3, "チャールズ・ダーウィンとは何？ わかりやすく": 1, "ダーウィン": 2}}
    assert nf.candidates(state) == ["チャールズ・ダーウィン", "Charles Darwin", "ダーウィン"]


def test_spaces_and_readings_are_matched_and_quoted_verbatim():
    body = "教員一覧\n| 大原 剛三（オオハラ コウゾウ） | 理工学部 | 教授 |\n専門は機械学習。"
    f = nf.name_fact(body, ["大原剛三", "オオハラ コウゾウ"], "氏名・別名", "https://ex.ac.jp/s")
    assert f["text"] == "氏名は「大原 剛三」（オオハラ コウゾウ とも書く）"
    assert f["quote"] == "大原 剛三（オオハラ コウゾウ） | 理工学部 | 教授"           # 端の罫線は外す
    assert quote_in_text(f["quote"], body) and f["aspect"] == "氏名・別名" and f["origin"] == "name"


def test_the_page_title_counts_when_the_body_has_no_name():
    body = nf.source_text("研究者情報 - 大原 剛三", "| 職歴 |\n| 1996/04 | 日本学術振興会 特別研究員 |")
    f = nf.name_fact(body, ["大原剛三"], "氏名・別名", "u")
    assert f["quote"] == "研究者情報 - 大原 剛三" and quote_in_text(f["quote"], body)


def test_latin_names_in_another_order_or_case():
    f = nf.name_fact("Profile\nDÜRST, Martin J. — Professor", ["DÜRST, Martin J", "Martin J. Dürst"], "氏名・別名", "u")
    assert f["text"] == "氏名は「DÜRST, Martin J」"
    assert nf.name_fact("Martinez and Durstine", ["Martin Dürst"], "氏名・別名", "u") is None


def test_long_lines_are_cut_to_the_sentence_with_the_name():
    long = "序文。" * 30 + "1809年2月12日、チャールズ・ダーウィン（Charles Darwin）はシュルーズベリーに生まれた。父ロバートは医師で" + "あった。" * 30
    f = nf.name_fact(long, ["チャールズ・ダーウィン", "Charles Darwin"], "氏名・別名", "u")
    assert f["quote"].endswith("シュルーズベリーに生まれた。") and len(f["quote"]) <= nf.MAX_QUOTE
    assert f["text"] == "氏名は「チャールズ・ダーウィン」（Charles Darwin とも書く）"
    assert quote_in_text(f["quote"], long)


def test_no_name_no_fact():
    assert nf.name_fact("関係のない本文です。", ["大原剛三"], "氏名・別名", "u") is None
    assert nf.name_fact("大原剛三", ["大原剛三"], "氏名・別名", "u") is None     # 短すぎる引用は認めない


# ---- ページを読む処理につないだところ ----

class LLM:
    def __init__(self, facts):
        self.facts = facts

    def chat_json(self, system, user, schema, temperature=0.2):
        return {"facts": self.facts, "other_entity": ""}


PAGE = type("P", (), {"ok": True, "url": "https://ex.ac.jp/ohara", "title": "研究者情報 - 大原 剛三",
                      "text": "| 職歴 |\n| 1997/04 ～ 2007/03 | 大阪大学 産業科学研究所 助手 |"})()
STATE = {"request": "大原先生", "target": {"id": "", "label": "大原剛三", "description": ""}, "name_variants": {"オオハラ コウゾウ": 1},
         "aspects": cov.aspects_for("人物")}
CAREER = {"text": "大阪大学の助手だった", "quote": "大阪大学 産業科学研究所 助手", "aspect": "経歴", "about": prompts.ABOUT_TARGET}


def read(facts):
    with patch("research_agent.agent.nodes.fetch.fetch_page", return_value=PAGE):
        return Nodes(SETTINGS, LLM(facts))._read_one(STATE, PAGE.url)[1]


def test_a_page_with_facts_about_the_target_also_gives_the_name():
    facts = read([CAREER])
    assert [f["aspect"] for f in facts] == ["経歴", "氏名・別名"]
    assert facts[1]["quote"] == "研究者情報 - 大原 剛三"


def test_no_name_fact_from_pages_that_are_not_about_the_target():
    other = {**CAREER, "about": "別の人"}
    assert read([other]) == []                                          # 同名の別人のページから名前を拾わない


def test_the_llm_s_own_name_fact_is_not_duplicated():
    own = {"text": "氏名は大原剛三", "quote": "研究者情報 - 大原 剛三", "aspect": "氏名・別名", "about": prompts.ABOUT_TARGET}
    facts = read([CAREER, own])
    assert [f["aspect"] for f in facts].count("氏名・別名") <= 1


def test_two_sites_with_the_name_confirm_the_aspect():
    aspects = cov.aspects_for("人物")
    a = nf.name_fact("大原 剛三（オオハラ コウゾウ） 教授", ["大原剛三"], "氏名・別名", "https://www.aoyama.ac.jp/staff")
    b = nf.name_fact("研究者情報 - 大原 剛三", ["大原剛三"], "氏名・別名", "https://raweb1.jm.aoyama.ac.jp/S000272")
    row = next(r for r in cov.build_coverage(aspects, [a, b]) if r["aspect"] == "氏名・別名")
    assert row["facts"] == 2 and row["status"] != cov.MISSING


def test_the_page_s_own_alternative_names_are_used():
    state = {"target": {"label": "チャールズ・ダーウィン"}, "name_variants": {}}
    body = nf.source_text("チャールズ・ダーウィン - Wikipedia", "| チャールズ・ダーウィン Charles Robert Darwin |\n本文")
    names = nf.candidates(state, ["Charles Robert Darwin"])
    f = nf.name_fact(body, names, "氏名・別名", "u")
    assert f["text"] == "氏名は「チャールズ・ダーウィン」（Charles Robert Darwin とも書く）"   # 題名より、別名の並ぶ行
