"""調査エージェントのグラフ。

    START → check_feasibility ─┬─ report_infeasible → END      （例 D: 調べられない依頼）
                               └─ decide ─┬─ search ───→ decide
                                          ├─ ask_user ─→ decide   （例 A: 聞き返す）
                                          ├─ read_pages → decide  （ページ本文の深掘り）
                                          ├─ check_conflicts → decide（出典どうしの食い違い）
                                          └─ respond ──→ END
"""

from __future__ import annotations

import time
from collections.abc import Callable

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from my_llm_app.agent.nodes import Nodes, route, route_feasibility
from my_llm_app.agent.state import AgentState
from my_llm_app.config import Settings, load_settings
from my_llm_app.llm import LLM


def timed(name: str, fn: Callable[[AgentState], dict]) -> Callable[[AgentState], dict]:
    """ノードの所要時間を実行記録（trace）に残す。どこに時間がかかったかを調べるため。"""
    def run(state: AgentState) -> dict:
        started = time.perf_counter()
        out = fn(state)
        seconds = round(time.perf_counter() - started, 1)
        return {**out, "trace": out.get("trace", []) + [{"node": name, "seconds": seconds}]}
    return run


def build_graph(settings: Settings | None = None, llm: LLM | None = None):
    settings = settings or load_settings()
    nodes = Nodes(settings, llm or LLM(settings))

    g = StateGraph(AgentState)
    for name in ("check_feasibility", "report_infeasible", "decide", "search", "ask_user", "read_pages",
                 "check_conflicts", "respond"):
        g.add_node(name, timed(name, getattr(nodes, name)))

    g.add_edge(START, "check_feasibility")
    g.add_conditional_edges("check_feasibility", route_feasibility, ["report_infeasible", "decide"])
    g.add_edge("report_infeasible", END)
    g.add_conditional_edges("decide", route, ["search", "ask_user", "read_pages", "check_conflicts", "respond"])
    g.add_edge("read_pages", "decide")
    g.add_edge("check_conflicts", "decide")
    g.add_edge("search", "decide")
    g.add_edge("ask_user", "decide")
    g.add_edge("respond", END)

    # interrupt（聞き返し）で一時停止して再開するため、状態を保存する
    return g.compile(checkpointer=InMemorySaver())
