"""モデルの窓口の API（OpenAI 互換。docs/telemetry_enhancement_plan.md 6.1）。

    uv run uvicorn --factory my_llm_app.modelgw.app:create_app --port 8103

    GET  /v1/models              使えるモデル（OpenAI の形 + title・capabilities など）
    POST /v1/chat/completions    OpenAI 互換。model は id・モデル名・"default"・空（既定）・"role:<役割>"（[roles] の対応、なければ既定）
    GET  /v1/usage               モデルごとの使用量
    GET  /health                 モデルごとにつながるか（20 秒覚えておく）

- やり直し・待ち時間の数え方・同時実行の上限は、モデルごとに 1 つの LLM（my_llm_app.llm）が受け持つ。
  上限はこの窓口の中で守るので、エージェントがいくつあっても、モデルに同時に送る数は max_concurrency まで
- 待った秒数を応答に x_model_wait として付ける（流しながらの応答は最初のかたまりに）。エージェントはそれを
  調査の時間の上限から除く。やり直した後の失敗には gateway: true を付ける（エージェントはやり直さない）
- モデルが持たない能力を使う依頼（画像を読めないモデルに画像、JSON スキーマに沿えないモデルに json_schema）は、
  送る前に 400 で断る
- MODEL_GATEWAY_KEY を設定すると、/v1/* に鍵を求める
- 設定ファイルは MODELS_CONFIG（なければ config/models.toml）
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from openai import APIConnectionError, APIStatusError

from my_llm_app.config import Settings
from my_llm_app.llm import LLM
from my_llm_app.modelgw.registry import ModelSpec, Usage, load_catalog

log = logging.getLogger(__name__)

# OpenAI の SDK が名前で受け取る項目。それ以外は extra_body に入れてそのまま送る
SDK_PARAMS = {"temperature", "top_p", "max_tokens", "max_completion_tokens", "response_format", "stop", "seed",
              "presence_penalty", "frequency_penalty", "n", "tools", "tool_choice", "parallel_tool_calls",
              "stream_options", "logprobs", "top_logprobs", "user"}
HEALTH_SECONDS = 20


class Gate:
    """モデルに同時に送る数の上限。今送っている数と、その最大も数える（上限を守れているかを確かめるため）。"""

    def __init__(self, limit: int, usage: Usage) -> None:
        self._sem = threading.Semaphore(limit)
        self.usage = usage

    def __enter__(self) -> None:
        self._sem.acquire()
        with self.usage.lock:
            self.usage.sending += 1
            self.usage.peak_sending = max(self.usage.peak_sending, self.usage.sending)

    def __exit__(self, *exc) -> None:
        with self.usage.lock:
            self.usage.sending -= 1
        self._sem.release()


class Model:
    def __init__(self, spec: ModelSpec, http_client: httpx.Client | None = None) -> None:
        self.spec = spec
        self.usage = Usage()
        settings = Settings(llm_base_url=spec.base_url, llm_model=spec.model, llm_api_key=spec.api_key,
                            searxng_url="", llm_timeout=spec.timeout)
        self.llm = LLM(settings, http_client=http_client, semaphore=Gate(spec.max_concurrency, self.usage),
                       extra_body=spec.extra_body)


def needs_of(body: dict) -> set[str]:
    """依頼が使う能力（画像を読む・JSON スキーマに沿う）。"""
    out = set()
    rf = body.get("response_format") or {}
    if isinstance(rf, dict) and rf.get("type") == "json_schema":
        out.add("json_schema")
    for m in body.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in content):
            out.add("vision")
    return out


def create_app(models_path: Path | None = None, specs: list[ModelSpec] | None = None, default: str | None = None,
               key: str | None = None, http_client: httpx.Client | None = None,
               roles: dict[str, str] | None = None) -> FastAPI:
    if specs is None:
        from my_llm_app.config import REPO_ROOT
        catalog = load_catalog(models_path or Path(os.getenv("MODELS_CONFIG", "") or REPO_ROOT / "config" / "models.toml"))
        specs, default, roles = catalog.specs, catalog.default, catalog.roles
    default = default or specs[0].id
    roles = dict(roles or {})
    key = os.getenv("MODEL_GATEWAY_KEY", "") if key is None else key
    models = {s.id: Model(s, http_client) for s in specs}
    app = FastAPI(title="model-gateway")
    app.state.models = models

    def check(authorization: str | None) -> None:
        if key and authorization != f"Bearer {key}":
            raise HTTPException(status_code=401, detail="invalid model gateway key")

    def resolve(name: str) -> Model:
        name = (name or "").strip()
        if name in ("", "default"):
            return models[default]
        if name.startswith("role:"):                                # 役割の既定のモデル（書いていなければ既定）
            return models[roles.get(name[5:]) or default]
        if name in models:
            return models[name]
        for m in models.values():                                   # 上流のモデル名でも受ける（qwen3.8-27b など）
            if m.spec.model == name:
                return m
        raise HTTPException(status_code=404, detail=f"モデル {name} はありません")

    def failure(e: Exception, waited: float, model: Model) -> JSONResponse:
        status = e.status_code if isinstance(e, APIStatusError) else 503
        with model.usage.lock:
            model.usage.errors += 1
        log.warning("model %s failed: %s", model.spec.id, e)
        # OpenAI の SDK は error の中だけを呼び出し側に渡すので、印と待った秒数は error の中にも入れる
        mark = {"gateway": True, "x_model_wait": round(waited, 1), "x_model_id": model.spec.id}
        return JSONResponse(status_code=status, content={
            "error": {"message": f"{type(e).__name__}: {e}", "type": "upstream_error", "code": status, **mark}, **mark})

    def track(model: Model, started: float, waited: float, usage: object) -> None:
        with model.usage.lock:
            model.usage.calls += 1
            model.usage.seconds += time.perf_counter() - started
            model.usage.waited += waited
            if usage is not None:
                model.usage.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                model.usage.completion_tokens += getattr(usage, "completion_tokens", 0) or 0

    @app.get("/v1/models")
    def list_models(authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        return {"object": "list", "default": default, "roles": roles,
                "data": [m.spec.public(m.spec.id == default) for m in models.values()]}

    @app.get("/v1/usage")
    def usage(authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        return {"models": {k: m.usage.as_dict() for k, m in models.items()}}

    @app.post("/v1/chat/completions")
    def chat(body: dict, authorization: str | None = Header(default=None)):
        check(authorization)
        model = resolve(str(body.get("model", "")))
        messages = body.get("messages") or []
        if not messages:
            raise HTTPException(status_code=400, detail="messages が空です")
        missing = needs_of(body) - set(model.spec.capabilities)
        if missing:
            raise HTTPException(status_code=400, detail=f"モデル {model.spec.id} は {', '.join(sorted(missing))} に対応していません")
        kwargs = {k: v for k, v in body.items() if k in SDK_PARAMS}
        extra = {k: v for k, v in body.items() if k not in SDK_PARAMS | {"model", "messages", "stream", "extra_body"}}
        extra.update(body.get("extra_body") or {})
        if extra:
            kwargs["extra_body"] = extra
        started = time.perf_counter()
        with model.usage.lock:
            model.usage.active += 1
        if not body.get("stream"):
            try:
                r, waited = model.llm.call(messages, **kwargs)
            except (APIConnectionError, APIStatusError) as e:
                return failure(e, getattr(e, "model_wait", 0.0), model)
            finally:
                with model.usage.lock:
                    model.usage.active -= 1
            track(model, started, waited, r.usage)
            return JSONResponse({**r.model_dump(exclude_none=True), "x_model_wait": round(waited, 1),
                                 "x_model_id": model.spec.id})
        return stream(model, messages, kwargs, started)

    def stream(model: Model, messages: list[dict], kwargs: dict, started: float):
        waited = [0.0]
        chunks = model.llm.stream_chunks(messages, on_wait=lambda s: waited.__setitem__(0, waited[0] + s), **kwargs)
        try:
            first = next(chunks, None)        # 最初のかたまりまで（やり直しを含む）を待ってから、応答を始める
        except (APIConnectionError, APIStatusError) as e:
            with model.usage.lock:
                model.usage.active -= 1
            return failure(e, waited[0], model)

        def gen() -> Iterator[str]:
            usage = None
            try:
                if first is not None:
                    usage = first.usage
                    yield f"data: {json.dumps({**first.model_dump(exclude_none=True), 'x_model_wait': round(waited[0], 1), 'x_model_id': model.spec.id}, ensure_ascii=False)}\n\n"
                for chunk in chunks:
                    usage = chunk.usage or usage
                    yield f"data: {json.dumps(chunk.model_dump(exclude_none=True), ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except (APIConnectionError, APIStatusError) as e:        # 途中で切れた（やり直さない）
                log.warning("model %s stream broke: %s", model.spec.id, e)
                with model.usage.lock:
                    model.usage.errors += 1
                yield f"data: {json.dumps({'error': {'message': f'{type(e).__name__}: {e}'}, 'gateway': True})}\n\n"
            finally:
                with model.usage.lock:
                    model.usage.active -= 1
                track(model, started, waited[0], usage)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    health_cache: dict = {}

    @app.get("/health")
    def health() -> dict:
        if health_cache and time.time() - health_cache["at"] < HEALTH_SECONDS:
            return health_cache["value"]
        from my_llm_app.tools import http as http_tools

        def check_model(m: Model) -> dict:
            started = time.time()
            out = {"title": m.spec.title, "model": m.spec.model, "active": m.usage.as_dict()["active"],
                   "max_concurrency": m.spec.max_concurrency}
            try:
                r = http_tools.get(m.spec.base_url.rstrip("/") + "/models", timeout=4, cache=False, public=False,
                                   headers={"Authorization": f"Bearer {m.spec.api_key}"})
                return {**out, "ok": r.status_code == 200, "seconds": round(time.time() - started, 1)}
            except Exception as e:  # noqa: BLE001 - つながらない理由を画面に出す
                return {**out, "ok": False, "error": type(e).__name__}

        value = {"ok": True, "default": default, "models": {k: check_model(m) for k, m in models.items()}}
        health_cache.update(at=time.time(), value=value)
        return value

    return app
