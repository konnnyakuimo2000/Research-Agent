"""費用の記録と上限（docs/telemetry_enhancement_plan.md 6.3。M3）。

- 値段を書いたモデル（Web API）の呼び出しごとに、費用（ドル）を足す。日ごと（この PC の日付）と調査ごと
- 上限（models.toml の [limits]）: daily_usd は今日の合計、run_usd は 1 つの調査の合計。
  上限に達していたら、値段のあるモデルへは送る前に断る（値段のないモデル＝研究室のモデルは断らない）
- 上限は「送る前に」確かめるので、同時に送っている分だけ少し超えることがある
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
        self.runs: dict[str, dict] = {}          # 調査 → {usd, models: {モデル: 費用}, updated}
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
        self.runs = data.get("runs", {})

    def _save(self) -> None:
        if not self.file:
            return
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"day": self.day, "daily": self.daily, "runs": self.runs}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.file)
        except OSError as e:
            log.warning("費用の記録を残せません: %s", e)

    def _roll(self) -> None:
        if today() != self.day:                  # 日付が変わった
            self.day, self.daily = today(), {}

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
                    "limit_usd": self.limits.get("run_usd")}

    def as_dict(self) -> dict:
        with self.lock:
            self._roll()
            return {"day": self.day, "today_usd": round(sum(self.daily.values()), 6),
                    "today_by_model": {k: round(v, 6) for k, v in self.daily.items()}, "limits": self.limits}
