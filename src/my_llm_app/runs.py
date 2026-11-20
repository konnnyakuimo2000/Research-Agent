"""テレメトリー画面のための調査の実行と記録（docs/telemetry_design.md 7 章）。

- 調査はサーバーの中のスレッドで動かす。画面との接続が切れても続ける
- イベントは Recorder に溜め、`runs/<ID>.events.jsonl` にも書く。画面は SSE で受け取る（途中から受け直せる）
- 聞き返し（interrupt）で止まったら「答え待ち」にし、答えが来たら同じスレッドの続きから再開する
- 終わった調査（サーバーを立ち上げ直した後も）は、jsonl から一覧と再生ができる
- 一時停止: 今の作業（ノード）が終わったところで止め、再開すると次の作業から続ける
  （LangGraph の stream は、次を受け取りに行くまで次の作業を始めないので、受け取る側で待つ）
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from langgraph.types import Command

from my_llm_app.agent.telemetry import Recorder

log = logging.getLogger(__name__)


@dataclass
class Run:
    id: str
    request: str
    thread_id: str
    recorder: Recorder
    status: str = "running"          # running / paused / waiting / done / error
    question: str = ""
    created: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)
    gate: threading.Event = field(default_factory=threading.Event)   # 閉じている間は次の作業に進まない（一時停止）

    def __post_init__(self) -> None:
        self.gate.set()

    def summary(self) -> dict:
        return {"id": self.id, "request": self.request, "status": self.status, "question": self.question,
                "created": self.created, "events": len(self.recorder.events)}


class RunManager:
    def __init__(self, graph, runs_dir: Path) -> None:
        self.graph = graph
        self.dir = runs_dir
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def start(self, request: str) -> Run:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        run = Run(run_id, request, str(uuid.uuid4()), Recorder(run_id, self.dir / f"{run_id}.events.jsonl"))
        with self._lock:
            self.runs[run_id] = run
        run.recorder.add({"type": "run.start", "request": request})
        self._spawn(run, {"request": request})
        return run

    def answer(self, run_id: str, text: str) -> Run:
        run = self.runs.get(run_id)
        if not run or run.status != "waiting":
            raise KeyError(run_id)
        run.status, run.question = "running", ""
        self._spawn(run, Command(resume=text))
        return run

    def pause(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if not run or run.status != "running":
            raise KeyError(run_id)
        run.gate.clear()
        run.status = "paused"
        run.recorder.add({"type": "run.paused"})
        return run

    def resume(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if not run or run.status != "paused":
            raise KeyError(run_id)
        run.status = "running"
        run.recorder.add({"type": "run.resumed"})
        run.gate.set()
        return run

    def _spawn(self, run: Run, payload: object) -> None:
        threading.Thread(target=self._drive, args=(run, payload), daemon=True, name=f"run-{run.id}").start()

    def _drive(self, run: Run, payload: object) -> None:
        config = {"configurable": {"thread_id": run.thread_id}, "recursion_limit": 40}
        question = ""
        with run.lock:
            try:
                for mode, chunk in self.graph.stream(payload, config, stream_mode=["updates", "custom"]):
                    if mode == "custom":
                        run.recorder.add(chunk)
                        continue
                    if "__interrupt__" in chunk:
                        question = chunk["__interrupt__"][0].value["question"]
                    run.gate.wait()   # 一時停止中は、次の作業を始めない
            except Exception as e:  # noqa: BLE001 - 原因を画面に出す
                log.exception("run %s failed", run.id)
                run.status = "error"
                run.recorder.add({"type": "run.error", "error": f"{type(e).__name__}: {e}"})
                run.recorder.finish()
                return
            if question:
                run.status, run.question = "waiting", question
                run.recorder.add({"type": "run.waiting", "question": question})
                return
            state = self.graph.get_state(config).values
            run.status = "done"
            run.recorder.add({"type": "run.end", "answer": state.get("answer", "")})
            run.recorder.finish()
            try:
                (self.dir / f"{run.id}.json").write_text(json.dumps(state, ensure_ascii=False, indent=1, default=str),
                                                        encoding="utf-8")
            except OSError:
                log.exception("could not save run %s", run.id)

    # ---- 記録 ----

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    def events_on_disk(self, run_id: str) -> list[dict]:
        path = self.dir / f"{run_id}.events.jsonl"
        if not _safe_id(run_id) or not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def events(self, run_id: str) -> list[dict]:
        run = self.runs.get(run_id)
        return list(run.recorder.events) if run else self.events_on_disk(run_id)

    def listing(self, limit: int = 50) -> list[dict]:
        """これまでの調査（新しい順）。動いているものはメモリから、終わったものは jsonl から。"""
        items = {r.id: r.summary() for r in self.runs.values()}
        for path in sorted(self.dir.glob("*.events.jsonl"), reverse=True)[: limit * 2]:
            run_id = path.name.removesuffix(".events.jsonl")
            if run_id in items:
                continue
            first, last = _first_last(path)
            if not first:
                continue
            status = {"run.end": "done", "run.error": "error"}.get(last.get("type"), "stopped")
            items[run_id] = {"id": run_id, "request": first.get("request", ""), "status": status, "question": "",
                             "created": path.stat().st_mtime, "events": last.get("seq", 0), "seconds": last.get("t", 0)}
        return sorted(items.values(), key=lambda x: x["id"], reverse=True)[:limit]


def _safe_id(run_id: str) -> bool:
    return bool(run_id) and all(c.isalnum() or c == "-" for c in run_id)


def _first_last(path: Path) -> tuple[dict, dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        return json.loads(lines[0]), json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        return {}, {}
