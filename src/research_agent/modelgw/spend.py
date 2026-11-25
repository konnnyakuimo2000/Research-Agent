"""費用の記録と上限（docs/telemetry_enhancement_plan.md 6.3。M3）。

- 値段を書いたモデル（Web API）の呼び出しごとに、費用（ドル）を足す。日ごと（この PC の日付）と調査ごと
- 上限（models.toml の [limits]）: daily_usd は今日の合計、run_usd は 1 つの調査の合計。
  上限に達していたら、値段のあるモデルへは送る前に断る（値段のないモデル＝研究室のモデルは断らない）
- 同じ調査から値段のあるモデルへの呼び出しは、調査ごとの鍵（lock_for）で 1 件ずつにし、予算の判定から費用の記録までを
  その中で行う。前は判定を送る前に 1 回だけ行い、費用は送信枠を返した後に足していたので、同じ調査から同時に来た
  呼び出しが、最初の費用が記録される前にすべて判定を通っていた（再レビュー 4）。
  日ごとの上限は、別々の調査が同時に送っている分だけ少し超えることがある
- 使用量が返らなかった呼び出しは「未計測」として数える（0 ドルの呼び出しと区別する）
- file を渡すと、日ごとの合計と調査ごとの合計をそこに残す（窓口を立ち上げ直しても、今日の分を数え直さない）
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_RUNS = 500     # 覚えておく調査の数（古いものから忘れる）


def today() -> str:
    return time.strftime("%Y-%m-%d")


class Spend:
    def __init__(self, limits: dict | None = None, file: Path | None = None) -> None:
        self.limits = {k: float(v) for k, v in (limits or {}).items() if v}
        self.file = file
        self.lock = threading.Lock()
        self.day = today()
        self.daily: dict[str, float] = {}        # モデル → 今日の費用
        self.runs: dict[str, dict] = {}          # 調査 → {usd, models: {モデル: 費用}, unmeasured, updated}
        self.unmeasured: dict[str, int] = {}     # モデル → 今日の未計測の呼び出しの数
        self._run_locks: dict[str, threading.Lock] = {}
        self._load()

    def _load(self) -> None:
        if not self.file or not self.file.exists():
            return
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("費用の記録を読めません（%s）。0 から数えます", e)
            return
        if data.get("day") == self.day:
            self.daily = {k: float(v) for k, v in data.get("daily", {}).items()}
            self.unmeasured = {k: int(v) for k, v in data.get("unmeasured", {}).items()}
        self.runs = data.get("runs", {})

    def _save(self) -> None:
        if not self.file:
            return
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"day": self.day, "daily": self.daily, "unmeasured": self.unmeasured,
                                       "runs": self.runs}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.file)
        except OSError as e:
            log.warning("費用の記録を残せません: %s", e)

    def _roll(self) -> None:
        if today() != self.day:                  # 日付が変わった
            self.day, self.daily, self.unmeasured = today(), {}, {}

    def lock_for(self, budget: str) -> threading.Lock:
        """同じ調査の、値段のあるモデルへの呼び出しを 1 件ずつにする鍵（予算の判定から費用の記録まで持つ）。"""
        with self.lock:
            return self._run_locks.setdefault(budget, threading.Lock())

    def add_unmeasured(self, model: str, budget: str | None) -> None:
        """値段のあるモデルの呼び出しで、使用量が返らなかった（費用を測れなかった）。"""
        with self.lock:
            self._roll()
            self.unmeasured[model] = self.unmeasured.get(model, 0) + 1
            if budget:
                r = self.runs.setdefault(budget, {"usd": 0.0, "models": {}})
                r["unmeasured"] = r.get("unmeasured", 0) + 1
                r["updated"] = time.time()
            self._save()

    def refusal(self, budget: str | None) -> str:
        """上限に達していれば、その理由（なければ空）。値段のあるモデルへ送る前に呼ぶ。"""
        with self.lock:
            self._roll()
            daily = self.limits.get("daily_usd")
            spent = sum(self.daily.values())
            if daily and spent >= daily:
                return f"今日の費用の上限（${daily:.2f}）に達しました（${spent:.4f}）"
            run = self.limits.get("run_usd")
            used = self.runs.get(budget or "", {}).get("usd", 0.0)
            if run and budget and used >= run:
                return f"この調査の費用の上限（${run:.2f}）に達しました（${used:.4f}）"
        return ""

    def add(self, model: str, budget: str | None, usd: float) -> None:
        if usd <= 0:
            return
        with self.lock:
            self._roll()
            self.daily[model] = self.daily.get(model, 0.0) + usd
            if budget:
                r = self.runs.setdefault(budget, {"usd": 0.0, "models": {}})
                r["usd"] += usd
                r["models"][model] = r["models"].get(model, 0.0) + usd
                r["updated"] = time.time()
                if len(self.runs) > KEEP_RUNS:
                    for key in sorted(self.runs, key=lambda k: self.runs[k].get("updated", 0))[:len(self.runs) - KEEP_RUNS]:
                        del self.runs[key]
            self._save()

    def run(self, budget: str) -> dict:
        with self.lock:
            r = self.runs.get(budget) or {"usd": 0.0, "models": {}}
            return {"usd": round(r["usd"], 6), "models": {k: round(v, 6) for k, v in r["models"].items()},
                    "unmeasured": r.get("unmeasured", 0), "limit_usd": self.limits.get("run_usd")}

    def as_dict(self) -> dict:
        with self.lock:
            self._roll()
            return {"day": self.day, "today_usd": round(sum(self.daily.values()), 6),
                    "today_by_model": {k: round(v, 6) for k, v in self.daily.items()},
                    "unmeasured_today": sum(self.unmeasured.values()), "limits": self.limits}
