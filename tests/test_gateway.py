"""ゲートウェイ: 契約だけでエージェントとつなぐ。OpenAI 互換の入口（質問 → 答えで再開、裏処理）、画面の API、
0 段のチャットと調査エージェントが同じゲートウェイに載ること、偽物のエージェントへの差し替え、前の記録の再生。

エージェントは本物のサービス（agents.research / agents.chat）を、偽物のグラフ・LLM で同じプロセスの中に立てる。
"""

from __future__ import annotations

import itertools
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from research_agent.agentkit import protocol
from research_agent.agents import chat, research
from research_agent.config import Settings
from research_agent.gateway import openai_compat
from research_agent.gateway.app import create_app
from research_agent.gateway.registry import AgentClient, AgentEntry, Registry

SETTINGS = Settings("http://localhost:1/v1", "test-model", "k", "http://localhost:2")


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _tagged_graph():
    """依頼ごとに番号を付け（最初のノード）、質問への答えと一緒に返す。どの依頼が再開されたかがわかる。"""
    counter = itertools.count()

    class T(TypedDict, total=False):
        request: str
        tag: int
        answer: str

    def start(state: T) -> dict:
        return {"tag": next(counter)}

    def ask(state: T) -> dict:
        reply = interrupt({"question": "どれのことですか？\n1. 惑星\n2. 歌手"})
        return {"answer": f"{state['request']} → tag{state['tag']}:{reply}"}

    g = StateGraph(T)
    g.add_node("start", start)
    g.add_node("ask_user", ask)
    g.add_edge(START, "start")
    g.add_edge("start", "ask_user")
    g.add_edge("ask_user", END)
    return g.compile(checkpointer=InMemorySaver())


class FakeLLM:
    def __init__(self):
        self.messages = None

    def stream_messages(self, messages):
        self.messages = messages
        yield from ["こん", "にちは"]


class Stack:
    def __init__(self, tmp_path, graph=None, extra: list[AgentClient] | None = None, catalog=None):
        self.graph = graph or _tagged_graph()
        self.llm = FakeLLM()
        self.research = research.create_app(graph=self.graph, settings=SETTINGS, key="")
        self.chat = chat.create_app(llm=self.llm, settings=SETTINGS, key="")
        self.registry = Registry([
            AgentClient(AgentEntry("research", "http://testserver", "", "research-agent"), TestClient(self.research)),
            AgentClient(AgentEntry("chat", "http://testserver", "", "", True), TestClient(self.chat)),
            *(extra or [])])
        self.tmp = tmp_path
        self.app = create_app(registry=self.registry, runs_dir=tmp_path, api_key="", catalog=catalog)
        self.c = TestClient(self.app)

    @property
    def gw(self):
        return self.app.state.gateway

    def threads(self):
        return set(self.graph.checkpointer.storage)

    def post(self, messages, headers=None, stream=False):
        r = self.c.post("/v1/chat/completions", headers=headers or {},
                        json={"model": "research-agent", "messages": messages, "stream": stream})
        assert r.status_code == 200, r.text
        if not stream:
            return r.json()["choices"][0]["message"]["content"]
        lines = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ") and ln != "data: [DONE]"]
        return "".join(json.loads(x)["choices"][0]["delta"].get("content", "") for x in lines)


@pytest.fixture
def s(tmp_path):
    return Stack(tmp_path)


FIRST = [{"role": "user", "content": "マーキュリーについて調べて"}]


# ---- OpenAI 互換の入口（Open WebUI） ----

def test_models_lists_only_agents_with_an_openai_name(s):
    assert [m["id"] for m in s.c.get("/v1/models").json()["data"]] == ["research-agent"]


def test_question_then_resume(s):
    r1 = s.post(FIRST)
    assert "どれのことですか" in r1 and "<think>" in r1
    r2 = s.post(FIRST + [{"role": "assistant", "content": r1}, {"role": "user", "content": "1"}])
    assert r2.endswith("マーキュリーについて調べて → tag0:1")


def test_new_chat_is_not_resumed(s):
    s.post(FIRST)
    r = s.post([{"role": "user", "content": "別の依頼"}])
    assert "どれのことですか" in r   # 最初から始まる（前の会話の答えとして扱わない）


def test_background_task_goes_to_the_chat_agent_and_is_hidden(s):
    r = s.post([{"role": "user", "content": "### Task:\nGenerate a concise title"}])
    assert r == "こんにちは" and s.llm.messages[0]["content"].startswith("### Task:")
    assert s.c.get("/api/runs").json()["runs"] == []                 # 裏処理は画面の一覧に出さない


def test_stream_format(s):
    r = s.c.post("/v1/chat/completions", json={"model": "research-agent", "stream": True,
                                               "messages": [{"role": "user", "content": [{"type": "text", "text": "マーキュリー"}]}]})
    lines = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(x) for x in lines[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert "どれのことですか" in text and chunks[-1]["choices"][0]["finish_reason"] == "stop"


# ---- レビュー 4: 同じ依頼で始まる会話を、それぞれ正しく再開する ----

def test_two_tabs_with_the_same_request_resume_their_own_runs(s):
    tab = {"a": {"x-openwebui-user-id": "u1", "x-openwebui-chat-id": "chat-a"},
           "b": {"x-openwebui-user-id": "u1", "x-openwebui-chat-id": "chat-b"}}
    qa, qb = s.post(FIRST, tab["a"]), s.post(FIRST, tab["b"], stream=True)   # 両方が質問待ちになる
    # 逆の順に答える（b → a）
    rb = s.post(FIRST + [{"role": "assistant", "content": qb}, {"role": "user", "content": "2"}], tab["b"])
    ra = s.post(FIRST + [{"role": "assistant", "content": qa}, {"role": "user", "content": "1"}], tab["a"], stream=True)
    assert rb.endswith("tag1:2") and ra.endswith("tag0:1")


def test_another_user_cannot_resume_someone_elses_run(s):
    q = s.post(FIRST, {"x-openwebui-user-id": "u1", "x-openwebui-chat-id": "chat-a"})
    other = s.post(FIRST + [{"role": "assistant", "content": q}, {"role": "user", "content": "1"}],
                   {"x-openwebui-user-id": "u2", "x-openwebui-chat-id": "chat-a"})
    assert "どれのことですか" in other                                   # 再開されず、新しい依頼として始まる


def test_without_open_webui_ids_a_gateway_token_tells_conversations_apart(s):
    qa, qb = s.post(FIRST), s.post(FIRST, stream=True)
    assert qa != qb and "[//]: # (conv:" in qa                          # 表示されない行に、別々のトークン
    rb = s.post(FIRST + [{"role": "assistant", "content": qb}, {"role": "user", "content": "2"}], stream=True)
    ra = s.post(FIRST + [{"role": "assistant", "content": qa}, {"role": "user", "content": "1"}])
    assert rb.endswith("tag1:2") and ra.endswith("tag0:1")


# ---- レビュー 5: 終わった・失敗した・期限切れの会話の記録を消す ----

def test_checkpoints_are_removed_when_done_expired_or_failed(s, monkeypatch, tmp_path):
    q = s.post(FIRST)
    assert len(s.threads()) == 1                                        # 答えを待っている間は残す
    s.post(FIRST + [{"role": "assistant", "content": q}, {"role": "user", "content": "1"}])
    assert s.threads() == set()                                         # 終わったら消す

    s.post(FIRST)
    assert len(s.threads()) == 1
    monkeypatch.setattr(openai_compat, "PENDING_TTL", -1)               # 期限切れにする
    s.post([{"role": "user", "content": "別の依頼"}])                     # 次の依頼で期限切れを止める
    monkeypatch.setattr(openai_compat, "PENDING_TTL", 6 * 3600)
    assert len(s.threads()) == 1 and len(s.app.state.sessions.pending) == 1   # 残っているのは新しい依頼の分だけ
    assert "cancelled" in [r["status"] for r in s.c.get("/api/runs").json()["runs"]]

    class Broken(TypedDict, total=False):
        request: str

    def boom(state):
        raise RuntimeError("壊れた")
    g = StateGraph(Broken)
    g.add_node("x", boom)
    g.add_edge(START, "x")
    g.add_edge("x", END)
    b = Stack(tmp_path / "b", graph=g.compile(checkpointer=InMemorySaver()))
    r = b.post([{"role": "user", "content": "壊れる依頼"}])
    assert "エラー" in r and "壊れた" in r and b.threads() == set()     # 回復できない失敗も消す


# ---- 画面の API: 0 段のチャットと調査エージェントが同じゲートウェイに載る ----

def _envs(text: str) -> list[dict]:
    return [e for n, e in protocol.read_sse(text.splitlines()) if n == "message"]


def test_both_agents_are_listed_with_their_levels(s):
    agents = {a["id"]: a for a in s.c.get("/api/agents").json()["agents"]}
    assert agents["research"]["levels"] == [0, 1] and agents["research"]["extensions"] == ["research/v1"]
    assert agents["chat"]["levels"] == [0] and agents["chat"]["extensions"] == []
    assert agents["research"]["default"] and agents["research"]["ok"]


def test_chat_agent_runs_on_the_same_screen_api(s):
    run = s.c.post("/api/runs", json={"request": "こんにちは", "agent": "chat"}).json()
    assert run["agent"] == "chat"
    assert wait_for(lambda: s.gw.store.get(run["id"])["status"] == "done")
    envs = _envs(s.c.get(f"/api/runs/{run['id']}/events").text)
    assert [e["type"] for e in envs] == ["run.start", "message.delta", "message.delta", "run.end", "run.saved"]
    assert envs[-2]["data"]["report"] == "こんにちは" and all(e["run_id"] == run["id"] for e in envs)
    assert s.c.get(f"/api/runs/{run['id']}/result").json() == {"report": "こんにちは", "artifacts": []}
    listed = s.c.get("/api/runs").json()["runs"]
    assert listed[0]["id"] == run["id"] and listed[0]["agent"] == "chat"


def test_research_run_through_the_screen_api(s):
    run = s.c.post("/api/runs", json={"request": "マーキュリー"}).json()          # agent を省くと 1 つ目
    assert run["agent"] == "research"
    assert wait_for(lambda: s.gw.store.get(run["id"])["status"] == "waiting")
    assert s.gw.store.get(run["id"])["question"].startswith("どれのことですか")
    assert s.c.post(f"/api/runs/{run['id']}/pause").status_code == 409          # 答え待ちは一時停止できない
    assert s.c.post(f"/api/runs/{run['id']}/answer", json={"answer": "1"}).status_code == 200
    assert wait_for(lambda: s.gw.store.get(run["id"])["status"] == "done")
    envs = _envs(s.c.get(f"/api/runs/{run['id']}/events").text)
    kinds = [e["type"] for e in envs]
    assert kinds[0] == "run.start" and "run.waiting" in kinds and kinds[-2:] == ["run.end", "run.saved"]
    assert envs[-2]["data"]["report"].endswith("tag0:1")
    result = s.c.get(f"/api/runs/{run['id']}/result").json()
    assert {a["kind"] for a in result["artifacts"]} >= {"research/v1:facts", "research/v1:sources"}
    assert (s.tmp / f"{run['id']}.events.jsonl").exists()


def test_unreachable_agent_is_reported_not_crashing(tmp_path):
    import httpx

    def down(request):
        raise httpx.ConnectError("down")
    dead = AgentClient(AgentEntry("dead", "http://dead", ""), httpx.Client(transport=httpx.MockTransport(down)))
    st = Stack(tmp_path, extra=[dead])
    agents = {a["id"]: a for a in st.c.get("/api/agents").json()["agents"]}
    assert not agents["dead"]["ok"] and "ConnectError" in agents["dead"]["error"]
    r = st.c.post("/api/runs", json={"request": "x", "agent": "dead"})
    assert r.status_code == 503
    assert st.c.get("/api/status", params={"agent": "dead"}).json()["ok"] is False


# ---- 偽物のエージェント: agentkit を使わず、契約だけを話す ----

def _fake_agent() -> FastAPI:
    app = FastAPI()
    events = [{"v": 1, "seq": 1, "t": 0, "run_id": "r1", "type": "run.start", "data": {"request": "x"}},
              {"v": 1, "seq": 2, "t": 0.5, "run_id": "r1", "type": "step.start", "data": {"node": "think"}},
              {"v": 1, "seq": 3, "t": 0.7, "run_id": "r1", "type": "papers/v1:paper.found", "data": {"title": "論文"}},
              {"v": 1, "seq": 4, "t": 1.0, "run_id": "r1", "type": "run.end", "data": {"report": "要約"}}]

    @app.get("/agent/v1/manifest")
    def manifest():
        return {"id": "fake", "name": "偽物", "version": "0.1", "protocol": "1", "levels": [0, 1],
                "extensions": ["papers/v1"], "capabilities": []}

    @app.post("/agent/v1/runs")
    def start(body: dict):
        return {"run_id": "r1", "status": "running"}

    @app.get("/agent/v1/runs/r1/events")
    def ev(after: int = 0):
        body = "".join(protocol.sse(e) for e in events if e["seq"] > after) + protocol.sse_signal("end")
        return StreamingResponse(iter([body]), media_type="text/event-stream")

    @app.get("/agent/v1/runs/r1/result")
    def result():
        return {"report": "要約", "artifacts": [{"id": "p", "kind": "papers/v1:list", "title": "論文", "data": []}]}

    return app


def test_a_fake_agent_that_only_speaks_the_contract_can_be_swapped_in(tmp_path):
    fake = AgentClient(AgentEntry("fake", "http://testserver", ""), TestClient(_fake_agent()))
    st = Stack(tmp_path, extra=[fake])
    run = st.c.post("/api/runs", json={"request": "x", "agent": "fake"}).json()
    assert wait_for(lambda: st.gw.store.get(run["id"])["status"] == "done")
    envs = _envs(st.c.get(f"/api/runs/{run['id']}/events").text)
    assert [e["type"] for e in envs] == ["run.start", "step.start", "papers/v1:paper.found", "run.end", "run.saved"]
    assert st.c.get(f"/api/runs/{run['id']}/result").json()["artifacts"][0]["kind"] == "papers/v1:list"


# ---- 分ける前の記録と、ゲートウェイの立ち上げ直し ----

def test_records_made_before_the_split_are_listed_and_replayed(tmp_path):
    old = [{"seq": 1, "t": 0, "type": "run.start", "request": "水星について"},
           {"seq": 2, "t": 1, "type": "page.done", "url": "https://ja.wikipedia.org/wiki/x", "facts": []},
           {"seq": 3, "t": 2, "type": "run.end", "answer": "水星は惑星 [1]。"}]
    (tmp_path / "20261101-000000-abcd.events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in old), encoding="utf-8")
    st = Stack(tmp_path)
    listed = st.c.get("/api/runs").json()["runs"]
    assert listed[0] == {"id": "20261101-000000-abcd", "agent": "research", "request": "水星について", "status": "done",
                         "question": "", "created": listed[0]["created"], "events": 3, "seconds": 2, "models": {},
                         "settings": {}, "effective_settings": None}
    envs = _envs(st.c.get("/api/runs/20261101-000000-abcd/events").text)
    assert [e["type"] for e in envs] == ["run.start", "research/v1:page.done", "run.end"]   # 取り込んだ古い記録（保存の印はない）
    assert envs[-1]["data"] == {"report": "水星は惑星 [1]。"}
    assert "event: missing" in st.c.get("/api/runs/no-such/events").text


def test_restarted_gateway_picks_up_a_waiting_run(tmp_path):
    st = Stack(tmp_path)
    run = st.c.post("/api/runs", json={"request": "マーキュリー"}).json()
    assert wait_for(lambda: st.gw.store.get(run["id"])["status"] == "waiting")
    again = TestClient(create_app(registry=st.registry, runs_dir=tmp_path, api_key="", catalog=None))   # 立ち上げ直した
    gw2 = again.app.state.gateway
    assert gw2.store.get(run["id"])["status"] == "waiting"
    assert again.post(f"/api/runs/{run['id']}/answer", json={"answer": "2"}).status_code == 200
    assert wait_for(lambda: gw2.store.get(run["id"])["status"] == "done")
    envs = _envs(again.get(f"/api/runs/{run['id']}/events").text)
    assert envs[-2]["data"]["report"].endswith("tag0:2")
    assert [e["seq"] for e in envs] == list(range(1, len(envs) + 1))     # 重ならず、抜けもない
