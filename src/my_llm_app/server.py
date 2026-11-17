"""調査エージェントを OpenAI 互換の API として公開する（Open WebUI の接続先にする）。

    uv run uvicorn my_llm_app.server:app --port 8100

- /v1/models に「research-agent」を 1 つ出す。Open WebUI のモデル一覧に出る。
- 途中経過は <think> の中に流す（Open WebUI が開閉できる表示にする）。報告はその後に流す。
- エージェントが質問したら、それを回答として返して一時停止する。同じ会話の次のメッセージを
  利用者の答えとして、続きから再開する。会話は「それまでの利用者のメッセージ」で見分ける
  （Open WebUI の設定を変えずに済む）。
- タイトル生成などの Open WebUI の裏処理（「### Task:」で始まる）は、エージェントを動かさず
  LLM にそのまま送る。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Iterator

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.types import Command

from my_llm_app.agent.graph import build_graph
from my_llm_app.agent.progress import describe
from my_llm_app.config import load_settings
from my_llm_app.llm import LLM

MODEL_ID = "research-agent"
HEARTBEAT_SECONDS = 15   # LLM の応答待ちの間も接続が切れないよう、空のチャンクを送る間隔
PENDING_TTL = 6 * 3600   # 質問への答えを待つ時間。過ぎた会話は新しい依頼として扱う

log = logging.getLogger(__name__)
app = FastAPI(title="my_llm_app research agent")

_settings = load_settings()
_llm = LLM(_settings)
_graph = build_graph(_settings, _llm)
_api_key = os.getenv("AGENT_API_KEY", "")
# 質問して答えを待っている会話: 会話の鍵 → (thread_id, 質問した時刻)
_pending: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def text_of(content: object) -> str:
    """メッセージの content（文字列、または画像を含む部品の一覧）から文字だけを取り出す。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def conversation_key(user_texts: list[str]) -> str:
    return hashlib.sha256("\x1e".join(t.strip() for t in user_texts).encode("utf-8")).hexdigest()


def is_background_task(messages: list[dict]) -> bool:
    """Open WebUI のタイトル・タグ・続きの質問の生成など（利用者の依頼ではない）。"""
    last = text_of(messages[-1].get("content")) if messages else ""
    return last.lstrip().startswith("### Task:")


def start_or_resume(user_texts: list[str]) -> tuple[str, object]:
    """新しい依頼か、質問への答えかを見分けて、(thread_id, グラフへの入力) を返す。"""
    now = time.time()
    with _lock:
        for k in [k for k, (_, t) in _pending.items() if now - t > PENDING_TTL]:
            del _pending[k]
        waiting = _pending.pop(conversation_key(user_texts[:-1]), None) if len(user_texts) > 1 else None
    if waiting:
        return waiting[0], Command(resume=user_texts[-1])
    return str(uuid.uuid4()), {"request": user_texts[-1]}


def run_agent(user_texts: list[str]) -> Iterator[str]:
    """エージェントを動かし、画面に流す文字列を順に返す。None はハートビート。"""
    thread_id, payload = start_or_resume(user_texts)
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    events: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            for chunk in _graph.stream(payload, config, stream_mode="updates"):
                events.put(("chunk", chunk))
            events.put(("done", None))
        except Exception as e:  # noqa: BLE001  画面に原因を出すため、すべて受ける
            log.exception("agent failed")
            events.put(("error", e))

    threading.Thread(target=work, daemon=True).start()
    yield "<think>\n"
    question = None
    while True:
        try:
            kind, item = events.get(timeout=HEARTBEAT_SECONDS)
        except queue.Empty:
            yield None  # type: ignore[misc]
            continue
        if kind == "error":
            yield f"\n</think>\n\nエラーで調査を続けられませんでした: {type(item).__name__}: {item}"
            return
        if kind == "done":
            break
        for node, update in item.items():
            if node == "__interrupt__":
                question = update[0].value["question"]
                continue
            text = describe(node, update or {})
            if text:
                yield text + "\n"
    yield "</think>\n\n"
    if question:
        with _lock:
            _pending[conversation_key(user_texts)] = (thread_id, time.time())
        yield question
        return
    yield _graph.get_state(config).values.get("answer", "(回答なし)")


def _chunk(cid: str, created: int, delta: dict, finish: str | None = None) -> str:
    body = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


def _stream(parts: Iterator[str]) -> Iterator[str]:
    cid, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
    yield _chunk(cid, created, {"role": "assistant", "content": ""})
    for part in parts:
        yield _chunk(cid, created, {} if part is None else {"content": part})
    yield _chunk(cid, created, {}, "stop")
    yield "data: [DONE]\n\n"


def _check_key(authorization: str | None) -> None:
    if _api_key and authorization != f"Bearer {_api_key}":
        raise HTTPException(status_code=401, detail="invalid api key")


@app.get("/v1/models")
def models(authorization: str | None = Header(default=None)) -> dict:
    _check_key(authorization)
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "my_llm_app"}]}


@app.post("/v1/chat/completions")
def chat_completions(body: dict, authorization: str | None = Header(default=None)):
    _check_key(authorization)
    messages = body.get("messages") or []
    if is_background_task(messages):
        parts: Iterator[str] = iter([_llm.chat_messages(
            [{"role": m["role"], "content": text_of(m.get("content"))} for m in messages])])
    else:
        user_texts = [text_of(m.get("content")) for m in messages if m.get("role") == "user"]
        if not user_texts or not user_texts[-1].strip():
            raise HTTPException(status_code=400, detail="no user message")
        parts = run_agent(user_texts)
    if body.get("stream"):
        return StreamingResponse(_stream(parts), media_type="text/event-stream")
    content = "".join(p for p in parts if p)
    return JSONResponse({
        "id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    })


@app.get("/health")
def health() -> dict:
    return {"ok": True}
