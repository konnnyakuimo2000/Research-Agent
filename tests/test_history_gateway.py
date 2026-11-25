"""設定画面とアプリゲートウェイの改善（.claude/ui-gateway-settings-plan.md）のゲートウェイ側。

- 履歴の削除（DELETE /api/runs/{id}）: 終わった依頼だけ。一覧・イベント・成果物を消し、削除の印を残すので、
  古い記録の取り込み・成果物の取り直し・立ち上げ直しで戻らない。ファイルを消し切れなければ起動のときに続きを消す
- 開始時のスナップショット: 依頼を始めたときに効いた設定（既定を含む）を履歴に残す。前の版の記録は None
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from research_agent.gateway.app import create_app
from research_agent.gateway.registry import Registry
from research_agent.gateway.store import RunStore

SCHEMA = {"type": "object", "properties": {
    "max_seconds": {"type": "integer", "title": "時間の上限（秒）", "default": 900, "minimum": 420, "maximum": 3600},
    "images": {"type": "boolean", "title": "画像も探す", "default": True}}}


class FakeAgent:
    id = "research"
    error = ""

    def __init__(self):
        self.started = []

    def manifest(self):
        return {"name": "調査", "version": "1", "settings_schema": SCHEMA, "model_roles": []}

    def start(self, input, settings=None, models=None):
        self.started.append(settings)
        return {"run_id": f"a{len(self.started)}"}

    def events(self, run, after=0):
        yield "rest", {}                       # 中継はすぐ休む（テストではイベントを流さない）

    def result(self, run):
        return {"report": "x"}


def app_with(tmp_path: Path):
    reg = Registry([])
    agent = FakeAgent()
    reg.clients, reg.default = {"research": agent}, "research"
    app = create_app(registry=reg, runs_dir=tmp_path, api_key="", resume=False, catalog=False)
    return TestClient(app), app.state.gateway


def finished_run(gw, tmp_path, run_id="r1", status="done"):
    gw.store.create(run_id, "research", "1", "a1", "東京タワーの高さ")
    gw.store.update(run_id, status=status)
    gw.store.append(run_id, {"v": 1, "seq": 1, "t": 0, "type": "run.start", "data": {"request": "東京タワーの高さ"}})
    gw.store.save_result(run_id, {"report": "333 m"})
    return run_id


def test_a_finished_run_is_deleted_everywhere(tmp_path):
    c, gw = app_with(tmp_path)
    finished_run(gw, tmp_path, "r1")
    finished_run(gw, tmp_path, "r2")                                   # 別の履歴は残る
    r = c.delete("/api/runs/r1")
    assert r.status_code == 200 and r.json() == {"id": "r1", "deleted": True, "cleaned": True}
    assert [x["id"] for x in c.get("/api/runs").json()["runs"]] == ["r2"]
    assert c.get("/api/runs/r1/result").status_code == 404
    assert "missing" in c.get("/api/runs/r1/events").text
    assert not (tmp_path / "r1.events.jsonl").exists() and not (tmp_path / "r1.result.json").exists()
    assert (tmp_path / "r2.events.jsonl").exists() and c.get("/api/runs/r2/result").status_code == 200
    assert c.delete("/api/runs/r1").status_code == 404


def test_deleted_runs_do_not_come_back_after_a_restart(tmp_path):
    c, gw = app_with(tmp_path)
    finished_run(gw, tmp_path, "r1")
    c.delete("/api/runs/r1")
    # 古い記録（CLI の jsonl）と同じ名前のファイルがまた置かれても、立ち上げ直しの取り込みで一覧に戻さない
    (tmp_path / "r1.events.jsonl").write_text(json.dumps({"type": "run.start", "request": "x", "t": 0}) + "\n"
                                               + json.dumps({"type": "run.end", "t": 1}) + "\n", encoding="utf-8")
    store = RunStore(tmp_path)                                         # 立ち上げ直し
    store.import_legacy(force=True)
    assert store.get("r1") is None and [r["id"] for r in store.listing()] == []
    assert gw.retry_results() == []                                    # 成果物の取り直しの対象にもならない


def test_runs_that_have_not_finished_are_kept(tmp_path):
    c, gw = app_with(tmp_path)
    for status in ("running", "paused", "waiting", "finishing"):
        finished_run(gw, tmp_path, "r-" + status, status)
        r = c.delete(f"/api/runs/r-{status}")
        assert r.status_code == 409 and "終わってから" in r.json()["detail"]
        assert gw.store.get("r-" + status) is not None and (tmp_path / f"r-{status}.events.jsonl").exists()


def test_files_left_behind_are_removed_on_the_next_start(tmp_path, monkeypatch):
    c, gw = app_with(tmp_path)
    finished_run(gw, tmp_path, "r1")
    real = Path.unlink

    def locked(self, missing_ok=False):
        if self.name == "r1.events.jsonl":
            raise PermissionError("in use")                            # Windows で開かれているなど
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)
    assert c.delete("/api/runs/r1").json()["cleaned"] is False
    assert c.get("/api/runs").json()["runs"] == []                    # 一覧からは消えている
    monkeypatch.setattr(Path, "unlink", real)
    store = RunStore(tmp_path)                                         # 立ち上げ直しで続きを消す
    assert not (tmp_path / "r1.events.jsonl").exists()
    store.import_legacy(force=True)
    assert store.get("r1") is None


def test_the_settings_in_effect_at_the_start_are_kept(tmp_path):
    c, gw = app_with(tmp_path)
    r = c.post("/api/runs", json={"request": "東京タワーの高さ", "agent": "research", "settings": {"images": False}})
    assert r.status_code == 200, r.text
    run = c.get("/api/runs").json()["runs"][0]
    assert run["settings"] == {"images": False}                        # 変えたもの（エージェントに送ったもの）
    assert run["effective_settings"] == {"max_seconds": 900, "images": False}   # 既定を含めて効いた値
    # 前の版の記録は、効いた値を持たない（画面は「開始時の既定値は記録なし」と出す）
    gw.store.create("old", "research", "1", "a9", "古い依頼", {"max_pages": 6})
    old = next(x for x in c.get("/api/runs").json()["runs"] if x["id"] == "old")
    assert old["effective_settings"] is None and old["settings"] == {"max_pages": 6}


def test_an_old_database_gets_the_new_column(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "gateway.sqlite")
    db.execute("""CREATE TABLE runs (id TEXT PRIMARY KEY, agent TEXT, agent_version TEXT, agent_run TEXT, request TEXT,
        status TEXT, question TEXT, created REAL, updated REAL, seconds REAL, events INTEGER, settings TEXT,
        hidden INTEGER DEFAULT 0)""")
    db.execute("INSERT INTO runs VALUES ('r0','research','','','x','done','',1,1,0,0,'{}',0)")
    db.commit()
    db.close()
    store = RunStore(tmp_path)
    assert store.get("r0")["effective"] is None and store.get("r0")["models"] == "{}"
