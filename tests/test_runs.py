"""エージェント側の依頼の実行（agentkit.runs と調査エージェント）。グラフは偽物に差し替える。"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from langgraph.types import Command

from research_agent.agentkit import protocol
from research_agent.agentkit.runs import StateError
from research_agent.agents import research
from research_agent.agents.research import ResearchRuns


class FakeGraph:
    """1 回目は質問で止まり、答えで再開すると報告して終わる。"""

    def __init__(self):
        self.answered = None
        self.checkpointer = MagicMock()

    def stream(self, payload, config, stream_mode):
        if isinstance(payload, Command):
            self.answered = payload.resume
            yield "custom", {"type": "answer", "answer": payload.resume, "wall": time.time()}
            yield "updates", {"respond": {"answer": "報告"}}
            return
        yield "custom", {"type": "step.start", "node": "ask_user", "step": 1, "wall": time.time()}
        yield "custom", {"type": "page.done", "url": "https://example.org/", "wall": time.time()}
        yield "updates", {"__interrupt__": [SimpleNamespace(value={"question": "どれですか？\n1. 水星\n2. 歌手"})]}

    def get_state(self, config):
        return SimpleNamespace(values={"answer": "水星は太陽系の惑星です。", "verified": [], "coverage": []})


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def types(run):
    return [e["type"] for e in run.recorder.events]


def test_run_waits_for_answer_then_finishes(tmp_path):
    g = FakeGraph()
    m = ResearchRuns(g, tmp_path)
    run = m.start({"text": "マーキュリーについて"})
    assert wait_for(lambda: run.status == "waiting")
    assert run.question.startswith("どれですか")
    g.checkpointer.delete_thread.assert_not_called()                  # 答えを待っている間は残す
    m.answer(run.id, "1")
    assert wait_for(lambda: run.status == "done")
    assert g.answered == "1"
    assert types(run) == ["run.start", "step.start", "page.done", "run.waiting", "answer", "run.end"]
    assert run.recorder.events[-1]["report"] == "水星は太陽系の惑星です。"
    assert run.result["report"] == "水星は太陽系の惑星です。"
    assert {a["kind"] for a in run.result["artifacts"]} == {"research/v1:facts", "research/v1:sources",
                                                           "research/v1:images", "research/v1:coverage"}
    assert (tmp_path / f"{run.id}.json").exists()
    g.checkpointer.delete_thread.assert_called_once_with(run.thread_id)   # 終わったら会話の記録を消す


def test_answer_is_refused_when_not_waiting(tmp_path):
    m = ResearchRuns(FakeGraph(), tmp_path)
    try:
        m.answer("no-such-run", "1")
        raise AssertionError("ない依頼には答えられない")
    except KeyError:
        pass


class SlowGraph:
    """1 つ目の作業の後で止められるか確かめるための偽物。ready が立つまで最初の作業を返さない。"""

    def __init__(self):
        import threading
        self.ready = threading.Event()
        self.steps = 0
        self.checkpointer = MagicMock()

    def stream(self, payload, config, stream_mode):
        self.ready.wait(5)
        for i in range(3):
            self.steps += 1
            yield "updates", {f"step{i}": {}}

    def get_state(self, config):
        return SimpleNamespace(values={"answer": "報告"})


def test_pause_stops_before_the_next_step_and_resume_finishes(tmp_path):
    g = SlowGraph()
    m = ResearchRuns(g, tmp_path)
    run = m.start({"text": "水星"})
    m.pause(run.id)
    assert run.status == "paused"
    g.ready.set()
    assert wait_for(lambda: g.steps == 1)
    time.sleep(0.2)
    assert g.steps == 1 and run.status == "paused"          # 1 つ目の作業の後で止まっている
    m.resume(run.id)
    assert wait_for(lambda: run.status == "done")
    assert g.steps == 3
    assert types(run) == ["run.start", "run.paused", "run.held", "run.resumed", "run.end"]
    try:
        m.resume(run.id)
        raise AssertionError("止まっていない依頼は再開できない")
    except StateError:
        pass


def test_cancel_stops_a_running_or_waiting_run_and_deletes_its_checkpoint(tmp_path):
    g = SlowGraph()
    m = ResearchRuns(g, tmp_path)
    run = m.start({"text": "水星"})
    m.pause(run.id)
    g.ready.set()
    assert wait_for(lambda: g.steps == 1)
    m.cancel(run.id)                                          # 一時停止中に止める
    assert wait_for(lambda: run.status == "cancelled")
    assert g.steps == 1 and types(run)[-1] == "run.cancelled"
    g.checkpointer.delete_thread.assert_called_once_with(run.thread_id)

    f = FakeGraph()
    m2 = ResearchRuns(f, tmp_path)
    waiting = m2.start({"text": "マーキュリー"})
    assert wait_for(lambda: waiting.status == "waiting")
    m2.cancel(waiting.id)                                     # 答え待ちは、すぐ止まる
    assert waiting.status == "cancelled" and waiting.recorder.finished
    f.checkpointer.delete_thread.assert_called_once_with(waiting.thread_id)


def test_failed_runs_delete_their_checkpoints(tmp_path):
    class Broken(FakeGraph):
        def stream(self, payload, config, stream_mode):
            raise RuntimeError("壊れた")
            yield
    b = Broken()
    run = ResearchRuns(b, tmp_path).start({"text": "壊れる"})
    assert wait_for(lambda: run.status == "error")
    assert "壊れた" in run.recorder.events[-1]["error"]
    b.checkpointer.delete_thread.assert_called_once_with(run.thread_id)


def test_events_go_out_in_the_protocol_shape_and_rest_while_waiting():
    from research_agent.config import Settings
    app = research.create_app(graph=FakeGraph(), settings=Settings("http://localhost:1/v1", "m", "k", "http://x"),
                              key="")
    c = TestClient(app)
    assert c.get("/agent/v1/manifest").json()["extensions"] == ["research/v1"]
    run_id = c.post("/agent/v1/runs", json={"input": {"text": "マーキュリー"}}).json()["run_id"]
    assert wait_for(lambda: c.get(f"/agent/v1/runs/{run_id}").json()["status"] == "waiting")
    body = c.get(f"/agent/v1/runs/{run_id}/events").text
    envs = [e for name, e in protocol.read_sse(body.splitlines()) if name == "message"]
    assert [e["type"] for e in envs] == ["run.start", "step.start", "research/v1:page.done", "run.waiting"]
    assert envs[2]["data"]["url"] == "https://example.org/" and envs[0]["v"] == 1
    assert body.rstrip().endswith('event: rest\ndata: {"status": "waiting"}')     # 答え待ちでは閉じる
    assert c.post(f"/agent/v1/runs/{run_id}/control", json={"action": "resume"}).status_code == 409
    assert c.post(f"/agent/v1/runs/{run_id}/input", json={"kind": "answer", "text": "1"}).status_code == 200
    assert wait_for(lambda: c.get(f"/agent/v1/runs/{run_id}").json()["status"] == "done")
    rest = c.get(f"/agent/v1/runs/{run_id}/events", params={"after": 4}).text
    assert [e["type"] for n, e in protocol.read_sse(rest.splitlines()) if n == "message"] == ["answer", "run.end"]
    assert "event: end" in rest
    assert c.get(f"/agent/v1/runs/{run_id}/result").json()["report"] == "水星は太陽系の惑星です。"
