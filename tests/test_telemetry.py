"""テレメトリーのイベント（T0）。"""

import json
from concurrent.futures import ThreadPoolExecutor
from langgraph.graph import END, START, StateGraph

from research_agent.agent import telemetry
from research_agent.agent.graph import timed
from research_agent.agent.state import AgentState
from research_agent.agent.telemetry import Emitter, Recorder


def run(node) -> list[dict]:
    g = StateGraph(AgentState)
    g.add_node("n", timed("search", node))
    g.add_edge(START, "n")
    g.add_edge("n", END)
    return [c for mode, c in g.compile().stream({}, stream_mode=["updates", "custom"]) if mode == "custom"]


def test_events_reach_the_stream_from_worker_threads():
    def node(state):
        ev = telemetry.emitter()
        ev("page.start", url="u0")
        with ThreadPoolExecutor(2) as pool:   # ページを並行して読むときと同じ
            list(pool.map(lambda i: ev("page.done", url=f"u{i}"), [1, 2]))
        return {"steps": 1}

    events = run(node)
    types = [e["type"] for e in events]
    assert types[0] == "step.start" and types[-1] == "step.end"          # ステップの始まりと終わりが対になる
    assert sorted(e.get("url") for e in events if e["type"].startswith("page.")) == ["u0", "u1", "u2"]
    assert events[-1]["node"] == "search" and "seconds" in events[-1]


def test_emitter_outside_a_graph_does_nothing():
    ev = telemetry.emitter()
    assert not ev
    ev("page.start", url="u")            # 例外にならない
    assert not Emitter()


def test_recorder_numbers_events_and_writes_jsonl(tmp_path):
    rec = Recorder("r1", tmp_path / "r1.events.jsonl")
    rec.add({"type": "run.start", "request": "水星"})
    rec.add({"type": "step.start", "node": "decide", "step": 1})
    lines = [json.loads(x) for x in (tmp_path / "r1.events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["seq"] for e in lines] == [1, 2] and lines[0]["request"] == "水星"
    assert rec.wait_after(1, timeout=0.01)[0]["type"] == "step.start"
    rec.finish()
    assert rec.wait_after(2, timeout=5) == []                           # 終わっていれば待たない
