"""再レビュー（.claude/review-fixes.md）の 1: 出典の URL と番号から、正しい資料を開く。

- 過去の報告の「https://example.org/p（Tier 1）」で、リンク先に「（Tier」が入っていた
- 画面の URL の比べ方がクエリを丸ごと捨てていたので、?id=1 と ?id=2 を同じ資料とみなし、2 番で 1 番を開いた
- 成果物の出典 ID を URL のそのままの一致で作っていたので、表記が違うだけの同じ資料で ID が抜けた
画面は Node で本物のモジュールを動かして確かめる（tests/ui_harness.py）。
"""

from __future__ import annotations

from research_agent.agents.research import result_of
from research_agent.tiers import canonical
from ui_harness import run_js

P1, P2 = "https://ex.org/p?id=1", "https://ex.org/p?id=2"


def test_links_in_old_reports_stop_before_the_tier_note():
    html = run_js("""
      import { renderReport } from 'UI/panels/chat.js';
      out(renderReport("[1] 資料 — https://example.org/p（Tier 1）\\n画像の出典: [東京タワー](https://example.org/t?a=1&b=2)"));
    """)
    assert 'href="https://example.org/p"' in html and "（Tier 1）" in html
    assert 'href="https://example.org/t?a=1&amp;b=2"' in html and ">東京タワー</a>" in html
    assert html.count("<a ") == 2                                          # リンクの中をもう一度リンクにしない


def test_the_screen_compares_urls_like_the_server():
    same = run_js(f"""
      import {{ sameUrl, canonicalUrl }} from 'UI/core/util.ts';
      out([sameUrl("{P1}", "{P2}"), sameUrl("{P1}", "{P1}&utm_source=x#top"),
           sameUrl("https://ex.org/a;jsessionid=AB12/b", "https://ex.org/a/b"),
           sameUrl("https://ex.org/D%C3%BCrst/", "https://ex.org/Dürst/"),
           canonicalUrl("https://EX.org/p?utm_medium=y&id=3")]);
    """)
    assert same == [False, True, True, True, "https://ex.org/p?id=3"]
    assert canonical(P1) != canonical(P2) and canonical(P1) == canonical(P1 + "&utm_source=x#top")


def test_citation_two_opens_the_second_page():
    picked = run_js(f"""
      import {{ ctx }} from 'UI/core/ctx.ts';
      import {{ active }} from 'UI/core/events.ts';
      import {{ tree }} from 'UI/views/tree.js';
      import {{ treeRenderers }} from 'UI/extensions/research-v1/tree.js';
      await import('UI/extensions/research-v1/index.js');
      ctx.AGENTS = {{ research: {{ ok: true, extensions: ["research/v1"] }} }}; ctx.runAgent = "research"; ctx.view = "flow";
      const node = (id) => ({{ id, el: document.createElement("div"), kids: [], tgt: {{ x: 0, y: 0 }}, extra: [] }});
      for (const u of ["{P1}", "{P2}"]) {{ tree.N["p:" + u] = node("p:" + u); tree.order.push("p:" + u); tree.info["p:" + u] = {{ title: u }}; }}
      treeRenderers.report({{ sources: [{{ url: "{P1}", title: "1" }}, {{ url: "{P2}", title: "2" }}] }});
      active()[0].cite(2);
      const second = tree.selected;
      active()[0].cite(1);
      out([second, tree.selected]);
    """)
    assert picked == ["p:" + P2, "p:" + P1]


def test_artifact_source_ids_follow_the_report_numbers():
    state = {"pages": {P1: "一", P2: "二"}, "answer": "本文 [1][2]",
             "verified": [{"text": "a", "quote": "q", "aspect": "x", "source": P1},
                          {"text": "b", "quote": "q", "aspect": "x", "source": P2},
                          {"text": "c", "quote": "q", "aspect": "x", "source": P1 + "&utm_source=news"}],
             "images": [{"src": "i.jpg", "page_url": P2 + "#top", "level": "確認済み"}]}
    arts = {a["kind"].split(":")[1]: a["data"] for a in result_of(state)["artifacts"]}
    assert [(s["id"], s["url"]) for s in arts["sources"]] == [("s1", P1), ("s2", P2)]
    assert [f["source_id"] for f in arts["facts"]] == ["s1", "s2", "s1"]     # 追跡用のパラメータ違いは同じ資料
    assert arts["images"][0]["source_id"] == "s2"


def test_the_report_and_the_artifacts_number_sources_from_one_table():
    from unittest.mock import patch

    from research_agent.agent.nodes import Nodes

    class Writer:
        def chat_text(self, system, user, temperature=0.5):
            return "本文 [1][2]。"

    sent = {}
    state = {"request": "x", "pages": {P1: "一", P2: "二"},
             "verified": [{"text": "a", "quote": "q", "aspect": "x", "source": P2},
                          {"text": "b", "quote": "q", "aspect": "x", "source": P1 + "&utm_source=x"}]}
    with patch("research_agent.agent.nodes.telemetry.emitter",
               return_value=lambda kind, **data: sent.setdefault(kind, data)):
        answer = Nodes(None, Writer()).respond(state)["answer"]
    report_ids = {s["url"]: s["id"] for s in sent["report"]["sources"]}
    arts = {a["kind"].split(":")[1]: a["data"] for a in result_of({**state, "answer": answer})["artifacts"]}
    assert {s["url"]: s["id"] for s in arts["sources"]} == report_ids == {P2: "s1", P1 + "&utm_source=x": "s2"}
    assert f"[1] 二 — {P2}" in answer and "[2]" in answer                 # 一覧の番号も同じ表から
    assert [f["source_id"] for f in arts["facts"]] == ["s1", "s2"]
