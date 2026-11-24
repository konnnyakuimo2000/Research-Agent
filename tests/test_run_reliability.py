"""2026-11-21 のマーティン先生の調査で見つかった、調査とゲートウェイの不具合の直し。

1. 「狙って探したページを読む」の空回り: 読んだページの数え方を、判断（guard）と読む処理（read_pages）でそろえる
2. ゲートウェイの一覧が「実行中」のまま戻らない: 中継の見張り、想定外の失敗でのつなぎ直し、一覧の遅さ
3. 研究者データベースの表の中身（<label> の中）が本文から落ちる
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import trafilatura

from research_agent.agent.guard import pages_to_read, research_step
from research_agent.agentkit import protocol
from research_agent.gateway import core
from research_agent.gateway.core import Gateway
from research_agent.gateway.registry import Registry
from research_agent.gateway.store import Feed, RunStore
from research_agent.tools import fetch

# ---- 1. 読んだページの数え方 ----

PRIORITY = ["https://db.example.ac.jp/KgApp/k03/resid/S43;jsessionid=ABC",       # 表記が違うだけの既読
            "https://db.example.ac.jp/KgApp/k03/resid/S44"]                         # 転送先として既読
READ = [{"url": "https://db.example.ac.jp/KgApp/k03/resid/S43", "final_url": "https://db.example.ac.jp/KgApp/k03/resid/S43",
         "ok": True},
        {"url": "https://db.example.ac.jp/KgApp?kyoinId=x", "final_url": "https://db.example.ac.jp/KgApp/k03/resid/S44",
         "ok": True}]


def test_pages_read_under_another_spelling_or_as_a_redirect_are_not_read_again():
    state = {"priority_urls": PRIORITY, "reading_list": [], "pages_read": READ, "verified": [{"text": "x"}],
             "coverage": [], "conflicts_checked_at": 1}
    assert pages_to_read(state) == []
    # 前は判断の側が URL をそのまま比べ、「狙って探したページを読む」を何も読めないまま上限まで繰り返した
    step = research_step(state, {"action": "respond"})
    assert step is None or step["action"] != "read_pages"


def test_unread_priority_pages_are_still_read_first():
    state = {"priority_urls": PRIORITY + ["https://new.example.org/cv"], "reading_list": ["https://other.example.org/"],
             "pages_read": READ}
    assert pages_to_read(state) == ["https://new.example.org/cv", "https://other.example.org/"]


# ---- 3. 研究者データベースの表 ----

RAWEB_LIKE = """<html><head><title>研究者情報</title></head><body><div id="main">
<h2>職歴</h2><table><tbody>
""" + "".join(f"""<tr><td class="k03-number">{i}.</td><td class="k03-periodYMD">{2000 + i}/04/01 ～</td>
<td><label class="k03-gyoseki-title"><a href="/k04/{i}" class="disabled">青山学院大学 理工学部 職名{i}</a></label></td></tr>
""" for i in range(1, 9)) + "</tbody></table></div></body></html>"


def test_table_cells_inside_labels_are_kept():
    assert "職名3" not in (trafilatura.extract(RAWEB_LIKE, include_tables=True, favor_precision=True) or "")
    text = trafilatura.extract(fetch._unwrap_labels(RAWEB_LIKE), include_tables=True, favor_precision=True) or ""
    assert "青山学院大学 理工学部 職名3" in text
    assert fetch._unwrap_labels("<LABEL a>x</label><labelx>") == "<span a>x</span><labelx>"


# ---- 2. 中継 ----

def test_pings_are_reported_so_the_relay_can_tell_it_is_alive():
    out = list(protocol.read_sse([": ping", "", "data: {\"seq\": 1}", ""]))
    assert out == [("ping", {}), ("message", {"seq": 1})]


class SlowClient:
    """1 回目の中継は何も受け取らないまま止まり（スリープの後の接続）、2 回目はすべて返すエージェント。"""
    id = "research"

    def __init__(self):
        self.calls = 0
        self.hang = threading.Event()

    def events(self, run, after=0):
        self.calls += 1
        if self.calls == 1:
            self.hang.wait(10)                      # 止まった接続（テストの終わりに放す）
            yield "message", {"v": 1, "seq": 9, "t": 1, "type": "status", "data": {}}   # 古い中継が目を覚ましても書かない
            return
        for seq, kind in ((1, "run.start"), (2, "run.error")):
            if seq > after:
                yield "message", {"v": 1, "seq": seq, "t": seq, "type": kind, "data": {}}
        yield "end", {"status": "error"}

    def result(self, run):
        return {}


def gateway_with(client, tmp_path: Path) -> Gateway:
    gw = Gateway(Registry([]), RunStore(tmp_path))
    gw.registry.clients = {"research": client}
    gw.store.create("r1", "research", "1", "a1", "x")
    gw.feeds["r1"] = Feed()
    return gw


def test_a_stalled_relay_is_restarted_by_the_watch(tmp_path):
    client = SlowClient()
    gw = gateway_with(client, tmp_path)
    gw._ensure_relay("r1")
    time.sleep(0.1)
    assert gw.check_relays() == []                                   # まだ止まったとはみなさない
    assert gw.check_relays(now=time.time() + core.STALL_SECONDS + 1) == ["r1"]
    end = time.time() + 5
    while gw.store.get("r1")["status"] != "error" and time.time() < end:
        time.sleep(0.02)
    assert gw.store.get("r1")["status"] == "error" and gw.feeds["r1"].finished
    client.hang.set()                                                # 古い中継が目を覚ます
    time.sleep(0.1)
    assert [e["seq"] for e in gw.store.events("r1")] == [1, 2]      # 古い中継は何も書かずに抜ける


def test_a_relay_that_died_is_restarted(tmp_path):
    class Broken(SlowClient):
        def events(self, run, after=0):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("unexpected")       # 想定外の失敗でも、中継を黙って終わらせない
            yield from super().events(run, after)

    gw = gateway_with(Broken(), tmp_path)
    gw.store.update("r1", status="running")
    old = core.RETRY_SECONDS
    core.RETRY_SECONDS = 0.01
    try:
        gw._ensure_relay("r1")
        end = time.time() + 5
        while gw.store.get("r1")["status"] != "error" and time.time() < end:
            time.sleep(0.02)
    finally:
        core.RETRY_SECONDS = old
    assert gw.store.get("r1")["status"] == "error"


def test_the_watch_restarts_a_relay_that_is_not_running(tmp_path):
    client = SlowClient()
    client.calls = 1                                                 # 次の中継はすべて返す
    gw = gateway_with(client, tmp_path)
    gw.store.update("r1", status="running")
    assert gw.check_relays() == ["r1"]                               # 中継していないのに「実行中」
    end = time.time() + 5
    while gw.store.get("r1")["status"] != "error" and time.time() < end:
        time.sleep(0.02)
    assert gw.store.get("r1")["status"] == "error"
    gw.store.update("r1", status="waiting")
    gw.feeds["r2"] = Feed()
    assert gw.check_relays() == []                                   # 答え待ち・終わった依頼はつなぎ直さない


def test_the_listing_does_not_rescan_the_runs_folder_every_time(tmp_path):
    store = RunStore(tmp_path)
    store.listing()
    (tmp_path / "20260101-000000.events.jsonl").write_text(
        '{"seq": 1, "t": 0, "type": "run.start", "request": "old"}\n{"seq": 2, "t": 1, "type": "run.end"}\n',
        encoding="utf-8")
    assert store.listing() == []                                     # 30 秒以内はフォルダーを見直さない
    assert store.import_legacy(force=True) == 1
    assert [r["request"] for r in store.listing()] == ["old"]
