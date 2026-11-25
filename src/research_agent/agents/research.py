"""調査エージェントのサービス（契約の 0・1 段と、拡張 research/v1）。

    uv run uvicorn --factory research_agent.agents.research:create_app --port 8101

- 中身は今までの LangGraph のグラフ（research_agent.agent）。ノードが出すイベントは、契約の形にして外に出す
  （step.* などは共通のイベント、page.* などは research/v1 のイベント）
- 作業 1 回ごとに、状態の一行（status。今までの Open WebUI 向けの途中経過の文）も出す
- 終わったら、結果（報告と、research/v1 の成果物: 事実・出典・画像・網羅表）を返す。
  調べた状態の全体は runs/agent-research/<ID>.json に残す（調べ直すため）
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from langgraph.types import Command

from research_agent.agent.progress import describe
from research_agent.agent.state import PRESETS, SETTINGS
from research_agent.agentkit.runs import Cancelled, Done, Run, RunManager, Waiting
from research_agent.agentkit.service import agent_app

log = logging.getLogger(__name__)

VERSION = "1.5.0"
EXTENSION = "research/v1"
RECURSION_LIMIT = 40

NODES = [("check_feasibility", "依頼の確認"), ("decide", "判断"), ("search", "検索"), ("read_pages", "ページを読む"),
         ("find_images", "画像"), ("check_conflicts", "食い違い"), ("ask_user", "聞き返し"), ("respond", "報告"),
         ("report_infeasible", "調べられないことの報告")]

MANIFEST = {
    "id": "research", "name": "Research-Agent", "version": VERSION, "protocol": "1",
    "description": "公開の Web とデータベースを調べ、引用を照合した事実だけで出典付きの報告を書く",
    "levels": [0, 1],
    "extensions": [EXTENSION],
    "capabilities": ["questions", "pause", "cancel"],
    "settings_schema": {"type": "object", "properties": SETTINGS},
    "presets": PRESETS,
    "model_roles": [{"role": "decide", "title": "判断", "needs": ["json_schema"]},
                    {"role": "extract", "title": "ページの読み取り", "needs": ["json_schema"]},
                    {"role": "vision", "title": "画像の確認", "needs": ["json_schema", "vision"]},
                    {"role": "report", "title": "報告", "needs": []}],
    "graph": {
        "nodes": [{"id": "start", "label": "依頼", "kind": "terminal"}]
                 + [{"id": n, "label": label, "kind": "hub" if n == "decide" else "step"} for n, label in NODES]
                 + [{"id": "end", "label": "終わり", "kind": "terminal"}],
        "edges": [["start", "check_feasibility"], ["check_feasibility", "decide"],
                  ["check_feasibility", "report_infeasible"], ["report_infeasible", "end"], ["respond", "end"]]
                 + [e for n in ("search", "read_pages", "find_images", "check_conflicts", "ask_user")
                    for e in (["decide", n], [n, "decide"])] + [["decide", "respond"]],
    },
}


class ResearchRuns(RunManager):
    def __init__(self, graph, data_dir: Path | None = None) -> None:
        # 答え待ちの依頼は data_dir/waiting に残し、立ち上げ直したら戻す（agentkit/runs.py）
        super().__init__(data_dir / "waiting" if data_dir else None)
        self.graph = graph
        self.dir = data_dir

    def snapshot_state(self, run: Run) -> object:
        """グラフのチェックポイントのうち、この依頼のスレッドの分（InMemorySaver の中身。直列化済みの値）。"""
        saver, tid = getattr(self.graph, "checkpointer", None), run.thread_id
        if saver is None or not hasattr(saver, "storage"):
            return None
        return {"storage": {ns: dict(v) for ns, v in saver.storage.get(tid, {}).items()},
                "writes": {k: v for k, v in saver.writes.items() if k[0] == tid},
                "blobs": {k: v for k, v in saver.blobs.items() if k[0] == tid}}

    def restore_state(self, run: Run, state: object) -> None:
        saver = getattr(self.graph, "checkpointer", None)
        if not state or saver is None or not hasattr(saver, "storage"):
            raise RuntimeError("グラフのチェックポイントを戻せない")
        for ns, checkpoints in state["storage"].items():
            saver.storage[run.thread_id][ns].update(checkpoints)
        saver.writes.update(state["writes"])
        saver.blobs.update(state["blobs"])

    def work(self, run: Run, payload: tuple[str, object]) -> Done | Waiting | Cancelled:
        kind, value = payload
        graph_input = ({"request": run.request, "models": run.models, "budget": run.id, "settings": run.settings}
                       if kind == "start"
                       else Command(resume=value))
        config = {"configurable": {"thread_id": run.thread_id}, "recursion_limit": RECURSION_LIMIT}
        question = ""
        for mode, chunk in self.graph.stream(graph_input, config, stream_mode=["updates", "custom"]):
            if mode == "custom":
                run.recorder.add(chunk)
                continue
            if "__interrupt__" in chunk:
                question = chunk["__interrupt__"][0].value["question"]
            else:
                for node, update in chunk.items():
                    text = _describe(node, update)
                    if text:
                        run.emit("status", text=text)
            if run.checkpoint():          # 一時停止中は次の作業を始めない。止められたらやめる
                return Cancelled()
        if question:
            return Waiting(question)
        state = self.graph.get_state(config).values
        run.result = result_of(state)
        self._save(run, state)
        return Done(state.get("answer", ""))

    def cleanup(self, run: Run) -> None:
        checkpointer = getattr(self.graph, "checkpointer", None)
        if checkpointer is not None:          # 会話の記録（チェックポイント）は、終わったら要らない
            checkpointer.delete_thread(run.thread_id)

    def _save(self, run: Run, state: dict) -> None:
        if not self.dir:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{run.id}.json").write_text(json.dumps(state, ensure_ascii=False, indent=1, default=str),
                                                    encoding="utf-8")
        except OSError:
            log.exception("could not save run %s", run.id)


def _describe(node: str, update: object) -> str:
    try:
        return describe(node, update if isinstance(update, dict) else {})
    except Exception:  # noqa: BLE001 - 状態の一行が作れなくても調査は続ける
        return ""


def result_of(state: dict) -> dict:
    """結果の形（4.4）: 報告と、research/v1 の成果物。"""
    from research_agent.agent.report import SourceTable, collect_sources
    from research_agent.tiers import page_tier

    try:
        sources = collect_sources(state)
    except Exception:  # noqa: BLE001
        sources = []
    # 出典 ID は、報告の本文の出典番号と同じ対応表から作る（s3 = 本文の [3]）。表記が違うだけの同じ資料は同じ ID
    sid = SourceTable(sources).id_of

    facts = [{"id": f"f{i}", "text": f.get("text", ""), "quote": f.get("quote", ""), "aspect": f.get("aspect", ""),
              "source_id": sid(f.get("source", "")), "url": f.get("source", ""), "task_id": ""}
             for i, f in enumerate(state.get("verified", []), 1)]
    finals = {r.get("url"): r.get("final_url") for r in state.get("pages_read", []) if r.get("final_url")}
    srcs = [{"id": sid(s["url"]), "url": s["url"], "final_url": finals.get(s["url"], s["url"]), "title": s.get("title", ""),
             "tier": s.get("tier") or page_tier(s["url"], state)} for s in sources]
    imgs = [{"url": i.get("src", ""), "page_url": i.get("page_url", ""), "source_id": sid(i.get("page_url", "")),
             "level": i.get("level", ""), "license": i.get("license", "")} for i in state.get("images", [])]

    def art(kind: str, title: str, data: object) -> dict:
        return {"id": kind.split(":")[1], "kind": kind, "title": title, "media_type": "application/json", "data": data}

    return {"report": state.get("answer", ""), "artifacts": [
        art(f"{EXTENSION}:facts", "事実", facts), art(f"{EXTENSION}:sources", "出典", srcs),
        art(f"{EXTENSION}:images", "画像", imgs), art(f"{EXTENSION}:coverage", "網羅表", state.get("coverage", []))]}


_status: dict = {}


def health(settings) -> dict:
    """モデル（窓口を通すときは窓口）と検索（SearXNG）につながるか（20 秒覚えておく）。"""
    if _status and time.time() - _status["at"] < 20:
        return _status["value"]
    from research_agent.modelgw.status import model_check
    from research_agent.tools import http as http_tools

    started = time.time()
    try:
        r = http_tools.get(settings.searxng_url.rstrip("/") + "/healthz", timeout=4, cache=False, public=False)
        search = {"ok": r.status_code == 200, "seconds": round(time.time() - started, 1)}
    except Exception as e:  # noqa: BLE001 - つながらない理由を画面に出す
        search = {"ok": False, "error": type(e).__name__}
    model, llm = model_check(settings)
    value = {"model": model, "checks": {"llm": llm, "search": {"title": "検索（SearXNG の healthz）", "hint": "検索につながらない", **search}}}
    _status.update(at=time.time(), value=value)
    return value


def create_app(graph=None, settings=None, data_dir: Path | None = None, key: str | None = None):
    from research_agent.config import REPO_ROOT, load_settings

    settings = settings or load_settings()
    if graph is None:
        from research_agent.agent.graph import build_graph
        from research_agent.llm import LLM
        graph = build_graph(settings, LLM(settings))
        data_dir = data_dir or REPO_ROOT / "runs" / "agent-research"
    manager = ResearchRuns(graph, data_dir)
    manager.restore_waiting()          # 立ち上げ直す前に答え待ちだった依頼を戻す
    return agent_app(MANIFEST, manager, lambda: health(settings), key=key)
