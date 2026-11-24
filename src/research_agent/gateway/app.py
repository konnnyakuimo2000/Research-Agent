"""ゲートウェイの API と画面。

    uv run uvicorn --factory research_agent.gateway.app:create_app --port 8100

画面の API（/api/*）:
    GET  /api/agents                       つないでいるエージェント（名乗りの要約と、つながるか）
    POST /api/runs                         {request, agent} → 依頼を始める
    GET  /api/runs                         これまでの依頼（新しい順）
    GET  /api/runs/{id}/events             イベントの流れ（SSE。契約の封筒の形。?after= か Last-Event-ID より後から）
    POST /api/runs/{id}/answer|pause|resume|cancel
    GET  /api/runs/{id}/result             結果（報告と成果物）
    GET  /api/status?agent=…               そのエージェントが使うモデルなどにつながるか
    GET  /api/models                       モデルの窓口のモデル（役割の既定・能力・外部か・値段・使えるか）。
                                           POST /api/runs の models で選び直せる
    GET  /api/runs/{id}/usage              その調査の費用（モデルの窓口が数えたもの）
    GET  /api/models/status                モデルごとの接続の状態・使用量、今日の費用と上限
    GET  /api/favicon, /api/image          サイトのアイコンと画像（ゲートウェイが取得して配る）
OpenAI 互換の入口: /v1/models, /v1/chat/completions（openai_compat）

AGENT_API_KEY を設定すると、画面の API と OpenAI 互換の入口に鍵を求める。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from research_agent import assets
from research_agent.agentkit import protocol
from research_agent.gateway import openai_compat
from research_agent.gateway.core import Gateway, Unavailable, flat_events
from research_agent.gateway.models import ModelCatalog, ModelChoiceError
from research_agent.gateway.registry import AgentError, Registry
from research_agent.gateway.store import RunStore

HEARTBEAT_SECONDS = 15
UI_DIR = Path(__file__).resolve().parents[1] / "ui"


class UIFiles(StaticFiles):
    """画面のファイル。ブラウザに毎回確かめさせる（更新した画面が、古い版のまま使われないように）。"""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(registry: Registry | None = None, runs_dir: Path | None = None, api_key: str | None = None,
               resume: bool = True, catalog: ModelCatalog | None | bool = True) -> FastAPI:
    from research_agent.config import REPO_ROOT

    runs_dir = runs_dir or REPO_ROOT / "runs"
    registry = registry or Registry.from_file(REPO_ROOT / "config" / "agents.toml")
    api_key = os.getenv("AGENT_API_KEY", "") if api_key is None else api_key
    if catalog is True:
        catalog = ModelCatalog.from_env()          # MODEL_GATEWAY_URL がなければ、モデルの選び直しはできない
    gw = Gateway(registry, RunStore(runs_dir), catalog or None)
    assets_dir = runs_dir / ".assets"
    app = FastAPI(title="research_agent gateway")
    app.state.gateway = gw

    def check_key(authorization: str | None, key: str | None = None) -> None:
        # EventSource はヘッダーを付けられないので、?key= でも受ける
        if api_key and authorization != f"Bearer {api_key}" and key != api_key:
            raise HTTPException(status_code=401, detail="API キーが違います")

    def call(fn, *args):
        """エージェントの返事を、画面向けの状態コードにする。"""
        try:
            return fn(*args)
        except KeyError:
            raise HTTPException(status_code=404, detail="この依頼はありません") from None
        except Unavailable as e:
            raise HTTPException(status_code=503, detail=f"エージェントにつながりません: {e}") from None
        except AgentError as e:
            raise HTTPException(status_code=e.status if e.status in (400, 404, 409) else 502, detail=e.detail) from None
        except ModelChoiceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

    compat = openai_compat.router(gw, check_key)
    app.state.sessions = compat.sessions
    app.include_router(compat)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/agents")
    def api_agents(authorization: str | None = Header(default=None)) -> dict:
        check_key(authorization)
        return {"agents": gw.agents(), "default": registry.default}

    @app.post("/api/runs")
    def api_start(body: dict, authorization: str | None = Header(default=None)) -> dict:
        check_key(authorization)
        request = str(body.get("request", "")).strip()
        if not request:
            raise HTTPException(status_code=400, detail="依頼が空です")
        models = body.get("models") or {}
        if not isinstance(models, dict):
            raise HTTPException(status_code=400, detail="models は {役割: モデル} の形にしてください")
        return call(lambda: gw.start(str(body.get("agent", "")), request, models=models))

    @app.post("/api/runs/{run_id}/answer")
    def api_answer(run_id: str, body: dict, authorization: str | None = Header(default=None)) -> dict:
        check_key(authorization)
        return call(gw.answer, run_id, str(body.get("answer", "")).strip())

    for action in ("pause", "resume", "cancel"):
        def control(run_id: str, authorization: str | None = Header(default=None), _action: str = action) -> dict:
            check_key(authorization)
            return call(gw.control, run_id, _action)
        app.post(f"/api/runs/{{run_id}}/{action}", name=f"api_{action}")(control)

    @app.get("/api/models")
    def api_models(authorization: str | None = Header(default=None)) -> dict:
        check_key(authorization)
        value = gw.models()
        if value is None:
            return {"available": False, "error": gw.catalog.error if gw.catalog else "モデルの窓口を使っていない",
                    "default": "", "roles": {}, "models": []}
        return {"available": True, "default": value.get("default", ""), "roles": value.get("roles", {}),
                "models": [{k: m.get(k) for k in ("id", "title", "provider", "upstream_model", "capabilities",
                                                  "context_tokens", "default", "external", "price", "available",
                                                  "unavailable")} for m in value.get("data", [])]}

    @app.get("/api/models/status")
    def api_models_status(authorization: str | None = Header(default=None)) -> dict:
        check_key(authorization)
        value = gw.catalog.status() if gw.catalog else None
        if value is None:
            return {"available": False, "models": {}, "spend": {}}
        return {"available": True, **value}

    @app.get("/api/runs/{run_id}/usage")
    def api_run_usage(run_id: str, authorization: str | None = Header(default=None)) -> dict:
        check_key(authorization)
        if gw.store.get(run_id) is None:
            raise HTTPException(status_code=404, detail="その依頼はありません")
        value = gw.usage(run_id)
        if value is None:
            raise HTTPException(status_code=503, detail="モデルの窓口につながりません")
        return value

    @app.get("/api/runs")
    def api_runs(authorization: str | None = Header(default=None), key: str | None = None) -> dict:
        check_key(authorization, key)
        return {"runs": gw.listing()}

    @app.get("/api/runs/{run_id}/result")
    def api_result(run_id: str, authorization: str | None = Header(default=None), key: str | None = None) -> dict:
        check_key(authorization, key)
        result = gw.result(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="結果はありません")
        return result

    @app.get("/api/runs/{run_id}/events")
    def api_events(run_id: str, request: Request, after: int = 0, key: str | None = None,
                   authorization: str | None = Header(default=None)):
        check_key(authorization, key)
        last = request.headers.get("last-event-id")
        seq = max(after, int(last) if last and last.isdigit() else 0)

        def gen() -> Iterator[str]:
            if gw.store.get(run_id) is None:
                yield protocol.sse_signal("missing")
                return
            for env in gw.follow(run_id, seq, HEARTBEAT_SECONDS):
                yield ": ping\n\n" if env is None else protocol.sse(env)
            yield protocol.sse_signal("end")

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    status_cache: dict[str, tuple[float, dict]] = {}

    @app.get("/api/status")
    def api_status(agent: str = "") -> dict:
        """エージェントと、エージェントが使うモデル・検索につながるか（20 秒覚えておく）。"""
        try:
            client = registry.get(agent)
        except KeyError:
            raise HTTPException(status_code=404, detail="このエージェントはありません") from None
        cached = status_cache.get(client.id)
        if cached and time.time() - cached[0] < 20:
            return cached[1]
        try:
            h = client.health()
            value = {"agent": client.id, "ok": True, "model": h.get("model", ""), "checks": h.get("checks", {})}
        except Exception as e:  # noqa: BLE001 - つながらない理由を画面に出す
            value = {"agent": client.id, "ok": False, "error": type(e).__name__, "model": "", "checks": {}}
        status_cache[client.id] = (time.time(), value)
        return value

    @app.get("/api/favicon")
    def api_favicon(host: str) -> Response:
        data = assets.favicon(host, assets_dir)
        if not data:
            return Response(status_code=204)
        return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/image")
    def api_image(run: str, src: str, key: str | None = None,
                  authorization: str | None = Header(default=None)) -> Response:
        # research/v1 の画像（その依頼のイベントに出てきた画像だけを配る）
        check_key(authorization, key)
        data = assets.image(src, flat_events(gw.events(run)), assets_dir)
        if not data:
            return Response(status_code=404)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")

    app.mount("/ui", UIFiles(directory=UI_DIR, html=True), name="ui")
    if resume:
        gw.resume_live()
        gw.watch()          # 止まった中継をつなぎ直す見張り
    return app
