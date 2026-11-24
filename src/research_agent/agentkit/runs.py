"""依頼の実行（どのエージェントでも同じ動き）。

- 依頼はエージェントの中のスレッドで動かす。ゲートウェイとの接続が切れても続ける
- イベントは Recorder に溜める（記録の保存はゲートウェイの役目。エージェントは終わった依頼を一定の時間だけ覚えておく）
- 聞き返しで止まったら「答え待ち」にし、答えが来たら続きから再開する
- 一時停止: 今の作業が終わったところで止め、再開すると次の作業から続ける（作業の切れ目で gate を待つ）
- 止める（cancel）: 今の作業が終わったところでやめる。答え待ちならすぐやめる

エージェントごとの中身は、RunManager を継いで work() に書く。work() は次のどれかを返す:
Done(報告) / Waiting(質問) / Cancelled()
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

from research_agent.agent.telemetry import Recorder

log = logging.getLogger(__name__)

LIVE = ("running", "paused", "waiting")


@dataclass
class Done:
    report: str


@dataclass
class Waiting:
    question: str


@dataclass
class Cancelled:
    pass


@dataclass
class Run:
    id: str
    request: str
    input: dict
    settings: dict
    recorder: Recorder
    models: dict = field(default_factory=dict)      # 依頼ごとに選び直したモデル（役割 → モデルの窓口の id）
    status: str = "running"          # running / paused / waiting / done / error / cancelled
    question: str = ""
    created: float = field(default_factory=time.time)
    ended: float = 0.0
    result: dict = field(default_factory=dict)
    cancelled: bool = False
    thread_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    lock: threading.Lock = field(default_factory=threading.Lock)
    gate: threading.Event = field(default_factory=threading.Event)   # 閉じている間は次の作業に進まない（一時停止）

    def __post_init__(self) -> None:
        self.gate.set()

    def emit(self, type_: str, **data) -> dict:
        return self.recorder.add({"type": type_, **data})

    def checkpoint(self) -> bool:
        """作業の切れ目で呼ぶ。一時停止中なら再開まで待つ。止められたら True。"""
        self.gate.wait()
        return self.cancelled

    def summary(self) -> dict:
        return {"run_id": self.id, "request": self.request, "status": self.status, "question": self.question,
                "created": self.created, "events": len(self.recorder.events)}


class StateError(Exception):
    """今の状態ではできない操作（答えを待っていない調査に答えた、など）。"""


class RunManager:
    keep_seconds = 3600.0     # 終わった依頼を覚えておく時間（ゲートウェイが記録を受け取り終えるまで）

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    # ---- エージェントごとに書く ----

    def work(self, run: Run, payload: tuple[str, object]) -> Done | Waiting | Cancelled:
        """payload は ("start", 入力) か ("answer", 答えの文)。"""
        raise NotImplementedError

    def cleanup(self, run: Run) -> None:
        """終わった・失敗した・止めた依頼の後片付け（会話の記録を消すなど）。"""

    # ---- 操作 ----

    def start(self, input: dict, settings: dict | None = None, models: dict | None = None) -> Run:
        self._prune()
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        text = str(input.get("text", ""))
        models = {str(k): str(v) for k, v in (models or {}).items() if v}
        run = Run(run_id, text, input, settings or {}, Recorder(run_id), models)
        with self._lock:
            self.runs[run_id] = run
        run.emit("run.start", request=text, **({"models": models} if models else {}))
        self._spawn(run, ("start", input))
        return run

    def answer(self, run_id: str, text: str) -> Run:
        run = self._get(run_id)
        with self._lock:
            if run.status != "waiting":
                raise StateError("この依頼は答えを待っていません")
            run.status, run.question = "running", ""
        self._spawn(run, ("answer", text))
        return run

    def pause(self, run_id: str) -> Run:
        run = self._get(run_id)
        with self._lock:
            if run.status != "running":
                raise StateError("この依頼は動いていません")
            run.gate.clear()
            run.status = "paused"
        run.emit("run.paused")
        return run

    def resume(self, run_id: str) -> Run:
        run = self._get(run_id)
        with self._lock:
            if run.status != "paused":
                raise StateError("この依頼は止まっていません")
            run.status = "running"
        run.emit("run.resumed")
        run.gate.set()
        return run

    def cancel(self, run_id: str) -> Run:
        run = self._get(run_id)
        with self._lock:
            if run.status not in LIVE:
                raise StateError("この依頼はもう終わっています")
            run.cancelled = True
            idle = run.status == "waiting"        # 答え待ちは動いているスレッドがないので、ここで終える
            if idle:
                run.status = "cancelling"
        run.gate.set()
        if idle:
            self._end(run, Cancelled())
        return run

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    def _get(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    # ---- 実行 ----

    def _spawn(self, run: Run, payload: tuple[str, object]) -> None:
        threading.Thread(target=self._drive, args=(run, payload), daemon=True, name=f"run-{run.id}").start()

    def _drive(self, run: Run, payload: tuple[str, object]) -> None:
        with run.lock:
            try:
                outcome = self.work(run, payload)
            except Exception as e:  # noqa: BLE001 - 原因を画面に出す
                log.exception("run %s failed", run.id)
                run.status, run.ended = "error", time.time()
                run.emit("run.error", error=f"{type(e).__name__}: {e}")
                run.recorder.finish()
                self._cleanup(run)
                return
            self._end(run, outcome)

    def _end(self, run: Run, outcome: Done | Waiting | Cancelled) -> None:
        if isinstance(outcome, Waiting) and not run.cancelled:
            run.status, run.question = "waiting", outcome.question
            run.emit("run.waiting", question=outcome.question)
            return
        if isinstance(outcome, Done) and not run.cancelled:
            run.status = "done"
            run.result.setdefault("report", outcome.report)
            run.result.setdefault("artifacts", [])
            run.emit("run.end", report=outcome.report)
        else:
            run.status = "cancelled"
            run.emit("run.cancelled")
        run.ended = time.time()
        run.recorder.finish()
        self._cleanup(run)

    def _cleanup(self, run: Run) -> None:
        try:
            self.cleanup(run)
        except Exception:  # noqa: BLE001 - 片付けの失敗で結果を変えない
            log.exception("cleanup of run %s failed", run.id)

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            for k in [k for k, r in self.runs.items() if r.ended and now - r.ended > self.keep_seconds]:
                del self.runs[k]
