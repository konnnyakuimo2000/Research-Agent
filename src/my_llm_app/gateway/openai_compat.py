"""OpenAI 互換の入口（Open WebUI の接続先）。ゲートウェイの前に置く変換の部品（3 章の入口アダプター）。

- /v1/models に、config/agents.toml で openai_model を付けたエージェントを出す（調査エージェントは research-agent）
- 0 段のエージェント: 返ってくる文（message.delta）をそのまま流す
- 1 段のエージェント: 状態の一行（status）を <think> の中に流し（Open WebUI が開閉できる表示にする）、報告をその後に流す
- エージェントが質問したら、それを回答として返す。同じ会話の次のメッセージを答えとして、続きから再開する。
  会話は次のどちらかで見分ける（レビュー 4）
  1. Open WebUI が付ける利用者 ID とチャット ID（X-OpenWebUI-User-Id / X-OpenWebUI-Chat-Id。
     Open WebUI の ENABLE_FORWARD_USER_INFO_HEADERS=true で付く）。別の利用者の会話からは再開できない
  2. 付いていなければ、ゲートウェイが発行した会話トークン。質問の末尾に、表示されない Markdown の行
     （[//]: # (conv:…)）として入れ、次のターンで送られてくる会話の履歴から読み取る
- 答えを待つ期限が切れた依頼は、エージェントに止めさせる（エージェントが会話の記録を消す。レビュー 5）
- タイトル生成などの Open WebUI の裏処理（「### Task:」で始まる）は、background = true のエージェント
  （チャット）に回す。画面の一覧には出さない
- ここから始めた依頼も、テレメトリー画面の一覧に出る（同じゲートウェイの記録なので）
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Iterator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from my_llm_app.gateway.core import Gateway, Unavailable
from my_llm_app.gateway.registry import AgentError

HEARTBEAT_SECONDS = 15   # 応答待ちの間も接続が切れないよう、空のチャンクを送る間隔
PENDING_TTL = 6 * 3600   # 質問への答えを待つ時間。過ぎた会話は新しい依頼として扱う
USER_HEADER, CHAT_HEADER = "x-openwebui-user-id", "x-openwebui-chat-id"
# 質問の末尾に入れる会話トークン（Markdown のリンク定義の形なので、画面には表示されない）
TOKEN_LINE = "\n\n[//]: # (conv:{token})"
_TOKEN = re.compile(r"\[//\]: # \(conv:([0-9a-f]{32})\)")


def text_of(content: object) -> str:
    """メッセージの content（文字列、または画像を含む部品の一覧）から文字だけを取り出す。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def session_key(headers, messages: list[dict]) -> str | None:
    """答えを待っている会話の鍵。Open WebUI の利用者 ID とチャット ID があればその組、なければ会話トークン。"""
    user, chat = headers.get(USER_HEADER), headers.get(CHAT_HEADER)
    if user and chat:
        return f"owui:{user}:{chat}"
    for m in reversed(messages):
        if m.get("role") == "assistant":
            found = _TOKEN.findall(text_of(m.get("content")))
            return f"tok:{found[-1]}" if found else None
    return None


def is_background_task(messages: list[dict]) -> bool:
    """Open WebUI のタイトル・タグ・続きの質問の生成など（利用者の依頼ではない）。"""
    last = text_of(messages[-1].get("content")) if messages else ""
    return last.lstrip().startswith("### Task:")


class Sessions:
    """質問して答えを待っている会話: 会話の鍵 → (ゲートウェイの依頼 ID, 質問した時刻)。"""

    def __init__(self) -> None:
        self.pending: dict[str, tuple[str, float]] = {}
        self.lock = threading.Lock()

    def take(self, key: str | None) -> tuple[str | None, list[str]]:
        """答えを待っている依頼（なければ None）と、期限が切れた依頼の一覧。"""
        now = time.time()
        with self.lock:
            expired = [k for k, (_, t) in self.pending.items() if now - t > PENDING_TTL]
            stale = [self.pending.pop(k)[0] for k in expired]
            waiting = self.pending.pop(key, None) if key else None
        return (waiting[0] if waiting else None), stale

    def put(self, key: str, run_id: str) -> None:
        with self.lock:
            self.pending[key] = (run_id, time.time())


def router(gw: Gateway, check_key) -> APIRouter:
    r = APIRouter()
    sessions = Sessions()
    r.sessions = sessions  # type: ignore[attr-defined]  - テストから見るため

    def wait_report(run_id: str) -> str:
        parts, report = [], None
        for env in gw.follow(run_id, 0, HEARTBEAT_SECONDS):
            if env is None:
                continue
            t, d = env["type"], env.get("data") or {}
            if t == "message.delta":
                parts.append(d.get("text", ""))
            elif t == "run.end":
                report = d.get("report", "")
            elif t in ("run.error", "run.cancelled"):
                raise HTTPException(status_code=502, detail=d.get("error", "止まりました"))
        return report if report is not None else "".join(parts)

    def run_parts(agent_id: str, levels: list[int], text: str, key: str | None) -> Iterator[str | None]:
        waiting, stale = sessions.take(key)
        for run_id in stale:                      # 答えを待つ期限が切れた依頼は止める（記録も消える）
            try:
                gw.control(run_id, "cancel")
            except Exception:  # noqa: BLE001 - 止められなくても新しい依頼は続ける
                pass
        after = 0
        try:
            if waiting:
                after = gw.store.get(waiting)["events"] or 0
                gw.answer(waiting, text)
                run_id = waiting
            else:
                run_id = gw.start(agent_id, text)["id"]
        except (Unavailable, AgentError) as e:
            yield f"エージェントにつながりません: {e}"
            return
        think = 1 in levels
        if think:
            yield "<think>\n"
        question = report = None
        for env in gw.follow(run_id, after, HEARTBEAT_SECONDS):
            if env is None:
                yield None
                continue
            t, d = env["type"], env.get("data") or {}
            if t == "status" and think:
                yield d.get("text", "") + "\n"
            elif t == "message.delta":
                yield d.get("text", "")
            elif t == "run.waiting":
                question = d.get("question", "")
                break
            elif t == "run.end":
                report = d.get("report", "")
                break
            elif t in ("run.error", "run.cancelled"):
                msg = f"エラーで調査を続けられませんでした: {d.get('error', '')}" if t == "run.error" else "止めました"
                yield ("\n</think>\n\n" if think else "") + msg
                return
        if think:
            yield "</think>\n\n"
        if question is not None:
            suffix = ""
            if not key or not key.startswith("owui:"):
                token = uuid.uuid4().hex          # Open WebUI の ID がない: 会話トークンを発行して、次のターンで受け取る
                key, suffix = f"tok:{token}", TOKEN_LINE.format(token=token)
            sessions.put(key, run_id)
            yield question + suffix
            return
        if think and report is not None:
            yield report

    @r.get("/v1/models")
    def models(authorization: str | None = Header(default=None)) -> dict:
        check_key(authorization)
        data = [{"id": c.entry.openai_model, "object": "model", "created": 0, "owned_by": "my_llm_app"}
                for c in gw.registry.clients.values() if c.entry.openai_model]
        return {"object": "list", "data": data}

    @r.post("/v1/chat/completions")
    def chat_completions(body: dict, request: Request, authorization: str | None = Header(default=None)):
        check_key(authorization)
        messages = body.get("messages") or []
        model = body.get("model", "")
        if is_background_task(messages):
            bg = gw.registry.background()
            if bg is None:
                raise HTTPException(status_code=503, detail="裏処理を受け持つエージェントがありません")
            history = [{"role": m["role"], "content": text_of(m.get("content"))} for m in messages]
            try:
                run_id = gw.start(bg.id, history[-1]["content"], {"messages": history}, hidden=True)["id"]
            except (Unavailable, AgentError) as e:
                raise HTTPException(status_code=503, detail=str(e)) from None
            parts: Iterator[str | None] = iter([wait_report(run_id)])
            model = model or bg.entry.openai_model
        else:
            client = gw.registry.by_openai_model(model) or gw.registry.get("")
            user_texts = [text_of(m.get("content")) for m in messages if m.get("role") == "user"]
            if not user_texts or not user_texts[-1].strip():
                raise HTTPException(status_code=400, detail="no user message")
            levels = (client.manifest() or {}).get("levels", [0])
            parts = run_parts(client.id, levels, user_texts[-1], session_key(request.headers, messages))
            model = client.entry.openai_model or client.id
        if body.get("stream"):
            return StreamingResponse(_stream(parts, model), media_type="text/event-stream")
        content = "".join(p for p in parts if p)
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        })

    return r


def _chunk(cid: str, created: int, model: str, delta: dict, finish: str | None = None) -> str:
    body = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


def _stream(parts: Iterator[str | None], model: str) -> Iterator[str]:
    cid, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
    yield _chunk(cid, created, model, {"role": "assistant", "content": ""})
    for part in parts:
        yield _chunk(cid, created, model, {} if part is None else {"content": part})
    yield _chunk(cid, created, model, {}, "stop")
    yield "data: [DONE]\n\n"
