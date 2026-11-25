"""再レビュー（.claude/review-fixes.md）の 5: 成果物を保存するまで、終わりの処理を追う。

run.end を受け取った後、エージェントから成果物を取るのに一度失敗すると、一覧は「完了」なのに結果 API が 404 のまま
だった。今は「保存中」で SQLite に残し、見張りと起動のときに成果物だけを取り直して、保存できたら「完了」にする。
"""

from __future__ import annotations

import time

from research_agent.gateway import core
from research_agent.gateway.app import create_app
from research_agent.gateway.registry import AgentError
from test_gateway import Stack, wait_for
from test_ui_parts import _settings_graph


class Flaky:
    """client.result を n 回だけ失敗させる。"""

    def __init__(self, real, failures):
        self.real, self.failures, self.calls = real, failures, 0

    def __call__(self, agent_run):
        self.calls += 1
        if self.calls <= self.failures:
            raise AgentError(503, "一時的に取れない")
        return self.real(agent_run)


def start(tmp_path, failures):
    s = Stack(tmp_path, graph=_settings_graph())
    client = s.registry.clients["research"]
    client.result = Flaky(client.result, failures)
    run_id = s.c.post("/api/runs", json={"request": "x", "agent": "research"}).json()["id"]
    assert wait_for(lambda: s.gw.store.get(run_id)["status"] == core.FINISHING and s.gw.feeds[run_id].finished)
    return s, client, run_id


def test_a_failed_fetch_stays_pending_and_is_retried(tmp_path):
    s, client, run_id = start(tmp_path, failures=1)
    assert s.c.get(f"/api/runs/{run_id}/result").status_code == 404            # まだ「完了」にしない
    assert next(r for r in s.c.get("/api/runs").json()["runs"] if r["id"] == run_id)["status"] == core.FINISHING
    s.gw.check_relays()                                                        # 見張り
    assert s.gw.store.get(run_id)["status"] == "done"
    assert s.c.get(f"/api/runs/{run_id}/result").json()["report"] == "[]"
    assert not list(tmp_path.glob("*.tmp"))                                    # 一時ファイルは置き換えた


def test_a_restarted_gateway_fetches_the_pending_result(tmp_path):
    s, client, run_id = start(tmp_path, failures=1)
    events = len(s.gw.store.events(run_id))
    # ゲートウェイだけを立ち上げ直す（エージェントはそのまま）
    again = create_app(registry=s.registry, runs_dir=tmp_path, api_key="", catalog=None)
    gw = again.state.gateway
    assert wait_for(lambda: gw.store.get(run_id)["status"] == "done")
    assert gw.result(run_id)["report"] == "[]" and gw.result(run_id)["artifacts"]
    # 調査も終わりのイベントも二重にしない。足すのは、保存できた印（run.saved）の 1 件だけ
    assert len(gw.store.events(run_id)) == events + 1 and gw.store.events(run_id)[-1]["type"] == "run.saved"
    assert client.result.calls == 2


def test_after_a_long_time_the_report_text_is_kept(tmp_path):
    s, client, run_id = start(tmp_path, failures=99)
    assert s.gw.retry_results() == []
    assert s.gw.retry_results(now=time.time() + core.RESULT_GIVE_UP_SECONDS + 1) == [run_id]
    result = s.gw.result(run_id)
    assert result["report"] == "[]" and result["artifacts"] == [] and "取得できなかった" in result["note"]
    assert s.gw.store.get(run_id)["status"] == "done"
