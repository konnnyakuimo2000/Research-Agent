"""依頼の記録の置き場（docs/telemetry_enhancement_plan.md 8.3）。

- 一覧は SQLite（runs/gateway.sqlite）。エージェントの id と版、設定、状態を残す
- イベントは runs/<ID>.events.jsonl に、契約の封筒の形で 1 行ずつ書く（再生と持ち出しに使う）
- 結果（報告と成果物）は runs/<ID>.result.json
- 分ける前に作られた記録（平らなイベントの jsonl）は、起動したときに一覧に入れる（調査エージェントの記録として読む）
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from research_agent.agentkit import protocol

LEGACY_AGENT, LEGACY_EXTENSION = "research", "research/v1"
RECENT_SECONDS = 600.0
LEGACY_SCAN_SECONDS = 30.0   # CLI の記録を探しにフォルダーを見る間隔
COLUMNS = ("id", "agent", "agent_version", "agent_run", "request", "status", "question", "created", "updated",
           "seconds", "events", "settings", "hidden", "models")


def safe_id(run_id: str) -> bool:
    return bool(run_id) and len(run_id) < 80 and all(c.isalnum() or c == "-" for c in run_id)


class RunStore:
    def __init__(self, dir_: Path) -> None:
        self.dir = dir_
        dir_.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._legacy_at = 0.0
        self.db = sqlite3.connect(dir_ / "gateway.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, agent TEXT, agent_version TEXT, agent_run TEXT, request TEXT, status TEXT,
            question TEXT, created REAL, updated REAL, seconds REAL, events INTEGER, settings TEXT,
            hidden INTEGER DEFAULT 0, models TEXT DEFAULT '{}')""")
        # 前の版の記録（models の列がない）に列を足す
        if "models" not in {r[1] for r in self.db.execute("PRAGMA table_info(runs)").fetchall()}:
            self.db.execute("ALTER TABLE runs ADD COLUMN models TEXT DEFAULT '{}'")
        self.db.commit()
        self.import_legacy()

    # ---- 一覧 ----

    def create(self, run_id: str, agent: str, agent_version: str, agent_run: str, request: str,
               settings: dict | None = None, hidden: bool = False, models: dict | None = None) -> dict:
        now = time.time()
        row = {"id": run_id, "agent": agent, "agent_version": agent_version, "agent_run": agent_run, "request": request,
               "status": "running", "question": "", "created": now, "updated": now, "seconds": 0, "events": 0,
               "settings": json.dumps(settings or {}, ensure_ascii=False), "hidden": int(hidden),
               "models": json.dumps(models or {}, ensure_ascii=False)}
        with self._lock:
            self.db.execute(f"INSERT INTO runs ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                            [row[c] for c in COLUMNS])
            self.db.commit()
        return row

    def update(self, run_id: str, **fields) -> None:
        fields["updated"] = time.time()
        with self._lock:
            self.db.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                            [*fields.values(), run_id])
            self.db.commit()

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def listing(self, limit: int = 50, statuses: tuple[str, ...] | None = None) -> list[dict]:
        self.import_legacy()          # CLI（scripts/agent.py）で実行した調査も一覧に出す
        sql, args = "SELECT * FROM runs WHERE hidden = 0", []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY created DESC LIMIT ?", [*args, limit]).fetchall()   # ID は時刻帯で前後するので、作った時刻の順
        return [dict(r) for r in rows]

    # ---- イベントと結果 ----

    def events_path(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.events.jsonl"

    def append(self, run_id: str, env: dict) -> None:
        with open(self.events_path(run_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(env, ensure_ascii=False, default=str) + "\n")

    def events(self, run_id: str) -> list[dict]:
        """記録したイベント（封筒の形）。分ける前の記録も、封筒の形にして返す。"""
        path = self.events_path(run_id)
        if not safe_id(run_id) or not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(protocol.to_v1(json.loads(line), run_id, LEGACY_EXTENSION))
            except (json.JSONDecodeError, AttributeError):
                continue
        return out

    def save_result(self, run_id: str, result: dict) -> None:
        (self.dir / f"{run_id}.result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str),
                                                       encoding="utf-8")

    def result(self, run_id: str) -> dict | None:
        path = self.dir / f"{run_id}.result.json"
        if not safe_id(run_id) or not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # ---- 分ける前の記録 ----

    def import_legacy(self, force: bool = False) -> int:
        """CLI で実行した調査の記録を一覧に入れる。フォルダーを見るのは LEGACY_SCAN_SECONDS に 1 回まで
        （Windows の Docker のフォルダー共有では、100 個ほどのファイルを見るのに 2 秒かかり、一覧を開くたびに待たせていた）。"""
        now = time.time()
        if not force and now - self._legacy_at < LEGACY_SCAN_SECONDS:
            return 0
        self._legacy_at = now
        with self._lock:
            known = {r[0] for r in self.db.execute("SELECT id FROM runs").fetchall()}
        n = 0
        for path in sorted(self.dir.glob("*.events.jsonl")):
            run_id = path.name.removesuffix(".events.jsonl")
            if run_id in known or not safe_id(run_id):
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            try:
                first, last = json.loads(lines[0]), json.loads(lines[-1])
            except (IndexError, json.JSONDecodeError):
                continue
            first, last = protocol.to_v1(first, run_id), protocol.to_v1(last, run_id)
            status = {"run.end": "done", "run.error": "error", "run.cancelled": "cancelled"}.get(last["type"], "stopped")
            mtime = path.stat().st_mtime
            if status == "stopped" and time.time() - mtime < RECENT_SECONDS:
                continue                  # CLI でまだ動いているかもしれない（終わってから入れる）
            with self._lock:
                self.db.execute(f"INSERT INTO runs ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                                [run_id, LEGACY_AGENT, "", "", first["data"].get("request", ""), status, "", mtime,
                                 mtime, last.get("t", 0), last.get("seq", 0), "{}", 0, "{}"])
                self.db.commit()
            n += 1
        return n


@dataclass
class Feed:
    """動いている依頼のイベント（画面に配るため、メモリにも持つ）。"""
    events: list[dict] = field(default_factory=list)
    finished: bool = False
    relaying: bool = False
    generation: int = 0          # 中継をつなぎ直すたびに増やす（古い中継は、目が覚めたら自分で抜ける）
    seen: float = 0.0            # 中継がエージェントから最後に何か（イベント・生存の合図）を受け取った時刻
    condition: threading.Condition = field(default_factory=threading.Condition)

    @property
    def last_seq(self) -> int:
        return self.events[-1]["seq"] if self.events else 0

    def add(self, env: dict) -> None:
        with self.condition:
            self.events.append(env)
            self.condition.notify_all()

    def finish(self) -> None:
        with self.condition:
            self.finished = True
            self.condition.notify_all()

    def wait_after(self, seq: int, timeout: float) -> list[dict]:
        """seq より後のイベント。なければ timeout 秒まで待つ。"""
        with self.condition:
            new = [e for e in self.events if e["seq"] > seq]
            if not new and not self.finished:
                self.condition.wait(timeout)
                new = [e for e in self.events if e["seq"] > seq]
            return new
