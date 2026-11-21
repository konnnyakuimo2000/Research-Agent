"""調査エージェントを OpenAI 互換の API として公開する（Open WebUI の接続先にする）。

    uv run uvicorn my_llm_app.server:app --port 8100

- /v1/models に「research-agent」を 1 つ出す。Open WebUI のモデル一覧に出る。
- 途中経過は <think> の中に流す（Open WebUI が開閉できる表示にする）。報告はその後に流す。
- エージェントが質問したら、それを回答として返して一時停止する。同じ会話の次のメッセージを
  利用者の答えとして、続きから再開する。会話は次のどちらかで見分ける（レビュー 4）。
  1. Open WebUI が付ける利用者 ID とチャット ID（X-OpenWebUI-User-Id / X-OpenWebUI-Chat-Id。
     Open WebUI の ENABLE_FORWARD_USER_INFO_HEADERS=true で付く）。別の利用者の会話からは再開できない
  2. 付いていなければ、サーバーが発行した会話トークン。質問の末尾に、表示されない Markdown の行
     （[//]: # (conv:…)）として入れ、次のターンで送られてくる会話の履歴から読み取る
- 終わった・失敗した・答えを待つ期限が切れた会話の記録（チェックポイント）は消す（レビュー 5）。
- タイトル生成などの Open WebUI の裏処理（「### Task:」で始まる）は、エージェントを動かさず
  LLM にそのまま送る。
- テレメトリー画面（/ui/）と、その API（/api/...）も出す（docs/telemetry_design.md）。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Iterator

from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command

from my_llm_app import assets
from my_llm_app.agent.graph import build_graph
from my_llm_app.agent.progress import describe
from my_llm_app.config import REPO_ROOT, load_settings
from my_llm_app.llm import LLM
from my_llm_app.runs import RunManager

MODEL_ID = "research-agent"
HEARTBEAT_SECONDS = 15   # LLM の応答待ちの間も接続が切れないよう、空のチャンクを送る間隔
PENDING_TTL = 6 * 3600   # 質問への答えを待つ時間。過ぎた会話は新しい依頼として扱う

log = logging.getLogger(__name__)
app = FastAPI(title="my_llm_app research agent")

_settings = load_settings()
_llm = LLM(_settings)
_graph = build_graph(_settings, _llm)
_api_key = os.getenv("AGENT_API_KEY", "")
_runs = RunManager(_graph, REPO_ROOT / "runs")
_assets_dir = REPO_ROOT / "runs" / ".assets"
# 質問して答えを待っている会話: 会話の鍵 → (thread_id, 質問した時刻)
_pending: dict[str, tuple[str, float]] = {}
USER_HEADER, CHAT_HEADER = "x-openwebui-user-id", "x-openwebui-chat-id"
# 質問の末尾に入れる会話トークン（Markdown のリンク定義の形なので、画面には表示されない）
_TOKEN_LINE = "\n\n[//]: # (conv:{token})"
_TOKEN = re.compile(r"\[//\]: # \(conv:([0-9a-f]{32})\)")
_lock = threading.Lock()


def text_of(content: object) -> str:
    """メッセージの content（文字列、または画像を含む部品の一覧）から文字だけを取り出す。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def session_key(headers: dict, messages: list[dict]) -> str | None:
    """答えを待っている会話の鍵。Open WebUI の利用者 ID とチャット ID があればその組、なければ会話トークン。"""
    user, chat = headers.get(USER_HEADER), headers.get(CHAT_HEADER)
    if user and chat:
        return f"owui:{user}:{chat}"
    for m in reversed(messages):
        if m.get("role") == "assistant":
            found = _TOKEN.findall(text_of(m.get("content")))
            return f"tok:{found[-1]}" if found else None
    return None


def forget_thread(thread_id: str) -> None:
    """会話の記録（チェックポイント）を消す。終わった・失敗した・期限が切れた会話に使う。"""
    try:
        _graph.checkpointer.delete_thread(thread_id)
    except Exception:  # noqa: BLE001 - 消せなくても応答は続ける
        log.exception("could not delete thread %s", thread_id)


def is_background_task(messages: list[dict]) -> bool:
    """Open WebUI のタイトル・タグ・続きの質問の生成など（利用者の依頼ではない）。"""
    last = text_of(messages[-1].get("content")) if messages else ""
    return last.lstrip().startswith("### Task:")


def start_or_resume(key: str | None, text: str) -> tuple[str, object]:
    """新しい依頼か、質問への答えかを見分けて、(thread_id, グラフへの入力) を返す。"""
    now = time.time()
    with _lock:
        expired = [k for k, (_, t) in _pending.items() if now - t > PENDING_TTL]
        threads = [_pending.pop(k)[0] for k in expired]
        waiting = _pending.pop(key, None) if key else None
    for thread_id in threads:
        forget_thread(thread_id)          # 答えを待つ期限が切れた会話の記録を消す
    if waiting:
        return waiting[0], Command(resume=text)
    return str(uuid.uuid4()), {"request": text}


def run_agent(text: str, key: str | None) -> Iterator[str]:
    """エージェントを動かし、画面に流す文字列を順に返す。None はハートビート。

    最終の回答は、調査を動かしているスレッドが取り出してから会話の記録を消す（画面との接続が途中で切れても、
    調査が終わったところで消える）。
    """
    thread_id, payload = start_or_resume(key, text)
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    events: queue.Queue = queue.Queue()

    def work() -> None:
        question = None
        try:
            for chunk in _graph.stream(payload, config, stream_mode="updates"):
                if "__interrupt__" in chunk:
                    question = chunk["__interrupt__"][0].value["question"]
                events.put(("chunk", chunk))
            answer = None if question else _graph.get_state(config).values.get("answer", "(回答なし)")
        except Exception as e:  # noqa: BLE001  画面に原因を出すため、すべて受ける
            log.exception("agent failed")
            forget_thread(thread_id)      # 回復できない失敗: 記録を消す
            events.put(("error", e))
            return
        if not question:
            forget_thread(thread_id)      # 終わった: 回答を取り出したので記録を消す
        events.put(("done", answer))

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
            answer = item
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
        suffix = ""
        if not key or not key.startswith("owui:"):
            token = uuid.uuid4().hex          # Open WebUI の ID がない: 会話トークンを発行して、次のターンで受け取る
            key, suffix = f"tok:{token}", _TOKEN_LINE.format(token=token)
        with _lock:
            _pending[key] = (thread_id, time.time())
        yield question + suffix
        return
    yield answer


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
def chat_completions(body: dict, request: Request, authorization: str | None = Header(default=None)):
    _check_key(authorization)
    messages = body.get("messages") or []
    if is_background_task(messages):
        parts: Iterator[str] = iter([_llm.chat_messages(
            [{"role": m["role"], "content": text_of(m.get("content"))} for m in messages])])
    else:
        user_texts = [text_of(m.get("content")) for m in messages if m.get("role") == "user"]
        if not user_texts or not user_texts[-1].strip():
            raise HTTPException(status_code=400, detail="no user message")
        parts = run_agent(user_texts[-1], session_key(request.headers, messages))
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


# ---- テレメトリー画面の API（docs/telemetry_design.md 7 章） ----

def _check_ui_key(authorization: str | None, key: str | None) -> None:
    # EventSource はヘッダーを付けられないので、?key= でも受ける
    if _api_key and authorization != f"Bearer {_api_key}" and key != _api_key:
        raise HTTPException(status_code=401, detail="API キーが違います")


@app.post("/api/runs")
def api_start(body: dict, authorization: str | None = Header(default=None)) -> dict:
    _check_ui_key(authorization, None)
    request = str(body.get("request", "")).strip()
    if not request:
        raise HTTPException(status_code=400, detail="依頼が空です")
    return _runs.start(request).summary()


@app.post("/api/runs/{run_id}/answer")
def api_answer(run_id: str, body: dict, authorization: str | None = Header(default=None)) -> dict:
    _check_ui_key(authorization, None)
    try:
        return _runs.answer(run_id, str(body.get("answer", "")).strip()).summary()
    except KeyError:
        raise HTTPException(status_code=409, detail="この調査は答えを待っていません") from None


@app.post("/api/runs/{run_id}/pause")
def api_pause(run_id: str, authorization: str | None = Header(default=None)) -> dict:
    _check_ui_key(authorization, None)
    try:
        return _runs.pause(run_id).summary()
    except KeyError:
        raise HTTPException(status_code=409, detail="この調査は動いていません") from None


@app.post("/api/runs/{run_id}/resume")
def api_resume(run_id: str, authorization: str | None = Header(default=None)) -> dict:
    _check_ui_key(authorization, None)
    try:
        return _runs.resume(run_id).summary()
    except KeyError:
        raise HTTPException(status_code=409, detail="この調査は止まっていません") from None


@app.get("/api/runs")
def api_runs(authorization: str | None = Header(default=None), key: str | None = None) -> dict:
    _check_ui_key(authorization, key)
    return {"runs": _runs.listing()}


@app.get("/api/runs/{run_id}/events")
def api_events(run_id: str, request: Request, after: int = 0, key: str | None = None,
               authorization: str | None = Header(default=None)):
    """イベントの流れ（Server-Sent Events）。after か Last-Event-ID より後から送る。"""
    _check_ui_key(authorization, key)
    last = request.headers.get("last-event-id")
    seq = max(after, int(last) if last and last.isdigit() else 0)

    def sse(e: dict) -> str:
        return f"id: {e['seq']}\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n"

    def gen() -> Iterator[str]:
        nonlocal seq
        run = _runs.get(run_id)
        if run is None:
            events = _runs.events_on_disk(run_id)
            if not events:
                yield "event: missing\ndata: {}\n\n"
                return
            for e in events[seq:]:
                yield sse(e)
            yield "event: end\ndata: {}\n\n"
            return
        while True:
            new = run.recorder.wait_after(seq, HEARTBEAT_SECONDS)
            for e in new:
                yield sse(e)
                seq = e["seq"]
            if not new:
                if run.recorder.finished:
                    yield "event: end\ndata: {}\n\n"
                    return
                yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/favicon")
def api_favicon(host: str) -> Response:
    data = assets.favicon(host, _assets_dir)
    if not data:
        return Response(status_code=204)
    return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/image")
def api_image(run: str, src: str, key: str | None = None, authorization: str | None = Header(default=None)) -> Response:
    _check_ui_key(authorization, key)
    data = assets.image(src, _runs.events(run), _assets_dir)
    if not data:
        return Response(status_code=404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


_status_cache: dict = {}


@app.get("/api/status")
def api_status() -> dict:
    """研究室の LLM サーバーと検索（SearXNG）につながるか。画面の入力欄の下に出す（20 秒覚えておく）。"""
    if _status_cache and time.time() - _status_cache["at"] < 20:
        return _status_cache["value"]
    from my_llm_app.tools import http as http_tools

    def check(url: str) -> dict:
        started = time.time()
        try:
            r = http_tools.get(url, timeout=4, cache=False, public=False)   # 設定済みの LLM・SearXNG
            return {"ok": r.status_code == 200, "seconds": round(time.time() - started, 1)}
        except Exception as e:  # noqa: BLE001 - つながらない理由を画面に出す
            return {"ok": False, "error": type(e).__name__}

    value = {"model": _settings.llm_model,
             "llm": check(_settings.llm_base_url.rstrip("/") + "/models"),
             "search": check(_settings.searxng_url.rstrip("/") + "/healthz")}
    _status_cache.update(at=time.time(), value=value)
    return value


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/ui/")


class UIFiles(StaticFiles):
    """画面のファイル。ブラウザに毎回確かめさせる（更新した画面が、古い版のまま使われないように）。"""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/ui", UIFiles(directory=Path(__file__).parent / "ui", html=True), name="ui")
