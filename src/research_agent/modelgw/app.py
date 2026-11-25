"""モデルの窓口の API（OpenAI 互換。docs/telemetry_enhancement_plan.md 6.1）。

    uv run uvicorn --factory research_agent.modelgw.app:create_app --port 8103

    GET  /v1/models              使えるモデル（OpenAI の形 + title・capabilities など）
    POST /v1/chat/completions    OpenAI 互換。model は id・モデル名・"default"・空（既定）・"role:<役割>"（[roles] の対応、なければ既定）
    GET  /v1/usage               モデルごとの使用量と費用、今日の費用と上限
    GET  /v1/usage/runs/{id}     1 つの調査の費用（エージェントが X-Model-Budget に入れた調査の id）
    GET  /health                 モデルごとにつながるか（20 秒覚えておく）

- provider が "anthropic" のモデル（Claude）は、変換の部品（anthropic_provider）を通して送る。応答は OpenAI の形
- 値段（price）を書いたモデルは、呼び出しごとに費用を数える（応答に x_model_cost を付ける）。
  費用の上限（[limits]）に達していたら、送る前に 402 で断る。鍵のないモデル（使えない）は 503 で断る
- 費用の記録は MODEL_SPEND_FILE（なければ runs/model_spend.json。設定ファイルから立ち上げたとき）

- やり直し・待ち時間の数え方・同時実行の上限は、モデルごとに 1 つの LLM（research_agent.llm）が受け持つ。
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

from research_agent.config import Settings
from research_agent.llm import LLM
from research_agent.modelgw.anthropic_provider import AnthropicChat, UnsupportedRequest
from research_agent.modelgw.registry import ModelSpec, Usage, load_catalog
from research_agent.modelgw.spend import Spend

log = logging.getLogger(__name__)

# OpenAI の SDK が名前で受け取る項目。それ以外は extra_body に入れてそのまま送る
SDK_PARAMS = {"temperature", "top_p", "max_tokens", "max_completion_tokens", "response_format", "stop", "seed",
              "presence_penalty", "frequency_penalty", "n", "tools", "tool_choice", "parallel_tool_calls",
              "stream_options", "logprobs", "top_logprobs", "user"}
HEALTH_SECONDS = 20
PROBE_SECONDS = 4.0      # 接続の確認を打ち切る秒数（やり直さない）
RUN_LOCK_SECONDS = 600.0  # 同じ調査の値段のある呼び出しの順番を待つ上限（前の呼び出しが終わらないときに待ち続けない）


class Once:
    """鍵を 1 回だけ放す（失敗・成功・ストリームの終わりのどこから呼ばれても、二重に放さない）。"""

    def __init__(self, lock: threading.Lock | None) -> None:
        self._lock, self._done, self._mu = lock, lock is None, threading.Lock()

    def __call__(self) -> None:
        with self._mu:
            if self._done:
                return
            self._done = True
        self._lock.release()


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
    def __init__(self, spec: ModelSpec, http_client: httpx.Client | None = None, anthropic_http: object = None) -> None:
        self.spec = spec
        self.usage = Usage()
        settings = Settings(llm_base_url=spec.base_url or "http://unused/v1", llm_model=spec.model,
                            llm_api_key=spec.api_key, searxng_url="", llm_timeout=spec.timeout)
        # Claude は変換の部品を通す（OpenAI の SDK と同じ呼び方なので、やり直し・上限・待ち時間はそのまま効く）
        client = AnthropicChat(spec, anthropic_http) if spec.provider == "anthropic" else None
        self.llm = LLM(settings, http_client=http_client, semaphore=Gate(spec.max_concurrency, self.usage),
                       extra_body=spec.extra_body, client=client)


def usage_parts(usage: object) -> dict:
    """応答のトークン数を、費用の計算に使う形にする（キャッシュから読んだ分・書いた分を分ける）。"""
    if usage is None:
        return {}
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    read = (getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
    write = int((getattr(usage, "model_extra", None) or {}).get("x_cache_write_tokens") or 0)
    return {"prompt_tokens": max(prompt - read - write, 0), "cache_read_tokens": read, "cache_write_tokens": write,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0}


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
               roles: dict[str, str] | None = None, limits: dict | None = None, spend_file: Path | None = None,
               anthropic_http: object = None) -> FastAPI:
    """anthropic_http は Claude の部品が使う HTTP の部品（httpx2.Client。テストで偽の応答を返すため）。"""
    if specs is None:
        from research_agent.config import REPO_ROOT
        catalog = load_catalog(models_path or Path(os.getenv("MODELS_CONFIG", "") or REPO_ROOT / "config" / "models.toml"))
        specs, default, roles, limits = catalog.specs, catalog.default, catalog.roles, catalog.limits
        spend_file = spend_file or Path(os.getenv("MODEL_SPEND_FILE", "") or REPO_ROOT / "runs" / "model_spend.json")
    default = default or specs[0].id
    roles = dict(roles or {})
    key = os.getenv("MODEL_GATEWAY_KEY", "") if key is None else key
    models = {s.id: Model(s, http_client, anthropic_http) for s in specs}
    # 接続の確認に使う HTTP の部品。窓口がモデルを呼ぶのと同じもので、やり直しをせずに 1 回だけ確かめる
    probe = http_client or httpx.Client(trust_env=False)
    spend = Spend(limits, spend_file)
    app = FastAPI(title="model-gateway")
    app.state.models = models
    app.state.spend = spend

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

    def refuse(status: int, message: str, model: Model, kind: str) -> JSONResponse:
        """送らずに断る（やり直しても同じなので、エージェントにやり直させない印を付ける）。"""
        mark = {"gateway": True, "x_model_wait": 0.0, "x_model_id": model.spec.id}
        return JSONResponse(status_code=status, content={
            "error": {"message": message, "type": kind, "code": status, **mark}, **mark})

    def failure(e: Exception, waited: float, model: Model) -> JSONResponse:
        status = e.status_code if isinstance(e, APIStatusError) else 503
        with model.usage.lock:
            model.usage.errors += 1
        log.warning("model %s failed: %s", model.spec.id, e)
        # OpenAI の SDK は error の中だけを呼び出し側に渡すので、印と待った秒数は error の中にも入れる
        mark = {"gateway": True, "x_model_wait": round(waited, 1), "x_model_id": model.spec.id}
        return JSONResponse(status_code=status, content={
            "error": {"message": f"{type(e).__name__}: {e}", "type": "upstream_error", "code": status, **mark}, **mark})

    def track(model: Model, started: float, waited: float, usage: object, budget: str | None) -> float:
        """使用量を足し、この呼び出しの費用（ドル）を返す。値段のあるモデルで使用量が返らなければ「未計測」に数える。"""
        parts = usage_parts(usage)
        cost = model.spec.cost(parts)
        if model.spec.price and usage is None:
            with model.usage.lock:
                model.usage.unmeasured += 1
            spend.add_unmeasured(model.spec.id, budget)
        with model.usage.lock:
            model.usage.calls += 1
            model.usage.seconds += time.perf_counter() - started
            model.usage.waited += waited
            if usage is not None:
                model.usage.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                model.usage.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            model.usage.cost += cost
        spend.add(model.spec.id, budget, cost)
        return cost

    @app.get("/v1/models")
    def list_models(authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        return {"object": "list", "default": default, "roles": roles,
                "data": [m.spec.public(m.spec.id == default) for m in models.values()]}

    @app.get("/v1/usage")
    def usage(authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        return {"models": {k: m.usage.as_dict() for k, m in models.items()}, "spend": spend.as_dict()}

    @app.get("/v1/usage/runs/{budget}")
    def run_usage(budget: str, authorization: str | None = Header(default=None)) -> dict:
        check(authorization)
        return spend.run(budget)

    @app.post("/v1/chat/completions")
    def chat(body: dict, authorization: str | None = Header(default=None),
             x_model_budget: str | None = Header(default=None)):
        check(authorization)
        model = resolve(str(body.get("model", "")))
        messages = body.get("messages") or []
        if not messages:
            raise HTTPException(status_code=400, detail="messages が空です")
        missing = needs_of(body) - set(model.spec.capabilities)
        if missing:
            raise HTTPException(status_code=400, detail=f"モデル {model.spec.id} は {', '.join(sorted(missing))} に対応していません")
        if model.spec.unavailable:
            return refuse(503, f"モデル {model.spec.id} は使えません（{model.spec.unavailable}）", model, "model_unavailable")
        # 値段のあるモデル: 同じ調査の呼び出しは 1 件ずつ。予算の判定（送る直前）から費用の記録までを鍵の中で行う
        held = spend.lock_for(x_model_budget) if model.spec.price and x_model_budget else None
        if held and not held.acquire(timeout=RUN_LOCK_SECONDS):
            return refuse(429, "同じ調査の前の呼び出しが終わらないため、送れません", model, "run_busy")
        release = Once(held)
        try:
            return send(model, body, messages, x_model_budget, release)
        except BaseException:
            release()
            raise

    def send(model: Model, body: dict, messages: list[dict], x_model_budget: str | None, release: Once):
        """送る直前に予算を判定して送る。release（調査ごとの鍵を放す）は、費用を記録した後に呼ぶ
        （ストリームは流し終えた後。ストリームでない呼び出しは、成功・失敗のどちらでも最後に）。"""
        if model.spec.price:
            reason = spend.refusal(x_model_budget)
            if reason:
                with model.usage.lock:
                    model.usage.refused += 1
                release()
                return refuse(402, reason, model, "budget_exceeded")
        kwargs = {k: v for k, v in body.items() if k in SDK_PARAMS and k not in model.spec.drop_params}
        if body.get("stream") and model.spec.price and model.spec.provider != "anthropic":
            # OpenAI 互換の上流は、頼んだときだけストリームの最後に使用量を返す（頼まないと費用が 0 ドルになっていた）
            kwargs["stream_options"] = {**(kwargs.get("stream_options") or {}), "include_usage": True}
        extra = {k: v for k, v in body.items() if k not in SDK_PARAMS | {"model", "messages", "stream", "extra_body"}}
        extra.update(body.get("extra_body") or {})
        for k in model.spec.drop_params:
            extra.pop(k, None)
        if extra:
            kwargs["extra_body"] = extra
        started = time.perf_counter()
        with model.usage.lock:
            model.usage.active += 1
        if not body.get("stream"):
            try:
                try:
                    r, waited = model.llm.call(messages, **kwargs)
                except (APIConnectionError, APIStatusError) as e:
                    return failure(e, getattr(e, "model_wait", 0.0), model)
                except UnsupportedRequest as e:
                    return refuse(400, str(e), model, "unsupported_request")
                finally:
                    with model.usage.lock:
                        model.usage.active -= 1
                cost = track(model, started, waited, r.usage, x_model_budget)
            finally:
                release()                                   # 費用を記録してから、同じ調査の次の呼び出しを通す
            return JSONResponse({**r.model_dump(exclude_none=True), "x_model_wait": round(waited, 1),
                                 "x_model_id": model.spec.id, "x_model_cost": round(cost, 6)})
        return stream(model, messages, kwargs, started, x_model_budget, release)

    def stream(model: Model, messages: list[dict], kwargs: dict, started: float, budget: str | None, release):
        waited = [0.0]
        chunks = model.llm.stream_chunks(messages, on_wait=lambda s: waited.__setitem__(0, waited[0] + s), **kwargs)
        try:
            first = next(chunks, None)        # 最初のかたまりまで（やり直しを含む）を待ってから、応答を始める
        except (APIConnectionError, APIStatusError) as e:
            with model.usage.lock:
                model.usage.active -= 1
            release()
            return failure(e, waited[0], model)
        except UnsupportedRequest as e:
            with model.usage.lock:
                model.usage.active -= 1
            release()
            return refuse(400, str(e), model, "unsupported_request")

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
                try:
                    track(model, started, waited[0], usage, budget)
                finally:
                    release()

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    health_cache: dict = {}

    @app.get("/health")
    def health() -> dict:
        """モデルごとに、今つながるかを素早く確かめる（画面のモデルの窓・エージェントの接続の表示）。

        前は汎用の取得の部品（やり直しあり）を使っていたので、VPN が切れていると、つながらないことが分かるまで
        1 分近く待たせていた。確認はやり直さず、PROBE_SECONDS で打ち切る。"""
        if health_cache and time.time() - health_cache["at"] < HEALTH_SECONDS:
            return health_cache["value"]

        def check_model(m: Model) -> dict:
            started = time.time()
            out = {"title": m.spec.title, "model": m.spec.model, "active": m.usage.as_dict()["active"],
                   "max_concurrency": m.spec.max_concurrency, "external": m.spec.external}
            if m.spec.unavailable:
                return {**out, "ok": False, "error": m.spec.unavailable}
            try:
                if m.spec.provider == "anthropic":           # 一覧を 1 件だけ読む（費用はかからない）
                    m.llm.client.client.with_options(timeout=PROBE_SECONDS, max_retries=0).models.list(limit=1)
                    return {**out, "ok": True, "seconds": round(time.time() - started, 1)}
                r = probe.get(m.spec.base_url.rstrip("/") + "/models", timeout=PROBE_SECONDS,
                              headers={"Authorization": f"Bearer {m.spec.api_key}"})
                return {**out, "ok": r.status_code == 200, "seconds": round(time.time() - started, 1)}
            except Exception as e:  # noqa: BLE001 - つながらない理由を画面に出す
                return {**out, "ok": False, "error": type(e).__name__}

        value = {"ok": True, "default": default, "models": {k: check_model(m) for k, m in models.items()}}
        health_cache.update(at=time.time(), value=value)
        return value

    return app
