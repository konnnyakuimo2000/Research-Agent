"""Open WebUI 向け API サーバー: 質問 → 答えで再開、裏処理の中継、ストリーミングの形式。"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("VL_BASE_URL", "http://localhost:1/v1")

from fastapi.testclient import TestClient  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import interrupt  # noqa: E402
from typing_extensions import TypedDict  # noqa: E402

from my_llm_app import server  # noqa: E402


class S(TypedDict, total=False):
    request: str
    answer: str


def _fake_graph():
    def ask(state: S) -> dict:
        reply = interrupt({"question": "どれのことですか？\n1. 惑星\n2. 歌手"})
        return {"answer": f"{state['request']} → {reply} について報告"}

    g = StateGraph(S)
    g.add_node("ask_user", ask)
    g.add_edge(START, "ask_user")
    g.add_edge("ask_user", END)
    return g.compile(checkpointer=InMemorySaver())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_graph", _fake_graph())
    monkeypatch.setattr(server, "_pending", {})
    return TestClient(server.app)


def _post(client, messages, stream=False):
    return client.post("/v1/chat/completions", json={"model": server.MODEL_ID, "messages": messages,
                                                     "stream": stream})


def _content(r) -> str:
    return r.json()["choices"][0]["message"]["content"]


def test_models(client):
    assert client.get("/v1/models").json()["data"][0]["id"] == "research-agent"


def test_question_then_resume(client):
    first = [{"role": "user", "content": "マーキュリーについて調べて"}]
    r1 = _content(_post(client, first))
    assert "どれのことですか" in r1 and "<think>" in r1
    second = first + [{"role": "assistant", "content": r1}, {"role": "user", "content": "1"}]
    r2 = _content(_post(client, second))
    assert r2.endswith("マーキュリーについて調べて → 1 について報告")


def test_new_chat_is_not_resumed(client):
    _post(client, [{"role": "user", "content": "マーキュリーについて調べて"}])
    r = _content(_post(client, [{"role": "user", "content": "別の依頼"}]))
    assert "どれのことですか" in r   # 最初から始まる（前の会話の答えとして扱わない）


def test_background_task_goes_to_llm(client, monkeypatch):
    called = {}
    monkeypatch.setattr(server._llm, "chat_messages", lambda m: called.setdefault("m", m) and "タイトル")
    r = _content(_post(client, [{"role": "user", "content": "### Task:\nGenerate a concise title"}]))
    assert r == "タイトル" and called["m"][0]["content"].startswith("### Task:")


def test_stream_format(client):
    r = _post(client, [{"role": "user", "content": [{"type": "text", "text": "マーキュリー"}]}], stream=True)
    lines = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(x) for x in lines[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert "どれのことですか" in text and chunks[-1]["choices"][0]["finish_reason"] == "stop"


# ---- レビュー 4: 同じ依頼で始まる会話を、それぞれ正しく再開する ----

def _tagged_graph():
    """会話ごとに番号を付け（最初のノード）、質問への答えと一緒に返す。どの会話が再開されたかがわかる。"""
    import itertools
    counter = itertools.count()

    class T(TypedDict, total=False):
        request: str
        tag: int
        answer: str

    def start(state: T) -> dict:
        return {"tag": next(counter)}

    def ask(state: T) -> dict:
        reply = interrupt({"question": "どれのことですか？\n1. 惑星\n2. 歌手"})
        return {"answer": f"tag{state['tag']}:{reply}"}

    g = StateGraph(T)
    g.add_node("start", start)
    g.add_node("ask_user", ask)
    g.add_edge(START, "start")
    g.add_edge("start", "ask_user")
    g.add_edge("ask_user", END)
    return g.compile(checkpointer=InMemorySaver())


def _post_h(client, messages, headers=None, stream=False):
    r = client.post("/v1/chat/completions", headers=headers or {},
                    json={"model": server.MODEL_ID, "messages": messages, "stream": stream})
    if not stream:
        return _content(r)
    lines = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ") and ln != "data: [DONE]"]
    return "".join(json.loads(x)["choices"][0]["delta"].get("content", "") for x in lines)


def test_two_tabs_with_the_same_request_resume_their_own_runs(monkeypatch):
    monkeypatch.setattr(server, "_graph", _tagged_graph())
    monkeypatch.setattr(server, "_pending", {})
    c = TestClient(server.app)
    first = [{"role": "user", "content": "マーキュリーについて調べて"}]
    tab = {"a": {"x-openwebui-user-id": "u1", "x-openwebui-chat-id": "chat-a"},
           "b": {"x-openwebui-user-id": "u1", "x-openwebui-chat-id": "chat-b"}}
    qa, qb = _post_h(c, first, tab["a"]), _post_h(c, first, tab["b"], stream=True)   # 両方が質問待ちになる
    # 逆の順に答える（b → a）
    rb = _post_h(c, first + [{"role": "assistant", "content": qb}, {"role": "user", "content": "2"}], tab["b"])
    ra = _post_h(c, first + [{"role": "assistant", "content": qa}, {"role": "user", "content": "1"}], tab["a"], stream=True)
    assert rb.endswith("tag1:2") and ra.endswith("tag0:1")


def test_another_user_cannot_resume_someone_elses_run(monkeypatch):
    monkeypatch.setattr(server, "_graph", _tagged_graph())
    monkeypatch.setattr(server, "_pending", {})
    c = TestClient(server.app)
    first = [{"role": "user", "content": "マーキュリーについて調べて"}]
    q = _post_h(c, first, {"x-openwebui-user-id": "u1", "x-openwebui-chat-id": "chat-a"})
    other = _post_h(c, first + [{"role": "assistant", "content": q}, {"role": "user", "content": "1"}],
                    {"x-openwebui-user-id": "u2", "x-openwebui-chat-id": "chat-a"})
    assert "どれのことですか" in other                                   # 再開されず、新しい調査として始まる


def test_without_open_webui_ids_a_server_token_tells_conversations_apart(monkeypatch):
    monkeypatch.setattr(server, "_graph", _tagged_graph())
    monkeypatch.setattr(server, "_pending", {})
    c = TestClient(server.app)
    first = [{"role": "user", "content": "マーキュリーについて調べて"}]
    qa, qb = _post_h(c, first), _post_h(c, first, stream=True)
    assert qa != qb and "[//]: # (conv:" in qa                          # 表示されない行に、別々のトークン
    rb = _post_h(c, first + [{"role": "assistant", "content": qb}, {"role": "user", "content": "2"}], stream=True)
    ra = _post_h(c, first + [{"role": "assistant", "content": qa}, {"role": "user", "content": "1"}])
    assert rb.endswith("tag1:2") and ra.endswith("tag0:1")


# ---- レビュー 5: 終わった・失敗した・期限切れの会話の記録を消す ----

def _threads():
    return set(server._graph.checkpointer.storage)


def test_checkpoints_are_removed_when_done_expired_or_failed(monkeypatch):
    monkeypatch.setattr(server, "_graph", _tagged_graph())
    monkeypatch.setattr(server, "_pending", {})
    c = TestClient(server.app)
    first = [{"role": "user", "content": "マーキュリーについて調べて"}]
    q = _post_h(c, first)
    assert len(_threads()) == 1                                         # 答えを待っている間は残す
    _post_h(c, first + [{"role": "assistant", "content": q}, {"role": "user", "content": "1"}])
    assert _threads() == set()                                          # 終わったら消す

    _post_h(c, first)
    assert len(_threads()) == 1
    monkeypatch.setattr(server, "PENDING_TTL", -1)                      # 期限切れにする
    _post_h(c, [{"role": "user", "content": "別の依頼"}])                 # 次の依頼で期限切れを回収する
    monkeypatch.setattr(server, "PENDING_TTL", 6 * 3600)
    assert len(_threads()) == 1 and len(server._pending) == 1           # 残っているのは新しい依頼の分だけ

    class Broken(TypedDict, total=False):
        request: str

    def boom(state):
        raise RuntimeError("壊れた")
    g = StateGraph(Broken)
    g.add_node("x", boom)
    g.add_edge(START, "x")
    g.add_edge("x", END)
    monkeypatch.setattr(server, "_graph", g.compile(checkpointer=InMemorySaver()))
    r = _post_h(c, [{"role": "user", "content": "壊れる依頼"}])
    assert "エラー" in r and _threads() == set()                         # 回復できない失敗も消す
