"""調査エージェントのグラフ。

    START → check_feasibility ─┬─ report_infeasible → END      （例 D: 調べられない依頼）
                               └─ decide ─┬─ search ───→ decide
                                          ├─ ask_user ─→ decide   （例 A: 聞き返す）
                                          ├─ read_pages → decide  （ページ本文の深掘り）
                                          ├─ check_conflicts → decide（出典どうしの食い違い）
                                          ├─ find_images → decide （画像の探索と確認）
                                          └─ respond ──→ END
"""

from __future__ import annotations

import time
from collections.abc import Callable

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph

from research_agent.agent import telemetry
from research_agent.agent.nodes import Nodes, route, route_feasibility
from research_agent.agent.state import AgentState
from research_agent.config import Settings, load_settings
from research_agent.llm import LLM, WAIT_METER, WaitMeter


LLM_OUTAGE = "研究室の LLM サーバーに一時的に接続できなかった（待った時間は調査の時間に数えていない）"


def timed(name: str, fn: Callable[[AgentState], dict], llm: LLM | None = None) -> Callable[[AgentState], dict]:
    """ノードの所要時間を実行記録（trace）に残す。どこに時間がかかったかを調べるため。

    LLM サーバーに接続できずに待った時間（llm_wait）も残す。時間の上限はこれを除いて数える（guard.elapsed）。
    """
    def run(state: AgentState) -> dict:
        started = time.perf_counter()
        meter = WaitMeter()                         # このノードの実行で待った時間だけを測る（別の調査の待ちを混ぜない）
        token = WAIT_METER.set(meter)
        ev = telemetry.emitter()
        step = len([t for t in state.get("trace", []) if "seconds" in t]) + 1
        ev("step.start", node=name, step=step, decision=(state.get("decision") or {}).get("action", ""))
        try:
            out = fn(state)
        except Exception as e:
            if not isinstance(e, GraphBubbleUp):   # 聞き返し（interrupt）は失敗ではない
                ev("step.end", node=name, step=step, error=f"{type(e).__name__}: {e}")
            raise
        finally:
            WAIT_METER.reset(token)
        seconds = round(time.perf_counter() - started, 1)
        # 待ちは重なりを除いて数える（並列に読むページの待ちを二重に数えない）。念のためノードの時間で頭打ちにする
        wait = round(min(seconds, meter.seconds()), 1)
        ev("step.end", node=name, step=step, seconds=seconds, llm_wait=max(wait, 0))
        entry = {"node": name, "seconds": seconds} | ({"llm_wait": wait} if wait > 0 else {})
        out = {**out, "trace": out.get("trace", []) + [entry]}
        if wait > 0:
            out["failures"] = list(dict.fromkeys(out.get("failures", state.get("failures", [])) + [LLM_OUTAGE]))
        return out
    return run


def build_graph(settings: Settings | None = None, llm: LLM | None = None):
    settings = settings or load_settings()
    llm = llm or LLM(settings)
    nodes = Nodes(settings, llm)

    g = StateGraph(AgentState)
    for name in ("check_feasibility", "report_infeasible", "decide", "search", "ask_user", "read_pages",
                 "check_conflicts", "find_images", "respond"):
        g.add_node(name, timed(name, getattr(nodes, name), llm))

    g.add_edge(START, "check_feasibility")
    g.add_conditional_edges("check_feasibility", route_feasibility, ["report_infeasible", "decide"])
    g.add_edge("report_infeasible", END)
    g.add_conditional_edges("decide", route, ["search", "ask_user", "read_pages", "check_conflicts", "find_images",
                                              "respond"])
    g.add_edge("find_images", "decide")
    g.add_edge("read_pages", "decide")
    g.add_edge("check_conflicts", "decide")
    g.add_edge("search", "decide")
    g.add_edge("ask_user", "decide")
    g.add_edge("respond", END)

    # interrupt（聞き返し）で一時停止して再開するため、状態を保存する
    return g.compile(checkpointer=InMemorySaver())
