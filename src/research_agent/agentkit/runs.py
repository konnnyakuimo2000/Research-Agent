"""依頼の実行（どのエージェントでも同じ動き）。

- 依頼はエージェントの中のスレッドで動かす。ゲートウェイとの接続が切れても続ける
- イベントは Recorder に溜める（記録の保存はゲートウェイの役目。エージェントは終わった依頼を一定の時間だけ覚えておく）
- 聞き返しで止まったら「答え待ち」にし、答えが来たら続きから再開する
- 一時停止: 今の作業が終わったところで止め、再開すると次の作業から続ける（作業の切れ目で gate を待つ）
- 止める（cancel）: 今の作業が終わったところでやめる。答え待ちならすぐやめる

エージェントごとの中身は、RunManager を継いで work() に書く。work() は次のどれかを返す:
Done(報告) / Waiting(質問) / Cancelled()

答え待ちの依頼は、エージェントを立ち上げ直しても続けられる（全体アーキテクチャの分析 2）:
- 答え待ちになったら、依頼（id・スレッド id・質問・入力・設定）と、それまでのイベント（通番と時刻）と、
  エージェントの途中の状態（snapshot_state。調査ならグラフのチェックポイント）を waiting_dir に残す
- 立ち上げたとき restore_waiting で戻す。同じ依頼に答えると、続きから 1 回だけ動く（答えた時点で残したものを消すので、
  答えた後に立ち上げ直しても、同じ質問がもう一度答え待ちにはならない）
- 調べている途中の依頼も、作業の区切りごとに同じ形で残し（save_progress。エージェントが区切りで呼ぶ）、立ち上げたら
  最後の区切りから続ける。区切りの途中だった作業は、初めからやり直す（その作業のイベントは二重に出ることがある）
- イベントは依頼ごとのファイル（waiting_dir/events/<id>.jsonl）にも 1 件ずつ書く。立ち上げ直した後も、通番は
  ゲートウェイが受け取った続きから付く（区切りの後に出したイベントも失わない）。終わったら消す
"""

from __future__ import annotations

import json
import logging
import pickle
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from research_agent.agent.telemetry import Recorder

log = logging.getLogger(__name__)

LIVE = ("running", "paused", "waiting")


def _replace(tmp: Path, path: Path, tries: int = 20) -> None:
    """書き終えた一時ファイルで置き換える。Windows では、置き換える先を誰かが読んでいる間は置き換えられない
    （PermissionError）ので、少し待ってやり直す（前は 1 回で諦め、答え待ちを残せないことがあった）。"""
    for i in range(tries):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


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
        """作業の切れ目で呼ぶ。一時停止中なら再開まで待つ。止められたら True。
        一時停止中にここへ来たら、実際に止まったこと（run.held）を知らせる（前は「受け付けた」までしか分からなかった）。"""
        if not self.gate.is_set() and not self.cancelled:
            self.emit("run.held")
        self.gate.wait()
        return self.cancelled

    def summary(self) -> dict:
        return {"run_id": self.id, "request": self.request, "status": self.status, "question": self.question,
                "created": self.created, "events": len(self.recorder.events)}


class StateError(Exception):
    """今の状態ではできない操作（答えを待っていない調査に答えた、など）。"""


class RunManager:
    keep_seconds = 3600.0     # 終わった依頼を覚えておく時間（ゲートウェイが記録を受け取り終えるまで）

    def __init__(self, waiting_dir: Path | None = None) -> None:
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()
        self.waiting_dir = waiting_dir
        self.events_dir = waiting_dir / "events" if waiting_dir else None

    # ---- エージェントごとに書く ----

    def work(self, run: Run, payload: tuple[str, object]) -> Done | Waiting | Cancelled:
        """payload は ("start", 入力) か ("answer", 答えの文)。"""
        raise NotImplementedError

    def cleanup(self, run: Run) -> None:
        """終わった・失敗した・止めた依頼の後片付け（会話の記録を消すなど）。"""

    def snapshot_state(self, run: Run) -> object:
        """答え待ちの依頼の、エージェントの途中の状態（立ち上げ直しても続けるため。pickle できるもの）。"""
        return None

    def restore_state(self, run: Run, state: object) -> None:
        """snapshot_state で残したものを戻す。"""

    # ---- 答え待ちの依頼を残す・戻す ----

    def _waiting_path(self, run_id: str) -> Path | None:
        return self.waiting_dir / f"{run_id}.waiting.pkl" if self.waiting_dir else None

    def _events_path(self, run_id: str) -> Path | None:
        return self.events_dir / f"{run_id}.jsonl" if self.events_dir else None

    def save_progress(self, run: Run) -> None:
        """作業の区切りで呼ぶ: 調べている途中の依頼を残す（立ち上げ直したら、この区切りから続ける）。"""
        if run.status in ("running", "paused") and not run.cancelled:
            self._save_waiting(run)

    def _save_waiting(self, run: Run) -> None:
        path = self._waiting_path(run.id)
        if path is None:
            return
        try:
            data = {"run": {"id": run.id, "request": run.request, "input": run.input, "settings": run.settings,
                            "models": run.models, "thread_id": run.thread_id, "question": run.question,
                            "status": run.status, "created": run.created, "started": run.recorder.started,
                            "events": list(run.recorder.events)},
                    "state": self.snapshot_state(run)}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps(data))
            _replace(tmp, path)
        except Exception:  # noqa: BLE001 - 残せなくても答え待ちは続ける（立ち上げ直したら続けられないだけ）
            log.exception("could not keep waiting run %s", run.id)

    def _drop_waiting(self, run: Run) -> None:
        path = self._waiting_path(run.id)
        if path is not None:
            path.unlink(missing_ok=True)

    def _drop_events(self, run: Run) -> None:
        path = self._events_path(run.id)
        if path is not None:
            path.unlink(missing_ok=True)

    def restore_waiting(self) -> list[str]:
        """立ち上げたとき: 残っている答え待ちの依頼を戻す。戻した依頼の id を返す。
        ファイルは、このエージェントが書いたもの（waiting_dir の中）だけを読む。"""
        if not self.waiting_dir or not self.waiting_dir.exists():
            return []
        out = []
        for path in sorted(self.waiting_dir.glob("*.waiting.pkl")):
            try:
                data = pickle.loads(path.read_bytes())
                r = data["run"]
                status = r.get("status", "waiting")
                recorder = Recorder(r["id"], self._events_path(r["id"]))
                # イベントは、ファイルに書いたもの（区切りの後に出した分も含む）があればそれを使う
                recorder.events, recorder.started = self._read_events(r["id"]) or list(r["events"]), r["started"]
                run = Run(r["id"], r["request"], r["input"], r["settings"], recorder, r["models"], status=status,
                          question=r["question"] if status == "waiting" else "", created=r["created"],
                          thread_id=r["thread_id"])
                self.restore_state(run, data.get("state"))
            except Exception:  # noqa: BLE001 - 戻せない記録（版が違うなど）は飛ばす
                log.exception("could not restore run from %s", path.name)
                continue
            with self._lock:
                self.runs[run.id] = run
            out.append(run.id)
            log.info("restored %s run %s (%d events)", status, run.id, len(run.recorder.events))
            if status in ("running", "paused"):
                if status == "paused":
                    run.gate.clear()                       # 一時停止のまま戻す（再開を押したら続く）
                run.emit("status", text="エージェントを立ち上げ直したため、最後の区切りから続ける")
                self._spawn(run, ("resume", None))
        return out

    def _read_events(self, run_id: str) -> list[dict]:
        path = self._events_path(run_id)
        if path is None or not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue                                   # 書いている途中で止まった最後の行
        return out

    # ---- 操作 ----

    def start(self, input: dict, settings: dict | None = None, models: dict | None = None) -> Run:
        self._prune()
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        text = str(input.get("text", ""))
        models = {str(k): str(v) for k, v in (models or {}).items() if v}
        run = Run(run_id, text, input, settings or {}, Recorder(run_id, self._events_path(run_id)), models)
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
        self._drop_waiting(run)          # 答えたら消す（立ち上げ直しても、同じ質問に二度は答えない）
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
                self._drop_waiting(run)
                run.emit("run.error", error=f"{type(e).__name__}: {e}")
                run.recorder.finish()
                self._cleanup(run)
                return
            self._end(run, outcome)

    def _end(self, run: Run, outcome: Done | Waiting | Cancelled) -> None:
        if isinstance(outcome, Waiting) and not run.cancelled:
            # 答え待ちにすることと残すことを、答え・止めると同じ鍵の中で行う（残す前に答えや止めるが来て、
            # 消した後に残してしまう・残す前に答え待ちに見える、を防ぐ）
            with self._lock:
                run.status, run.question = "waiting", outcome.question
                run.emit("run.waiting", question=outcome.question)
                self._save_waiting(run)
            return
        self._drop_waiting(run)
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
        self._drop_waiting(run)
        self._drop_events(run)
        try:
            self.cleanup(run)
        except Exception:  # noqa: BLE001 - 片付けの失敗で結果を変えない
            log.exception("cleanup of run %s failed", run.id)

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            for k in [k for k, r in self.runs.items() if r.ended and now - r.ended > self.keep_seconds]:
                del self.runs[k]
