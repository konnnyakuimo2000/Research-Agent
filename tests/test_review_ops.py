"""再レビュー（.claude/review-fixes.md）の「運用・設定の修正」。

- Open WebUI の答え待ちを、ゲートウェイを立ち上げ直しても続けられる（SQLite に置く。渡せてから消す）
- 中継を作り直しても、エージェントから受け取れなくなった時刻を引き継ぎ、通算で上限を超えたら止める
- LLM の待ち時間を、ノードの実行（調査）ごとに測る（同時に動く別の調査の待ちを混ぜない。重なりは除く）
- 読むページの数を、選ぶ時点で残りの枠に収める（追加の枠は狙って探したページだけ）
狭い画面での履歴の開閉は、ブラウザで確かめた（978px・1500px）。
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from research_agent.agent.graph import LLM_OUTAGE, timed
from research_agent.agent.guard import pages_to_read
from research_agent.gateway import core
from research_agent.gateway.app import create_app
from research_agent.gateway.store import Feed
from research_agent.llm import WAIT_METER, ContextPool, WaitMeter
from test_gateway import FIRST, Stack, wait_for
from test_run_reliability import SlowClient, gateway_with


# ---- Open WebUI の答え待ち ----

def test_a_waiting_conversation_survives_a_gateway_restart(tmp_path):
    s = Stack(tmp_path)
    r1 = s.post(FIRST)
    assert "どれのことですか" in r1
    # エージェントは動かしたまま、ゲートウェイだけを立ち上げ直す
    again = Stack.__new__(Stack)
    again.graph, again.llm, again.research, again.chat, again.registry, again.tmp = (
        s.graph, s.llm, s.research, s.chat, s.registry, tmp_path)
    again.app = create_app(registry=s.registry, runs_dir=tmp_path, api_key="", catalog=None)
    from fastapi.testclient import TestClient
    again.c = TestClient(again.app)
    assert len(again.app.state.sessions.pending) == 1
    r2 = again.post(FIRST + [{"role": "assistant", "content": r1}, {"role": "user", "content": "1"}])
    assert r2.endswith("マーキュリーについて調べて → tag0:1")                # 新しい依頼ではなく、元の依頼を再開した
    assert again.app.state.sessions.pending == {}                           # 渡せたので対応を消した


def test_a_failed_handover_can_be_retried(tmp_path):
    s = Stack(tmp_path)
    r1 = s.post(FIRST)
    with patch.object(s.gw, "answer", side_effect=core.Unavailable("一時的に落ちている")):
        failed = s.post(FIRST + [{"role": "assistant", "content": r1}, {"role": "user", "content": "1"}])
    assert "つながりません" in failed and len(s.app.state.sessions.pending) == 1   # 対応は残る
    r2 = s.post(FIRST + [{"role": "assistant", "content": r1}, {"role": "user", "content": "1"}])
    assert r2.endswith("tag0:1") and s.app.state.sessions.pending == {}


# ---- 中継の障害の開始時刻 ----

class SilentClient(SlowClient):
    """つないでも何も返さない（PC のスリープの後の接続のように、待ち続ける）。"""

    def events(self, run, after=0):
        self.calls += 1
        self.hang.wait(10)
        return
        yield


def test_the_outage_clock_survives_relay_restarts(tmp_path):
    client = SilentClient()
    gw = gateway_with(client, tmp_path)
    gw.store.update("r1", status="running")
    feed = gw.feeds["r1"]
    gw._ensure_relay("r1")
    time.sleep(0.05)
    t0 = feed.seen
    # 止まっていると判定するたびに中継を作り直すが、障害の開始は最初の時刻のまま
    assert gw.check_relays(now=t0 + core.STALL_SECONDS + 1) == ["r1"]
    first = feed.failed_since
    assert first == t0
    assert gw.check_relays(now=t0 + 2 * core.STALL_SECONDS + 2) == ["r1"]
    assert feed.failed_since == first
    # 通算で上限を超えたら止め、購読している側にも終わりを知らせる
    assert gw.check_relays(now=t0 + core.GIVE_UP_SECONDS + 1) == []
    assert gw.store.get("r1")["status"] == "stopped" and feed.finished
    client.hang.set()


def test_receiving_again_resets_the_outage_clock(tmp_path):
    feed = Feed()
    feed.failed_since = 123.0
    gw = gateway_with(SlowClient(), tmp_path)
    gw.feeds["r1"] = feed
    client = gw.registry.clients["research"]
    client.calls = 1                                                      # 次の中継はすべて返す
    gw.store.update("r1", status="running")
    gw._ensure_relay("r1")
    assert wait_for(lambda: feed.finished)
    assert feed.failed_since == 0.0


# ---- LLM の待ち時間 ----

def test_waits_are_measured_per_run_and_overlaps_count_once():
    results = {}
    barrier = threading.Barrier(2)

    def node_a(state):
        barrier.wait()
        time.sleep(0.2)                                                   # A は待たない
        return {}

    def node_b(state):
        barrier.wait()
        with ContextPool(max_workers=2) as pool:                          # B の中で 2 つのページを並行に読み、両方で待つ
            list(pool.map(lambda _: (time.sleep(0.2), WAIT_METER.get().add(0.15)), range(2)))
        return {}

    def run(name, fn):
        results[name] = timed("read_pages", fn)({"failures": []})

    threads = [threading.Thread(target=run, args=("a", node_a)), threading.Thread(target=run, args=("b", node_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    a, b = results["a"], results["b"]
    assert "llm_wait" not in a["trace"][0] and LLM_OUTAGE not in a.get("failures", [])   # A に B の待ちが混ざらない
    assert 0 < b["trace"][0]["llm_wait"] <= b["trace"][0]["seconds"] and LLM_OUTAGE in b["failures"]


def test_the_meter_counts_overlapping_waits_once():
    m = WaitMeter()
    with patch("research_agent.llm.time.perf_counter", side_effect=[10.0, 10.0, 30.0]):
        m.add(5)                                                          # 5〜10
        m.add(3)                                                          # 7〜10（重なり）
        m.add(4)                                                          # 26〜30
    assert m.seconds() == 9.0


# ---- 読むページの数 ----

def test_pages_are_limited_when_chosen():
    state = {"settings": {"max_pages": 2}, "pages_read": [],
             "reading_list": [f"https://ex.org/{i}" for i in range(4)]}
    assert pages_to_read(state, 4) == ["https://ex.org/0", "https://ex.org/1"]          # 前は 4 件読んだ
    one_left = {**state, "pages_read": [{"url": "https://ex.org/0", "ok": True}]}
    assert pages_to_read(one_left, 4) == ["https://ex.org/1"]
    full = {**state, "pages_read": [{"url": f"https://ex.org/{i}", "ok": True} for i in range(2)],
            "priority_urls": ["https://gov.example.go.jp/p"]}
    assert pages_to_read(full, 4) == ["https://gov.example.go.jp/p"]                     # 追加の枠は狙ったページだけ
    tried = {**state, "pages_read": [{"url": f"https://ex.org/x{i}", "ok": False} for i in range(24)]}
    assert pages_to_read(tried, 4) == []                                                  # 失敗を含めた試行の上限
