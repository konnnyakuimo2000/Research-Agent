"""ゲートウェイの中心: 依頼をエージェントに渡し、イベントを受け取って保存し、画面・入口へ配る。

- 依頼ごとに「中継」のスレッドを 1 本動かし、エージェントのイベントの流れ（SSE）を受け取る。
  接続が切れたら、最後に受け取った番号の続きからつなぎ直す
- エージェントが答え待ちで休んだら（rest）中継も休み、答えを送ったときにつなぎ直す
- ゲートウェイを立ち上げ直したら、動いていた依頼の中継を続きから始め直す（エージェントが覚えていれば続く）
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator

import httpx

from research_agent.agentkit import protocol
from research_agent.gateway.models import ModelCatalog, effective_models
from research_agent.gateway.registry import AgentClient, AgentError, Registry
from research_agent.gateway.settings import check_settings, effective_settings
from research_agent.gateway.store import Feed, RunStore

log = logging.getLogger(__name__)

LIVE = ("running", "paused", "waiting")
# 終わりのイベント（run.end）を受け取ったら「保存中」。成果物をエージェントから取って保存できたら「完了」にする。
# 「保存中」は、成果物の取得待ちの記録を兼ねる（ゲートウェイを立ち上げ直しても、取り直せる）
FINISHING = "finishing"
STATUS_OF = {"run.waiting": "waiting", "run.paused": "paused", "run.resumed": "running", "run.end": FINISHING,
             "run.error": "error", "run.cancelled": "cancelled"}
RETRY_SECONDS = 2.0          # 中継が切れたときに、つなぎ直すまでの最初の待ち（倍々に延ばす）
GIVE_UP_SECONDS = 1800.0     # この間ずっとつなげなければ、「途中で止まった」にする
FEED_KEEP_SECONDS = 600.0
# 見張り: エージェントは 15 秒ごとに生存の合図を送る。中継がこの秒数なにも受け取っていなければ、止まったとみなして
# つなぎ直す（PC のスリープの後に、接続が何も受け取らないまま待ち続け、一覧が「実行中」のまま戻らないことがあった）
STALL_SECONDS = 90.0
WATCH_SECONDS = 30.0
RESULT_GIVE_UP_SECONDS = 1800.0   # 成果物をこの間とれなければ、イベントにある報告の本文だけで結果を作る


class Unavailable(Exception):
    """エージェントにつながらない。"""


class RunBusy(Exception):
    """まだ終わっていない依頼（削除できない）。"""


class Gateway:
    def __init__(self, registry: Registry, store: RunStore, catalog: ModelCatalog | None = None) -> None:
        self.registry = registry
        self.store = store
        self.catalog = catalog
        self.feeds: dict[str, Feed] = {}
        self._wanted: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._add_lock = threading.Lock()        # 古い中継と新しい中継が同じイベントを二重に書かないように
        self._saving: set[str] = set()           # 成果物を取りに行っている依頼（同じ依頼を二重に取りに行かない）
        self._finished_at: dict[str, float] = {}

    # ---- エージェント ----

    def agents(self) -> list[dict]:
        out = []
        for c in self.registry.clients.values():
            m = c.manifest() or {}
            out.append({"id": c.id, "name": m.get("name", c.id), "version": m.get("version", ""),
                        "description": m.get("description", ""), "levels": m.get("levels", [0]),
                        "extensions": m.get("extensions", []), "capabilities": m.get("capabilities", []),
                        "graph": m.get("graph"), "model_roles": m.get("model_roles", []),
                        "settings_schema": m.get("settings_schema") or {}, "presets": m.get("presets") or {},
                        "ok": bool(m) and not c.error, "error": c.error,
                        "default": c.id == self.registry.default})
        return out

    # ---- 依頼 ----

    def models(self) -> dict | None:
        """モデルの窓口の一覧（窓口を使わない構成・つながらないときは None）。"""
        return self.catalog.get() if self.catalog else None

    def start(self, agent_id: str, text: str, extra: dict | None = None, hidden: bool = False,
              settings: dict | None = None, models: dict | None = None) -> dict:
        """依頼を始める。models は役割ごとの選び直し（選べないモデルなら ModelChoiceError）。"""
        client = self.registry.get(agent_id)
        manifest = client.manifest()
        if manifest is None:
            raise Unavailable(client.error or "エージェントにつながりません")
        chosen = effective_models(manifest, models, self.models())   # 役割ごとに実際に使うモデル（記録に残す）
        schema = manifest.get("settings_schema") or {}
        settings = check_settings(schema, settings or {})
        try:
            r = client.start({"text": text, **(extra or {})}, settings, chosen)
        except httpx.HTTPError as e:
            raise Unavailable(f"{type(e).__name__}: {e}") from None
        self._prune()
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.store.create(run_id, client.id, manifest.get("version", ""), r["run_id"], text, settings, hidden, chosen,
                          effective_settings(schema, settings))
        self.feeds[run_id] = Feed()
        self._ensure_relay(run_id)
        return self.summary(run_id)

    def answer(self, run_id: str, text: str) -> dict:
        rec, client = self._rec(run_id)
        client.answer(rec["agent_run"], text)
        self.store.update(run_id, status="running", question="")
        self._ensure_relay(run_id)
        return self.summary(run_id)

    def control(self, run_id: str, action: str) -> dict:
        rec, client = self._rec(run_id)
        client.control(rec["agent_run"], action)
        self._ensure_relay(run_id)        # 答え待ちで休んでいた依頼を止めたとき、その知らせを受け取る
        return self.summary(run_id)

    def delete(self, run_id: str) -> dict:
        """終わった依頼を、このアプリの一覧・イベント・結果・配信用のイベントから消す。動いている・答え待ち・
        一時停止中・報告を保存中の依頼は消さない（RunBusy）。Open WebUI の会話とエージェントの記録は消さない。"""
        rec = self.store.get(run_id)
        if rec is None:
            raise KeyError(run_id)
        if rec.get("status") in (*LIVE, FINISHING):
            raise RunBusy("調査が終わってから削除できます")
        cleaned = self.store.delete(run_id)
        if self.catalog and rec.get("agent_run"):
            self.catalog.delete_call_log(rec["agent_run"])     # 窓口に残った LLM とのやり取りも消す
        with self._lock:
            self.feeds.pop(run_id, None)
            self._finished_at.pop(run_id, None)
            self._wanted.pop(run_id, None)
        return {"id": run_id, "deleted": True, "cleaned": cleaned}

    def summary(self, run_id: str) -> dict:
        return _summary(self.store.get(run_id) or {})

    def listing(self, limit: int = 50) -> list[dict]:
        return [_summary(r) for r in self.store.listing(limit)]

    def usage(self, run_id: str) -> dict | None:
        """調査の費用（モデルの窓口が数えたもの）。窓口を使わない構成・つながらないときは None。"""
        rec = self.store.get(run_id)
        if rec is None or self.catalog is None:
            return None
        if not rec.get("agent_run"):                        # 分ける前の記録（窓口を通していない）
            return {"usd": 0.0, "models": {}, "limit_usd": None}
        return self.catalog.run_usage(rec["agent_run"])

    def llm_log(self, run_id: str) -> str | None:
        """LLM とのやり取りの記録のテキスト。依頼がなければ KeyError、記録がなければ None。"""
        from research_agent.gateway.llm_log import render
        rec = self.store.get(run_id)
        if rec is None:
            raise KeyError(run_id)
        if self.catalog is None or not rec.get("agent_run"):
            return None
        calls = self.catalog.call_log(rec["agent_run"])
        if calls is None:
            return None
        client = self.registry.clients.get(rec["agent"])
        roles = {r["role"]: r.get("title", r["role"]) for r in ((client.manifest() or {}).get("model_roles", []) if client else [])}
        return render(rec, calls, roles)

    def result(self, run_id: str) -> dict | None:
        return self.store.result(run_id)

    def _rec(self, run_id: str) -> tuple[dict, AgentClient]:
        rec = self.store.get(run_id)
        if rec is None:
            raise KeyError(run_id)
        return rec, self.registry.get(rec["agent"])

    # ---- イベント ----

    def events(self, run_id: str) -> list[dict]:
        feed = self.feeds.get(run_id)
        return list(feed.events) if feed else self.store.events(run_id)

    def follow(self, run_id: str, after: int = 0, heartbeat: float = 15.0) -> Iterator[dict | None]:
        """after より後のイベントを順に返す。動いている依頼は届くのを待つ（待っている間は None を返す）。"""
        feed = self.feeds.get(run_id)
        if feed is None:
            yield from (e for e in self.store.events(run_id) if e["seq"] > after)
            return
        seq = after
        while True:
            new = feed.wait_after(seq, heartbeat)
            for e in new:
                yield e
                seq = e["seq"]
            if not new:
                if feed.finished:
                    return
                yield None

    # ---- 中継 ----

    def resume_live(self) -> None:
        """立ち上げ直したとき: 動いていた依頼の中継を、記録の続きから始め直す。成果物の保存を待っていた依頼は、
        成果物だけを取り直す（調査もイベントの中継もやり直さない）。

        先に、一覧（SQLite）より先にイベントの記録（JSONL）へ書かれていたイベントを、一覧に当てはめる（_reconcile）。
        _add は JSONL に書いてから SQLite を更新するので、その間で止まると、終わりのイベントは記録にあるのに一覧は
        「実行中」のまま、成果物も取りに行かないままになっていた（全体アーキテクチャの分析 1）。"""
        for rec in self.store.listing(1000, LIVE, hidden=True):
            rec = self._reconcile(rec)
            if rec.get("status") not in LIVE:
                continue                                  # 記録の上では終わっていた（成果物の取得は下で）
            feed = Feed()
            for e in self.store.events(rec["id"]):
                feed.add(e)
            self.feeds[rec["id"]] = feed
            self._ensure_relay(rec["id"])
        if self.store.listing(1000, (FINISHING,), hidden=True):
            threading.Thread(target=self.retry_results, daemon=True, name="result-retry").start()

    def _reconcile(self, rec: dict) -> dict:
        """一覧が知っている通番（events）より後のイベントを、通番の順に一覧の状態へ当てはめる。何度呼んでも同じ
        （当てはめた分だけ events が進むので、2 回目は何もしない）。当てはめた後の記録を返す。"""
        done, fields = rec.get("events") or 0, {}
        for env in sorted(self.store.events(rec["id"]), key=lambda e: e.get("seq", 0)):
            if env.get("seq", 0) <= done:
                continue
            fields["events"], fields["seconds"] = env["seq"], env.get("t", 0)
            status = STATUS_OF.get(env.get("type"))
            if status:
                fields["status"] = status
                fields["question"] = (env.get("data") or {}).get("question", "") if status == "waiting" else ""
        if fields:
            log.warning("run %s: applied %d events recorded before the list was updated (status %s → %s)",
                        rec["id"], fields["events"] - done, rec.get("status"), fields.get("status", rec.get("status")))
            self.store.update(rec["id"], **fields)
            rec = {**rec, **fields}
        return rec

    def _ensure_relay(self, run_id: str, force: bool = False) -> None:
        """中継を始める。force なら、動いている（はずの）中継があっても新しい世代で始め直す（見張りから）。"""
        feed = self.feeds.get(run_id)
        if feed is None or feed.finished:
            return
        with self._lock:
            self._wanted[run_id] = True
            if feed.relaying and not force:
                return
            feed.relaying = True
            feed.generation += 1
            feed.seen = time.time()
            gen = feed.generation
        threading.Thread(target=self._relay, args=(run_id, feed, gen), daemon=True,
                         name=f"relay-{run_id}-{gen}").start()

    def _relay(self, run_id: str, feed: Feed, gen: int = 0) -> None:
        rec = self.store.get(run_id)
        client = self.registry.clients.get(rec["agent"]) if rec else None
        wait = RETRY_SECONDS
        stale = lambda: feed.generation != gen    # noqa: E731 - 見張りが新しい中継を始めた
        while True:
            with self._lock:
                self._wanted[run_id] = False
            outcome = "error"
            try:
                if client is None:
                    outcome = "missing"
                else:
                    for name, data in client.events(rec["agent_run"], after=feed.last_seq):
                        if stale():
                            return                       # 新しい中継に任せる（状態には触らない）
                        feed.seen = time.time()
                        feed.failed_since, wait = 0.0, RETRY_SECONDS
                        if name == "message":
                            self._add(run_id, feed, data)
                        elif name in ("end", "rest", "missing"):
                            outcome = name
                            break
            except (httpx.HTTPError, AgentError) as e:
                log.warning("relay %s: %s", run_id, e)
            except Exception:  # noqa: BLE001 - 想定外の失敗でも中継を黙って終わらせない（一覧が「実行中」のまま残る）
                log.exception("relay %s failed unexpectedly; reconnecting", run_id)
            if stale():
                return
            if outcome == "end":
                self._finish(run_id, client, rec, feed)
                return self._stop_relay(feed, gen)
            if outcome == "missing":
                self.store.update(run_id, status="stopped")
                feed.finish()
                return self._stop_relay(feed, gen)
            if outcome == "rest":
                with self._lock:
                    if not self._wanted.get(run_id) and feed.generation == gen:
                        feed.relaying = False
                        return
                continue
            # 切れた: 待ってからつなぎ直す。長くつなげなければ諦める。開始時刻は依頼ごとに持つので、見張りが中継を
            # 作り直しても通算で数える（前はスレッドの中で持っていて、作り直すたびに数え直しになっていた）
            feed.failed_since = feed.failed_since or time.time()
            if time.time() - feed.failed_since > GIVE_UP_SECONDS:
                self._give_up(run_id, feed)
                return self._stop_relay(feed, gen)
            time.sleep(wait)
            wait = min(wait * 2, 30.0)

    def _give_up(self, run_id: str, feed: Feed) -> None:
        """長くエージェントから受け取れなかった: 「途中で止まった」にして、購読している画面にも終わりを知らせる。"""
        self.store.update(run_id, status="stopped")
        feed.finish()
        with self._lock:
            feed.relaying = False

    def _stop_relay(self, feed: Feed, gen: int | None = None) -> None:
        with self._lock:
            if gen is None or feed.generation == gen:
                feed.relaying = False

    def watch(self) -> None:
        """見張りを始める（ゲートウェイの立ち上げで 1 回）。"""
        threading.Thread(target=self._watch_loop, daemon=True, name="relay-watch").start()

    def _watch_loop(self) -> None:
        while True:
            time.sleep(WATCH_SECONDS)
            try:
                self.check_relays()
            except Exception:  # noqa: BLE001 - 見張りは止めない
                log.exception("relay watch failed")

    def check_relays(self, now: float | None = None) -> list[str]:
        """止まった中継をつなぎ直す。つなぎ直した依頼を返す。

        - 中継しているはずなのに STALL_SECONDS なにも受け取っていない（生存の合図も来ない）
        - 中継が止まっているのに、記録はまだ「実行中・一時停止中」（答え待ちのときは、答えが来るまで中継しない）
        """
        now = time.time() if now is None else now
        restarted = []
        for run_id, feed in list(self.feeds.items()):
            if feed.finished:
                continue
            status = (self.store.get(run_id) or {}).get("status")
            stalled = feed.relaying and now - feed.seen > STALL_SECONDS
            dropped = not feed.relaying and status in ("running", "paused")
            if stalled:
                # 何も届かない状態も障害として数える（最後に受け取った時から）。つなぎ直しても通算で上限を超えたら止める
                feed.failed_since = feed.failed_since or feed.seen
                if now - feed.failed_since > GIVE_UP_SECONDS:
                    log.warning("relay %s gave up after %.0f s without events", run_id, now - feed.failed_since)
                    feed.generation += 1                 # 動いている中継は、目が覚めたら自分で抜ける
                    self._give_up(run_id, feed)
                    continue
            if stalled or dropped:
                log.warning("relay %s %s; reconnecting", run_id, "stalled" if stalled else "was not running")
                self._ensure_relay(run_id, force=True)
                restarted.append(run_id)
        self.retry_results(now)
        return restarted

    def retry_results(self, now: float | None = None) -> list[str]:
        """成果物の保存を待っている依頼（保存中）の成果物を取り直す。保存できた依頼を返す（見張りと起動のときに呼ぶ）。"""
        saved = []
        for rec in self.store.listing(1000, (FINISHING,), hidden=True):
            client = self.registry.clients.get(rec["agent"])
            if self._save_result(rec["id"], client, rec, now):
                saved.append(rec["id"])
        return saved

    def _save_result(self, run_id: str, client: AgentClient | None, rec: dict, now: float | None = None) -> bool:
        """成果物をエージェントから取って保存し、「完了」にする。取れなければ「保存中」のまま（後で取り直す）。

        取れないまま RESULT_GIVE_UP_SECONDS たったら（エージェントも立ち上げ直して成果物を失ったなど）、
        イベントに残っている報告の本文だけで結果を作って保存する（いつまでも保存中のまま残さない）。"""
        with self._lock:
            if run_id in self._saving:
                return False
            self._saving.add(run_id)
        try:
            try:
                if client is None:
                    raise AgentError(404, "エージェントがない")
                result = client.result(rec["agent_run"])
            except (httpx.HTTPError, AgentError) as e:
                log.warning("could not fetch the result of %s: %s", run_id, e)
                if (now or time.time()) - (rec.get("updated") or 0) < RESULT_GIVE_UP_SECONDS:
                    return False
                result = {"report": self._report_from_events(run_id), "artifacts": [],
                          "note": "成果物をエージェントから取得できなかったため、報告の本文だけを保存した"}
            try:
                self.store.save_result(run_id, result)
            except OSError as e:
                log.warning("could not save the result of %s: %s", run_id, e)
                return False
            self._mark_saved(run_id)                     # 印を足してから完了にする（完了と見えたら、印もある）
            self.store.update(run_id, status="done")
            return True
        finally:
            with self._lock:
                self._saving.discard(run_id)

    def _mark_saved(self, run_id: str) -> None:
        """報告と成果物を保存できたことを、イベントの記録の最後に足す（画面が「保存した」と出せるように）。
        通番は記録の最後の続き。動いている依頼の配信にも足す（終わりを知らせる前）。"""
        with self._add_lock:
            feed = self.feeds.get(run_id)
            events = feed.events if feed and feed.events else self.store.events(run_id)
            last = events[-1] if events else {}
            env = {"v": 1, "seq": (last.get("seq") or 0) + 1, "t": last.get("t", 0), "run_id": run_id, "task_id": "",
                   "type": "run.saved", "data": {}}
            try:
                self.store.append(run_id, env)
            except OSError as e:
                log.warning("could not mark %s saved: %s", run_id, e)
                return
            self.store.update(run_id, events=env["seq"])
            if feed and not feed.finished:
                feed.add(env)

    def _report_from_events(self, run_id: str) -> str:
        for env in reversed(self.events(run_id)):
            if env.get("type") == "run.end":
                return (env.get("data") or {}).get("report", "")
        return ""

    def _add(self, run_id: str, feed: Feed, env: dict) -> None:
        with self._add_lock:
            if env.get("seq", 0) <= feed.last_seq:
                return                                   # つなぎ直したときに重なった分
            env = {**env, "run_id": run_id}
            self.store.append(run_id, env)
            fields = {"events": env["seq"], "seconds": env.get("t", 0)}
            status = STATUS_OF.get(env["type"])
            if status:
                fields["status"] = status
                fields["question"] = (env.get("data") or {}).get("question", "") if status == "waiting" else ""
            self.store.update(run_id, **fields)
            feed.add(env)

    def _finish(self, run_id: str, client: AgentClient, rec: dict, feed: Feed) -> None:
        current = self.store.get(run_id) or {}
        if current.get("status") == FINISHING:
            # 保存できなければ「保存中」のまま残り、見張りと起動のときに取り直す（前は一度失敗すると、一覧は完了なのに
            # 結果 API が 404 のままだった。再レビュー 5）
            self._save_result(run_id, client, current)
        feed.finish()
        self._finished_at[run_id] = time.time()

    def _prune(self) -> None:
        now = time.time()
        for k in [k for k, t in self._finished_at.items() if now - t > FEED_KEEP_SECONDS]:
            self.feeds.pop(k, None)
            self._finished_at.pop(k, None)
            self._wanted.pop(k, None)


def _summary(rec: dict) -> dict:
    out = {k: rec.get(k) for k in ("id", "agent", "request", "status", "question", "created", "events", "seconds")}
    for key in ("models", "settings"):          # 依頼ごとに選び直したモデルと設定（画面のチャットの頭に出す）
        try:
            out[key] = json.loads(rec.get(key) or "{}")
        except ValueError:
            out[key] = {}
    # 始めたときに効いた設定（既定を含む）。前の版の記録にはない（None。画面は「開始時の既定値は記録なし」）
    try:
        out["effective_settings"] = json.loads(rec["effective"]) if rec.get("effective") else None
    except ValueError:
        out["effective_settings"] = None
    return out


def flat_events(envs: list[dict]) -> list[dict]:
    return [protocol.flat(e) for e in envs]
