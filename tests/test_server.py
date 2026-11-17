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
