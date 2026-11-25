"""全体アーキテクチャの分析（.claude/architecture-review.md）の 2: 答え待ちの依頼は、エージェントを立ち上げ直しても続けられる。

質問を出した状態でエージェントを立ち上げ直し、同じ依頼に答えると、調査が続きから 1 回だけ動く。
グラフは本物の LangGraph（interrupt で聞き返す小さなもの）。立ち上げ直しは、新しいチェックポイント置き場の
グラフと新しい RunManager で表す。
"""

from __future__ import annotations

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
    wait_for(kept.exists)
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
