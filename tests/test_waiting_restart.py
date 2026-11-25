"""全体アーキテクチャの分析（.claude/architecture-review.md）の 2: 答え待ちの依頼は、エージェントを立ち上げ直しても続けられる。

質問を出した状態でエージェントを立ち上げ直し、同じ依頼に答えると、調査が続きから 1 回だけ動く。
グラフは本物の LangGraph（interrupt で聞き返す小さなもの）。立ち上げ直しは、新しいチェックポイント置き場の
グラフと新しい RunManager で表す。
"""

from __future__ import annotations

import threading
import time
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from research_agent.agents.research import ResearchRuns


class St(TypedDict, total=False):
    request: str
    models: dict
    budget: str
    settings: dict
    found: str
    answer: str


def make_graph(calls: list[str]):
    def search(state: St) -> dict:
        calls.append("search")                       # 立ち上げ直した後に、もう一度は動かない
        return {"found": "東京タワーは 2 つある"}

    def ask(state: St) -> dict:
        reply = interrupt({"question": "どちらの東京タワーですか？"})
        calls.append(f"ask:{reply}")
        return {"answer": f"{reply} の東京タワーは 333 m（{state['found']}）"}

    g = StateGraph(St)
    g.add_node("search", search)
    g.add_node("ask_user", ask)
    g.add_edge(START, "search")
    g.add_edge("search", "ask_user")
    g.add_edge("ask_user", END)
    return g.compile(checkpointer=InMemorySaver())


def saved_status(path):
    import pickle
    try:
        return pickle.loads(path.read_bytes())["run"].get("status")
    except (OSError, EOFError, pickle.UnpicklingError):
        return None


def wait_for(cond, seconds=5.0):
    end = time.time() + seconds
    while not cond() and time.time() < end:
        time.sleep(0.02)
    assert cond()


def test_a_waiting_run_continues_once_after_a_restart(tmp_path):
    calls: list[str] = []
    first = ResearchRuns(make_graph(calls), tmp_path)
    run = first.start({"text": "東京タワーの高さ"})
    wait_for(lambda: run.status == "waiting")
    assert run.question == "どちらの東京タワーですか？" and calls == ["search"]
    kept = tmp_path / "waiting" / f"{run.id}.waiting.pkl"
    wait_for(lambda: saved_status(kept) == "waiting")        # 作業の区切りでも残すので、答え待ちとして残るまで待つ
    seqs = [e["seq"] for e in run.recorder.events]

    # 立ち上げ直し（チェックポイントの置き場も新しい）
    second = ResearchRuns(make_graph(calls), tmp_path)
    assert second.restore_waiting() == [run.id]
    back = second.get(run.id)
    assert back.status == "waiting" and back.question == "どちらの東京タワーですか？" and back.thread_id == run.thread_id
    assert [e["seq"] for e in back.recorder.events] == seqs          # ゲートウェイは通番の続きから受け取れる

    second.answer(run.id, "港区")
    assert not kept.exists()                                          # 答えたら消す
    wait_for(lambda: back.status == "done")
    assert calls == ["search", "ask:港区"]                            # 検索はやり直さず、続きが 1 回だけ動いた
    assert back.result["report"] == "港区 の東京タワーは 333 m（東京タワーは 2 つある）"
    assert [e["seq"] for e in back.recorder.events] == list(range(1, len(back.recorder.events) + 1))
    assert back.recorder.events[-1]["type"] == "run.end"

    # 答えた後にもう一度立ち上げ直しても、同じ質問がまた答え待ちにはならない
    assert ResearchRuns(make_graph(calls), tmp_path).restore_waiting() == []


def test_a_cancelled_waiting_run_is_not_restored(tmp_path):
    first = ResearchRuns(make_graph([]), tmp_path)
    run = first.start({"text": "x"})
    wait_for(lambda: run.status == "waiting")
    first.cancel(run.id)
    assert run.status == "cancelled"
    assert ResearchRuns(make_graph([]), tmp_path).restore_waiting() == []


def test_a_broken_record_is_skipped(tmp_path):
    (tmp_path / "waiting").mkdir()
    (tmp_path / "waiting" / "bad.waiting.pkl").write_bytes(b"not a pickle")
    assert ResearchRuns(make_graph([]), tmp_path).restore_waiting() == []


def make_three_steps(calls: list[str], block: threading.Event | None = None):
    """a → b → c。block を渡すと、b の途中で止まる（立ち上げ直す前のエージェントが、作業の途中で消えたことを表す）。"""
    def a(state: St) -> dict:
        calls.append("a")
        return {"found": "A"}

    def b(state: St) -> dict:
        calls.append("b")
        if block is not None:
            block.wait(30)
        return {"found": state["found"] + "B"}

    def c(state: St) -> dict:
        calls.append("c")
        return {"answer": state["found"] + "C"}

    g = StateGraph(St)
    for name, fn in (("a", a), ("b", b), ("c", c)):
        g.add_node(name, fn)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", END)
    return g.compile(checkpointer=InMemorySaver())


def test_a_running_run_continues_from_the_last_step_after_a_restart(tmp_path):
    first_calls: list[str] = []
    stuck = threading.Event()                          # 放さない（前のエージェントは b の途中で消えた）
    first = ResearchRuns(make_three_steps(first_calls, stuck), tmp_path)
    run = first.start({"text": "x"})
    kept = tmp_path / "waiting" / f"{run.id}.waiting.pkl"
    wait_for(lambda: first_calls == ["a", "b"] and kept.exists())
    before = [e["seq"] for e in run.recorder.events]

    calls: list[str] = []
    second = ResearchRuns(make_three_steps(calls), tmp_path)
    assert second.restore_waiting() == [run.id]
    back = second.get(run.id)
    wait_for(lambda: back.status == "done")
    assert calls == ["b", "c"]                         # 終わっていた a はやり直さず、途中だった b から続けた
    assert back.result["report"] == "ABC"
    seqs = [e["seq"] for e in back.recorder.events]
    assert seqs[:len(before)] == before and seqs == list(range(1, len(seqs) + 1))   # 通番は途切れない
    assert any("立ち上げ直した" in (e.get("text") or "") for e in back.recorder.events)
    assert not kept.exists() and not (tmp_path / "waiting" / "events" / f"{run.id}.jsonl").exists()   # 終わったら消す
    stuck.set()                                        # 前のエージェントのスレッドを片付ける（テストの終わりの後始末）
    wait_for(lambda: run.status in ("done", "error"), 10)
