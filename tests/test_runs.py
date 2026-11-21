"""テレメトリー画面のための調査の実行（RunManager）。グラフは偽物に差し替える。"""

import time
from types import SimpleNamespace

from langgraph.types import Command

from my_llm_app.runs import RunManager


class FakeGraph:
    """1 回目は質問で止まり、答えで再開すると報告して終わる。"""

    def __init__(self):
        self.answered = None

    def stream(self, payload, config, stream_mode):
        if isinstance(payload, Command):
            self.answered = payload.resume
            yield "custom", {"type": "answer", "answer": payload.resume, "wall": time.time()}
            yield "updates", {"respond": {"answer": "報告"}}
            return
        yield "custom", {"type": "step.start", "node": "ask_user", "step": 1, "wall": time.time()}
        yield "updates", {"__interrupt__": [SimpleNamespace(value={"question": "どれですか？\n1. 水星\n2. 歌手"})]}

    def get_state(self, config):
        return SimpleNamespace(values={"answer": "水星は太陽系の惑星です。"})


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_run_waits_for_answer_then_finishes_and_is_listed(tmp_path):
    m = RunManager(FakeGraph(), tmp_path)
    run = m.start("マーキュリーについて")
    assert wait_for(lambda: run.status == "waiting")
    assert run.question.startswith("どれですか")
    m.answer(run.id, "1")
    assert wait_for(lambda: run.status == "done")
    types = [e["type"] for e in run.recorder.events]
    assert types == ["run.start", "step.start", "run.waiting", "answer", "run.end"]
    assert run.recorder.events[-1]["answer"] == "水星は太陽系の惑星です。"
    assert (tmp_path / f"{run.id}.events.jsonl").exists() and (tmp_path / f"{run.id}.json").exists()

    # サーバーを立ち上げ直した後も、記録から一覧と再生ができる
    fresh = RunManager(FakeGraph(), tmp_path)
    listed = fresh.listing()
    assert listed[0]["id"] == run.id and listed[0]["status"] == "done" and listed[0]["request"] == "マーキュリーについて"
    assert [e["type"] for e in fresh.events(run.id)] == types


def test_answer_is_refused_when_not_waiting_and_unsafe_ids_are_ignored(tmp_path):
    m = RunManager(FakeGraph(), tmp_path)
    try:
        m.answer("no-such-run", "1")
        raise AssertionError("答えを待っていない調査には答えられない")
    except KeyError:
        pass
    assert m.events_on_disk("../etc/passwd") == []


class SlowGraph:
    """1 つ目の作業の後で止められるか確かめるための偽物。ready が立つまで最初の作業を返さない。"""

    def __init__(self):
        import threading
        self.ready = threading.Event()
        self.steps = 0

    def stream(self, payload, config, stream_mode):
        self.ready.wait(5)
        for i in range(3):
            self.steps += 1
            yield "updates", {f"step{i}": {}}

    def get_state(self, config):
        return SimpleNamespace(values={"answer": "報告"})


def test_pause_stops_before_the_next_step_and_resume_finishes(tmp_path):
    g = SlowGraph()
    m = RunManager(g, tmp_path)
    run = m.start("水星")
    m.pause(run.id)
    assert run.status == "paused"
    g.ready.set()
    assert wait_for(lambda: g.steps == 1)
    time.sleep(0.2)
    assert g.steps == 1 and run.status == "paused"          # 1 つ目の作業の後で止まっている
    m.resume(run.id)
    assert wait_for(lambda: run.status == "done")
    assert g.steps == 3
    types = [e["type"] for e in run.recorder.events]
    assert types == ["run.start", "run.paused", "run.resumed", "run.end"]
    try:
        m.resume(run.id)
        raise AssertionError("止まっていない調査は再開できない")
    except KeyError:
        pass


def test_finished_and_failed_runs_delete_their_checkpoints(tmp_path):
    from unittest.mock import MagicMock
    g = FakeGraph()
    g.checkpointer = MagicMock()
    m = RunManager(g, tmp_path)
    run = m.start("マーキュリーについて")
    assert wait_for(lambda: run.status == "waiting")
    g.checkpointer.delete_thread.assert_not_called()                  # 答えを待っている間は残す
    m.answer(run.id, "1")
    assert wait_for(lambda: run.status == "done")
    g.checkpointer.delete_thread.assert_called_once_with(run.thread_id)

    class Broken(FakeGraph):
        def stream(self, payload, config, stream_mode):
            raise RuntimeError("壊れた")
            yield
    b = Broken()
    b.checkpointer = MagicMock()
    run2 = RunManager(b, tmp_path).start("壊れる")
    assert wait_for(lambda: run2.status == "error")
    b.checkpointer.delete_thread.assert_called_once_with(run2.thread_id)
