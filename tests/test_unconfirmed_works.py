"""本人のものか確かめていない資料（著者名が同じだけ）は、報告の本文に書かず、別の節に並べる（2026-11-25「松田教授」）。

国会図書館の著者検索「松田浩一」は、ゲノム研究の松田浩一教授とは別の人の画像処理の論文も返し、報告はそれを
本人の業績として書いていた。著者名で引いた国会図書館の資料と、手掛かりのない CiNii の論文は「同定: 未確定」とし、
その事実は本文の材料にしない。
"""

from __future__ import annotations

from unittest.mock import patch

from research_agent.agent import nodes as nodes_mod
from research_agent.agent.nodes import Nodes
from research_agent.agent.report import SourceTable, collect_sources, unconfirmed_section
from research_agent.config import Settings
from research_agent.tiers import canonical
from research_agent.tools import databases

KAKEN = "https://nrid.nii.ac.jp/ja/nrid/1000090401257/"
OWN = "https://ndlsearch.ndl.go.jp/books/R000000004-I027612281"
OTHER = "https://ndlsearch.ndl.go.jp/books/R000000004-I5937190"


def state():
    return {"request": "松田教授について", "target": {"id": "Q1", "label": "松田浩一", "description": "研究者"},
            "pages": {KAKEN: "KAKEN — 松田 浩一", OWN: "国立国会図書館サーチ: 骨軟部腫瘍のゲノム解析",
                      OTHER: "国立国会図書館サーチ: 圧力エネルギーを考慮した2D Active Netモデルとその適用"},
            "verified": [{"text": "東京大学の教授", "source": KAKEN, "aspect": "所属"},
                         {"text": "『骨軟部腫瘍のゲノム解析』の著者", "source": OWN, "aspect": "業績"},
                         {"text": "論文「2D Active Netモデル」の共著者", "source": OTHER, "aspect": "業績"}],
            "db_records": {canonical(OWN): {"url": OWN, "database": "国立国会図書館サーチ", "identity": "unconfirmed"},
                           canonical(OTHER): {"url": OTHER, "database": "国立国会図書館サーチ", "identity": "unconfirmed"}},
            "aspects": [{"name": "所属"}, {"name": "業績"}], "coverage": []}


def test_ndl_author_records_are_marked_unconfirmed():
    xml = ("<rss xmlns:dc='http://purl.org/dc/elements/1.1/'><channel><item><title>x</title>"
           "<link>https://ndlsearch.ndl.go.jp/books/R1-I2</link><dc:title>本</dc:title><dc:creator>松田浩一</dc:creator>"
           "</item></channel></rss>")
    reply = type("R", (), {"content": xml.encode(), "raise_for_status": lambda self: None})()
    with patch.object(databases.http, "get", return_value=reply):
        (by_author,) = databases.ndl("松田浩一", by_creator=True)
    assert by_author.identity == "unconfirmed" and "同定: 未確定" in by_author.text


def test_unconfirmed_works_stay_out_of_the_body_and_get_their_own_section():
    s = state()
    table = SourceTable(collect_sources(s))
    section = unconfirmed_section(s, table)
    assert section.startswith("\n### 同じ名前の人の資料（本人のものか確かめていない）")
    assert "- 骨軟部腫瘍のゲノム解析 [2]" in section and "- 圧力エネルギーを考慮した2D Active Netモデルとその適用 [3]" in section
    seen = {}

    class LLM:
        def chat_text(self, system, user, **kw):
            seen["materials"] = user
            return "松田浩一は東京大学の教授です [1]。"

    n = Nodes(Settings(llm_base_url="http://llm.test/v1", llm_model="m", llm_api_key="k", searxng_url="http://s.test"), LLM())
    with patch.object(nodes_mod.telemetry, "emitter", return_value=lambda *a, **k: None):
        out = n.respond(s)
    assert "東京大学の教授" in seen["materials"] and "Active Net" not in seen["materials"] and "ゲノム解析』の著者" not in seen["materials"]
    assert "### 同じ名前の人の資料（本人のものか確かめていない）" in out["answer"]
    assert out["answer"].index("東京大学の教授です") < out["answer"].index("### 同じ名前の人の資料")
