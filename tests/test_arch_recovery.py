"""全体アーキテクチャの分析（.claude/architecture-review.md）の 1: 立ち上げ直したとき、一覧（SQLite）より先に
イベントの記録（JSONL）へ書かれていたイベントを、一覧に当てはめてから中継・成果物の取得に戻す。

前は、_add が JSONL に書いた後・SQLite を更新する前に止まると、終わりのイベントは記録にあるのに一覧は
「実行中」のまま、成果物も取りに行かなかった。
"""

from __future__ import annotations

import time

from research_agent.gateway.core import Gateway
from research_agent.gateway.registry import Registry
from research_agent.gateway.store import RunStore


class Agent:
    id = "research"
    error = ""

    def __init__(self):
        self.relayed, self.results = 0, 0

    def manifest(self):
        return {"name": "調査"}

    def events(self, run, after=0):
        self.relayed += 1                         # 終わった依頼の中継は始めないはず
        yield "rest", {}

    def result(self, run):
        self.results += 1
        return {"report": "東京タワーの高さは 333 m [1]", "artifacts": []}


def env(seq, kind, data=None):
    return {"v": 1, "seq": seq, "t": seq * 1.5, "type": kind, "data": data or {}, "run_id": "r1"}


def crashed_store(tmp_path, events, status="running"):
    """イベントを JSONL に書いたところで止まった状態（一覧は始めたときのまま）。"""
    store = RunStore(tmp_path)
    store.create("r1", "research", "1", "a1", "東京タワーの高さ", hidden=True)   # Open WebUI から始めた依頼も戻す
    store.update("r1", status=status)
    for e in events:
        store.append("r1", e)
    return store


def restart(tmp_path, agent):
    gw = Gateway(Registry([]), RunStore(tmp_path))
    gw.registry.clients = {"research": agent}
    gw.resume_live()
    end = time.time() + 5
    while (gw.store.get("r1") or {}).get("status") == "finishing" and time.time() < end:
        time.sleep(0.02)
    return gw


def test_an_end_written_only_to_the_log_is_finished_on_restart(tmp_path):
    crashed_store(tmp_path, [env(1, "run.start", {"request": "東京タワーの高さ"}), env(2, "status"),
                             env(3, "run.end", {"report": "東京タワーの高さは 333 m [1]"})])
    agent = Agent()
    gw = restart(tmp_path, agent)
    rec = gw.store.get("r1")
    assert rec["status"] == "done" and rec["events"] == 3 and rec["seconds"] == 4.5
    assert gw.result("r1")["report"] == "東京タワーの高さは 333 m [1]"
    assert agent.relayed == 0 and agent.results == 1                       # 終わった依頼の中継は始めない
    # 同じ記録のまま、もう 1 回立ち上げ直しても、状態も件数も変わらない（成果物も取り直さない）
    gw2 = restart(tmp_path, agent)
    rec2 = gw2.store.get("r1")
    assert (rec2["status"], rec2["events"], rec2["seconds"]) == ("done", 3, 4.5)
    assert agent.results == 1 and len(gw2.store.events("r1")) == 3


def test_a_question_written_only_to_the_log_shows_as_waiting(tmp_path):
    crashed_store(tmp_path, [env(1, "run.start"), env(2, "run.waiting", {"question": "どちらの東京タワー？"})])
    gw = restart(tmp_path, Agent())
    rec = gw.store.get("r1")
    assert rec["status"] == "waiting" and rec["question"] == "どちらの東京タワー？" and rec["events"] == 2
    assert gw.feeds["r1"].last_seq == 2                                    # 中継は記録の続きから


def test_errors_and_cancels_written_only_to_the_log_are_applied(tmp_path):
    crashed_store(tmp_path, [env(1, "run.start"), env(2, "run.error", {"error": "x"})])
    gw = restart(tmp_path, agent := Agent())
    assert gw.store.get("r1")["status"] == "error" and agent.relayed == 0 and "r1" not in gw.feeds


def test_a_list_that_is_up_to_date_is_left_alone(tmp_path):
    store = crashed_store(tmp_path, [env(1, "run.start"), env(2, "status")])
    store.update("r1", events=2, seconds=3.0)
    before = store.get("r1")["updated"]
    gw = restart(tmp_path, agent := Agent())
    rec = gw.store.get("r1")
    assert rec["status"] == "running" and rec["events"] == 2 and rec["updated"] == before
    end = time.time() + 2
    while agent.relayed == 0 and time.time() < end:
        time.sleep(0.02)
    assert agent.relayed == 1                                              # 動いている依頼は中継を続ける
