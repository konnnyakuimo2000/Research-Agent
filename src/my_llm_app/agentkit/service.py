"""契約の API（docs/telemetry_enhancement_plan.md 4.1）を出す FastAPI アプリを作る。

    GET  /agent/v1/manifest              名乗り
    POST /agent/v1/runs                  依頼を受ける {input: {text, ...}, settings, models: {役割: モデル}} → {run_id, status}
    GET  /agent/v1/runs/{id}             状態
    GET  /agent/v1/runs/{id}/events      イベントの流れ（SSE。?after= か Last-Event-ID より後から）
    POST /agent/v1/runs/{id}/input       {kind: "answer", text}
    POST /agent/v1/runs/{id}/control     {action: "pause" | "resume" | "cancel"}
    GET  /agent/v1/runs/{id}/result      結果 {report, artifacts}
    GET  /agent/v1/health                動いているか、使うモデルなどにつながるか

イベントの流れは、終わったら `event: end`、答えを待って休むときは `event: rest` を送って閉じる
（答えを待つ間は何時間にもなりうるので、つなぎっぱなしにしない。ゲートウェイは答えを送った後につなぎ直す）。
一時停止中はつないだまま、空の行（ping）を送り続ける。

AGENT_SERVICE_KEY を設定すると、Authorization: Bearer <鍵> のない呼び出しを断る（ゲートウェイだけが呼ぶ）。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from my_llm_app.agentkit import protocol
from my_llm_app.agentkit.runs import RunManager, StateError

HEARTBEAT_SECONDS = 15


def agent_app(manifest: dict, manager: RunManager, health: Callable[[], dict],
              key: str | None = None) -> FastAPI:
    key = os.getenv("AGENT_SERVICE_KEY", "") if key is None else key
    extension = (manifest.get("extensions") or [""])[0]
    capabilities = set(manifest.get("capabilities", []))
    app = FastAPI(title=manifest.get("name", "agent"))

    def check(authorization: str | None) -> None:
        if key and authorization != f"Bearer {key}":
            raise HTTPException(status_code=401, detail="invalid service key")

    def run_or_404(run_id: str):
        run = manager.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="この依頼はありません")
        return run

    @app.get("/agent/v1/manifest")
    def get_manifest(authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        return manifest

    @app.get("/agent/v1/health")
    def get_health(authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        return {"ok": True, **health()}

    @app.post("/agent/v1/runs")
    def post_run(body: dict, authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        input_ = body.get("input") or {}
        if not str(input_.get("text", "")).strip():
            raise HTTPException(status_code=400, detail="依頼が空です")
        models = body.get("models") or {}
        roles = {r["role"] for r in manifest.get("model_roles", [])}
        unknown = set(models) - roles
        if unknown:
            raise HTTPException(status_code=400, detail=f"この役割はありません: {', '.join(sorted(unknown))}")
        return manager.start(input_, body.get("settings") or {}, models).summary()

    @app.get("/agent/v1/runs/{run_id}")
    def get_run(run_id: str, authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        return run_or_404(run_id).summary()

    @app.post("/agent/v1/runs/{run_id}/input")
    def post_input(run_id: str, body: dict, authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        run_or_404(run_id)
        if body.get("kind") != "answer" or "questions" not in capabilities:
            raise HTTPException(status_code=400, detail=f"この入力は受け付けません: {body.get('kind')}")
        try:
            return manager.answer(run_id, str(body.get("text", "")).strip()).summary()
        except StateError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None

    @app.post("/agent/v1/runs/{run_id}/control")
    def post_control(run_id: str, body: dict, authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        run_or_404(run_id)
        action = body.get("action")
        if action not in ("pause", "resume", "cancel") or action == "resume" and "pause" not in capabilities \
                or action != "resume" and action not in capabilities:
            raise HTTPException(status_code=400, detail=f"この操作はできません: {action}")
        try:
            return getattr(manager, action)(run_id).summary()
        except StateError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None

    @app.get("/agent/v1/runs/{run_id}/result")
    def get_result(run_id: str, authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        run = run_or_404(run_id)
        if run.status != "done":
            raise HTTPException(status_code=409, detail="まだ結果はありません")
        return {"report": "", "artifacts": [], **run.result}

    @app.get("/agent/v1/runs/{run_id}/events")
    def get_events(run_id: str, request: Request, after: int = 0,
                   authorization: str | None = Header(default=None)):
        check(authorization)
        last = request.headers.get("last-event-id")
        seq = max(after, int(last) if last and last.isdigit() else 0)
        run = manager.get(run_id)

        def gen() -> Iterator[str]:
            nonlocal seq
            if run is None:
                yield protocol.sse_signal("missing")
                return
            rec = run.recorder
            while True:
                new = rec.events[seq:]
                for e in new:
                    yield protocol.sse(protocol.to_v1(e, run_id, extension))
                    seq = e["seq"]
                if seq >= len(rec.events):
                    if rec.finished:
                        yield protocol.sse_signal("end", {"status": run.status})
                        return
                    if run.status == "waiting" and rec.events[-1]["type"] == "run.waiting":
                        yield protocol.sse_signal("rest", {"status": run.status})
                        return
                if not new and not rec.wait_after(seq, HEARTBEAT_SECONDS):
                    yield ": ping\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.state.manager = manager
    return app
