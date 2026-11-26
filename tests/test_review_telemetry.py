"""再レビュー（.claude/review-fixes.md）の「テレメトリー表記」の受け入れテスト 1〜5。

画面の言葉を、実際に測っていることに合わせ、流れの図・木・詳細が同じ集計（extensions/research-v1/summary.js）から
同じ件数・単位・言葉を出す。画面は Node で本物のモジュールを動かして確かめる（tests/ui_harness.py）。
"""

from __future__ import annotations

import json
from unittest.mock import patch

from research_agent.agent import prompts
from research_agent.agent.nodes import Nodes
from research_agent.config import Settings
from ui_harness import UI, run_js

SETTINGS = Settings(llm_base_url="http://unused/v1", llm_model="m", llm_api_key="k", searxng_url="http://unused")

# 両方の画面を、同じイベントで動かす道具（流れの図と木の文字を読む）
SETUP = """
  import { ctx } from 'UI/core/ctx.ts';
  import { dispatch, norm } from 'UI/core/events.ts';
  import { tree } from 'UI/views/tree.js';
  await import('UI/extensions/research-v1/index.js');
  const { ResearchFlow } = await import('UI/extensions/research-v1/flow.js');
  const { S } = await import('UI/extensions/research-v1/tree.js');
  ctx.AGENTS = { research: { ok: true, levels: [0, 1], extensions: ["research/v1"] } }; ctx.runAgent = "research";
  ctx.instant = true; ctx.runId = "r1";
  // 文字を読めるように、木と流れの図の setText・setSub を記録する
  const texts = {};
  const keep = (id, t, s) => { texts[id] = [t ?? texts[id]?.[0], s ?? texts[id]?.[1]]; };
  const flow = new ResearchFlow("research"); ctx.flow = flow;
  const origText = flow.setText.bind(flow); flow.setText = (id, t, s) => { keep("flow:" + id, t, s); origText(id, t, s); };
  const origSub = tree.setSub.bind(tree); tree.setSub = (id, s) => { keep("tree:" + id, undefined, s); origSub(id, s); };
  const send = (type, data = {}, t = 0) => { const e = norm({ v: 1, seq: ++send.n, t, type, data }); dispatch(e); flow.on(e); };
  send.n = 0;
"""


def run(body: str) -> dict:
    return run_js(SETUP + body)


# ---- 1. 引用と主張 ----

def test_a_matching_quote_is_shown_as_a_quote_match_not_a_checked_number():
    page = type("P", (), {"ok": True, "url": "https://ex.org/b", "title": "建物", "text": "建物の高さは十メートル。屋上がある。"})()

    class LLM:
        def chat_json(self, system, user, schema, temperature=0.2):
            return {"facts": [{"text": "高さは九十九メートル", "quote": "建物の高さは十メートル", "aspect": "高さ",
                               "about": prompts.ABOUT_TARGET}], "other_entity": ""}

    state = {"request": "建物", "aspects": [{"name": "高さ", "words": ""}]}
    with patch("research_agent.agent.nodes.fetch.fetch_page", return_value=page):
        facts = Nodes(SETTINGS, LLM())._read_one(state, page.url)[1]
    assert [f["text"] for f in facts] == ["高さは九十九メートル"]          # 今の抽出処理は採用する（引用は本文にある）
    out = run(f"""
      import {{ showDetails }} from 'UI/panels/details.js';
      send("run.start", {{ request: "建物" }});
      send("step.start", {{ node: "read_pages", step: 1 }});
      send("research/v1:page.planned", {{ url: "https://ex.org/b", host: "ex.org", tier: 4 }});
      send("research/v1:page.start", {{ url: "https://ex.org/b", host: "ex.org", tier: 4 }});
      send("research/v1:page.fetched", {{ url: "https://ex.org/b", ok: true, chars: 20, title: "建物" }});
      send("research/v1:page.done", {{ url: "https://ex.org/b", tier: 4, base_tier: 4, facts: {json.dumps(facts, ensure_ascii=False)}, dropped: [] }});
      showDetails(tree.info["p:https://ex.org/b"]);
      const html = document.getElementById("dBody").innerHTML;
      out({{ sub: texts["tree:p:https://ex.org/b"][1], html, note: tree.info["p:https://ex.org/b"].note }});
    """)
    assert out["sub"] == "原文に引用が見つかった記述 1 件"
    assert "原文に引用が見つかった記述 1 件" in out["html"] and "「建物の高さは十メートル」" in out["html"]
    assert "数値や否定の意味までは確かめていない" in out["note"]
    for word in ("照合できた事実", "確認した", "確認済み"):
        assert word not in out["sub"] + out["html"]


# ---- 2. 観点と矛盾 ----

def test_meeting_the_source_rule_and_a_conflict_are_both_visible():
    out = run("""
      send("run.start", { request: "塔" });
      send("step.start", { node: "read_pages", step: 1 });
      send("research/v1:aspects", { aspects: ["高さ"], subject_type: "建造物", origin: "LLM" });
      send("research/v1:conflicts", { aspects: ["高さ"], conflicts: [{ aspect: "高さ", topic: "高さ",
        facts: [{ text: "10m", source: "https://a.example/" }, { text: "99m", source: "https://b.example/" }] }] });
      send("research/v1:coverage", { rows: [{ aspect: "高さ", facts: 2, status: "確認済み", best_tier: 2, independent: 2 }] });
      out({ treeCov: texts["tree:cov"][1], flowCov: texts["flow:cov"][1], aspect: texts["tree:a:高さ"][1] });
    """)
    assert out["treeCov"] == out["flowCov"] == "出典条件を満たした観点 1/1 · 食い違い候補 1 件"
    assert out["aspect"] == "出典条件を満たす（別サイト 2） · 食い違い候補あり"


# ---- 3. 判断と画像 ----

def test_a_query_only_correction_reads_the_same_in_both_views():
    out = run("""
      send("run.start", { request: "塔" });
      send("step.start", { node: "decide", step: 1 });
      send("research/v1:decision", { llm: { action: "search", query: "東京タワ" }, action: "search", query: "東京タワー",
        guard: "名前の綴りを直した（東京タワ→東京タワー）" });
      const d = tree.info["d1"].kv.reduce((m, [k, v]) => ({ ...m, [k]: v }), {});
      out({ flow: texts["flow:hub"], tree: d });
    """)
    assert out["flow"][1] == "検索語を補正（提案: 検索「東京タワ」）"
    assert out["tree"]["変更"] == "検索語を補正" and out["tree"]["採用した行動"] == "検索「東京タワー」"
    assert out["tree"]["モデルの提案"] == "検索「東京タワ」" and "綴り" in out["tree"]["変更理由"]


def images_body(fetched: int, judged: list[str], shown: list[str], reference: list[str] = ()) -> str:
    lines = ['send("run.start", { request: "塔" });', 'send("step.start", { node: "find_images", step: 1 });']
    for i in range(fetched):
        lines.append(f'send("research/v1:image.fetched", {{ src: "s{i}", ok: true, host: "ex.org", tier: 2, stage: "L1" }});')
    for r in reference:
        lines.append(f'send("research/v1:image.fetched", {{ src: "{r}", ok: true, host: "ex.org", tier: 2, stage: "L5", related: "上位" }});')
        lines.append(f'send("research/v1:image.done", {{ src: "{r}", level: "参考画像" }});')
    for s in judged:
        lines.append(f'send("research/v1:image.llm", {{ src: "{s}" }});')
        lines.append(f'send("research/v1:image.done", {{ src: "{s}", level: "確認済み", description: "塔" }});')
    lines.append(f'send("research/v1:image.final", {{ shown: {json.dumps(list(shown))}, withheld: [] }});')
    lines.append('out({ tree: texts["tree:w1"][1], flow: texts["flow:imgs"]?.[1] ?? texts["flow:images"][1], counts: S.summary.imageCounts() });')
    return "\n".join(lines)


def test_both_views_count_images_the_same_way():
    out = run(images_body(19, ["s0", "s1"], ["s0", "s1"]))
    assert out["tree"] == out["flow"] == "取得成功 19 · 画像モデルで判定 2 · 報告に掲載 2"
    ref = run(images_body(0, [], ["r1"], reference=["r1"]))
    assert ref["counts"] == {"fetched": 1, "judged": 0, "reference": 1, "shown": 1}   # 参考画像は判定に数えない
    assert ref["tree"] == ref["flow"] == "取得成功 1 · 画像モデルで判定 0 · 参考画像 1 · 報告に掲載 1"


# ---- 4. 取得と描画 ----

def test_extraction_failures_processing_and_zero_results():
    out = run("""
      send("run.start", { request: "塔" });
      send("step.start", { node: "read_pages", step: 1 });
      send("research/v1:page.planned", { url: "https://ex.org/p", host: "ex.org", tier: 2 });
      send("research/v1:page.start", { url: "https://ex.org/p", host: "ex.org", tier: 2 });
      send("research/v1:page.fetched", { url: "https://ex.org/p", ok: true, chars: 500, title: "p" });
      send("research/v1:page.done", { url: "https://ex.org/p", error: "LLM の出力を JSON として読めなかった", facts: [], dropped: [] });
      const page = texts["tree:p:https://ex.org/p"][1];
      send("step.start", { node: "search", step: 2 });
      send("research/v1:search.start", { query: "塔", kind: "free" });
      send("research/v1:search.done", { results: [{ title: "x", url: "https://y/", host: "y", relevant: false, tier: 4 }], candidates: [] });
      const particles = flow.particles.length;
      send("step.start", { node: "report_infeasible", step: 3 });
      out({ page, particles, summary: S.summary.pageText(), stage: S.summary.pages.get("https://ex.org/p").stage,
            infeasibleClass: flow.n.infeasible.el.className });
    """)
    assert out["page"].startswith("本文取得済み・抽出失敗")
    assert out["stage"] == "本文取得済み・抽出失敗" and out["summary"] == "対象資料 1 · 本文取得 1 · 抽出 0 · 引用一致 0 件"
    assert out["particles"] == 0                                            # 関係する結果 0 件 → 粒も 0 個
    assert "running" in out["infeasibleClass"] and "llm" not in out["infeasibleClass"]   # 定型文の工程は「処理中」


# ---- 5. 時刻と履歴の操作 ----

def test_replay_speed_does_not_change_counts_and_history_is_read_only():
    events = [("run.start", {"request": "塔"}, 0), ("step.start", {"node": "read_pages", "step": 1}, 1),
              ("research/v1:page.planned", {"url": "https://ex.org/a", "host": "ex.org", "tier": 2}, 2),
              ("research/v1:page.done", {"url": "https://ex.org/a", "tier": 2, "base_tier": 2,
                                         "facts": [{"text": "a", "quote": "q", "aspect": "x"}], "dropped": []}, 3),
              ("step.end", {"node": "read_pages", "step": 1, "seconds": 2.5, "llm_wait": 1.0}, 4),
              ("run.end", {"report": "r"}, 40)]
    sends = "\n".join(f'send({json.dumps(t)}, {json.dumps(d, ensure_ascii=False)}, {tt});' for t, d, tt in events)
    a = run(sends + 'out({ p: S.summary.pageText(), e: S.summary.elapsed, s: S.summary.stageSeconds, r: S.summary.retrySeconds });')
    b = run(sends.replace("ctx.instant = true", "") + 'out({ p: S.summary.pageText(), e: S.summary.elapsed, s: S.summary.stageSeconds, r: S.summary.retrySeconds });')
    assert a == b and a["e"] == 40 and a["s"] == {"read_pages": 2.5} and a["r"] == 1.0
    q = "どれのことですか？\\n1. 塔\\n2. 山\\n番号か、具体的な内容で答えてください。上にない場合はその内容を書いてください。"
    out = run(f"""
      import {{ hooks }} from 'UI/panels/chat.js';
      import {{ live }} from 'UI/stores/state.ts';
      const sent = [], composer = [];
      let was = null;
      ctx.runId = "r1";
      hooks.sendAnswer = (a) => sent.push(a);
      live.subscribe((l) => {{ if (l.waitingRun !== was) {{ was = l.waitingRun; composer.push(!!was); }} }});   // 入力欄が答えの形に変わったとき
      ctx.replay = true;
      send("question", {{ question: "{q}", choices: true }});
      send("run.waiting", {{ question: "{q}" }});
      const replayComposer = [...composer];
      ctx.replay = false;
      send("question", {{ question: "{q}x", choices: true }});
      send("run.waiting", {{ question: "{q}x" }});
      out({{ replayComposer, liveComposer: composer.slice(replayComposer.length), sent }});
    """)
    assert out["replayComposer"] == [] and out["liveComposer"] == [True] and out["sent"] == []


def test_the_screen_no_longer_uses_the_old_words():
    old = ("照合できた事実", "観点を確認済み", "LLM の選択どおり", "確かめた事実だけで報告", "捨てた事実")
    for p in [*UI.glob("extensions/research-v1/*.js"), UI / "panels" / "details.js", UI / "views" / "flow.js"]:
        text = p.read_text(encoding="utf-8")
        for word in old:
            assert word not in text, (p.name, word)
    html = (UI.parent / "index.html").read_text(encoding="utf-8")
    assert "工程別の流れ" in html and "判断ごとの履歴" in html and "調査の作業記録" in html   # 説明（title）として残す
    assert ">Flow</button>" in html and ">Tree</button>" in html                               # 切り替えの文字は英語
