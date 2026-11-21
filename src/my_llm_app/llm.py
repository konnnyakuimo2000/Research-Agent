"""LLM (OpenAI 互換 API) の呼び出し。

2 つの使われ方がある（docs/telemetry_enhancement_plan.md 6 章）。
- エージェントの中: モデルの窓口（model-gateway）を呼ぶ（settings.llm_gateway が True）。
  やり直しと同時実行の上限は窓口が受け持つ。窓口が返す「待った秒数」（x_model_wait）を足し合わせる。
  窓口そのものにつながらないときだけ、ここでやり直す
- 窓口の中、または窓口を使わずに動かすとき: モデルを直接呼び、やり直し・同時実行の上限・待った秒数をここで扱う
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, OpenAI

from my_llm_app.config import Settings

# 研究室サーバーは共有で同時処理数 4。こちらからの同時リクエストはそれ以下に抑える（窓口を使わないときの既定）
_MAX_CONCURRENCY = 2
_semaphore = threading.Semaphore(_MAX_CONCURRENCY)
# 研究室サーバーが一時的に応答しなくなることがあるため、間隔を広げながらやり直す
_RETRY_WAITS = (10, 30, 60)
# 研究室の qwen 向けの既定（思考モード OFF。応答速度を優先）。窓口を使うときは窓口の設定（models.toml）に従う
_DIRECT_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}

# やり直す失敗（一時的なもの）: 接続できない・待ち時間切れ（APIConnectionError とその子の APITimeoutError）と、
# 状態コード 408（待ち時間切れ）・409（競合）・429（混雑）・5xx（サーバーの障害）。SDK が既定でやり直すものと同じ範囲
_RETRY_STATUS = {408, 409, 429}

log = logging.getLogger(__name__)


class LLMOutputError(ValueError):
    """LLM の出力を、やり直しても JSON として読めなかった（呼び出した側で、その 1 件の失敗として扱う）。"""


def _transient(e: Exception) -> bool:
    if isinstance(e, APIConnectionError):
        return True
    return isinstance(e, APIStatusError) and (e.status_code in _RETRY_STATUS or e.status_code >= 500)


def _extra(obj: object) -> dict:
    """OpenAI の型に載っていない項目（窓口が付けた x_model_wait など）。"""
    return getattr(obj, "model_extra", None) or {}


def gateway_failure(e: Exception) -> tuple[bool, float]:
    """窓口がやり直した後の失敗か（そうならやり直さない）と、窓口が待った秒数。"""
    body = getattr(e, "body", None)
    if isinstance(body, dict) and body.get("gateway"):
        return True, float(body.get("x_model_wait") or 0)
    return False, 0.0


class LLM:
    def __init__(self, settings: Settings, http_client: httpx.Client | None = None,
                 semaphore: Any = None, extra_body: dict | None = None) -> None:
        self.model = settings.llm_model
        self.gateway = bool(getattr(settings, "llm_gateway", False))
        # 同時実行の上限: 窓口を呼ぶときは窓口が守るので、ここでは数えない
        self._sem = semaphore or (contextlib.nullcontext() if self.gateway else _semaphore)
        self.extra_body = extra_body if extra_body is not None else ({} if self.gateway else _DIRECT_EXTRA_BODY)
        # やり直しはこのクラス（_call）だけで行う。SDK のやり直し（既定 2 回）と重なると、
        # 1 回の呼び出しで最大 12 回送ってしまい、待った時間も数えられないため、SDK 側は 0 回にする
        self.client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout,
            max_retries=0,
            http_client=http_client,
        )
        # 接続できずに待った秒数の合計（失敗した呼び出しの時間 + やり直し前の待ち。窓口が待った分も含む）。
        # 調査の時間の上限から除くために使う（サーバーが止まっている間に上限を使い切らないように）
        self._waited = 0.0
        self._lock = threading.Lock()

    @property
    def waited(self) -> float:
        with self._lock:
            return self._waited

    def _add_wait(self, seconds: float) -> None:
        if seconds:
            with self._lock:
                self._waited += seconds

    def _kwargs(self, kwargs: dict) -> dict:
        extra = {**self.extra_body, **(kwargs.pop("extra_body", None) or {})}
        return {**kwargs, **({"extra_body": extra} if extra else {})}

    def _retry(self, e: Exception, attempt: int, started: float, on_wait: Callable[[float], None]) -> None:
        """やり直すなら待つ。やり直さないなら投げ直す。"""
        from_gateway, gw_wait = gateway_failure(e)
        if from_gateway:                      # 窓口がもうやり直した
            on_wait(gw_wait)
            raise e
        if not _transient(e):
            raise e                           # 400・401 などは、やり直しても同じ
        on_wait(time.perf_counter() - started)
        if attempt == len(_RETRY_WAITS):
            raise e
        wait = _RETRY_WAITS[attempt]
        log.warning("LLM に接続できません（%s）。%d 秒後にやり直します", type(e).__name__, wait)
        time.sleep(wait)
        on_wait(wait)

    def call(self, messages: list[dict], **kwargs: Any) -> tuple[Any, float]:
        """応答の全体と、この呼び出しで待った秒数。やり直しても失敗したら、例外に model_wait を付けて投げる。"""
        waited = 0.0

        def on_wait(seconds: float) -> None:
            nonlocal waited
            waited += seconds
            self._add_wait(seconds)

        kwargs = self._kwargs(kwargs)
        for attempt in range(len(_RETRY_WAITS) + 1):
            started = time.perf_counter()
            try:
                with self._sem:
                    r = self.client.chat.completions.create(model=self.model, messages=messages, **kwargs)
            except (APIConnectionError, APIStatusError) as e:
                try:
                    self._retry(e, attempt, started, on_wait)
                except Exception as final:
                    final.model_wait = waited  # type: ignore[attr-defined]
                    raise
                continue
            on_wait(float(_extra(r).get("x_model_wait") or 0))
            usage = r.usage
            log.info("llm: %.1fs (prompt %s, completion %s tokens)", time.perf_counter() - started,
                     usage.prompt_tokens if usage else "?", usage.completion_tokens if usage else "?")
            return r, waited
        raise RuntimeError("unreachable")

    def stream_chunks(self, messages: list[dict], on_wait: Callable[[float], None] | None = None,
                      **kwargs: Any) -> Iterator[Any]:
        """流しながらの応答のかたまり。やり直すのは、まだ何も受け取っていないときだけ（途中から送り直すと、文が重なるため）。"""
        def waited(seconds: float) -> None:
            self._add_wait(seconds)
            if on_wait and seconds:
                on_wait(seconds)

        kwargs = self._kwargs({**kwargs, "stream": True})
        for attempt in range(len(_RETRY_WAITS) + 1):
            started, got = time.perf_counter(), False
            try:
                with self._sem:
                    stream = self.client.chat.completions.create(model=self.model, messages=messages, **kwargs)
                    for chunk in stream:
                        if not got:
                            waited(float(_extra(chunk).get("x_model_wait") or 0))
                        got = True
                        yield chunk
                return
            except (APIConnectionError, APIStatusError) as e:
                if got:
                    raise
                self._retry(e, attempt, started, waited)

    def _create(self, messages: list[dict], **kwargs: Any) -> str:
        r, _ = self.call(messages, **kwargs)
        return r.choices[0].message.content or ""

    def chat_json(self, system: str, user: str, schema: dict, temperature: float = 0.2,
                  image_data_url: str | None = None) -> dict:
        """JSON スキーマに沿った出力を返す。壊れた JSON なら 1 回だけやり直す。画像を 1 枚添えられる。"""
        content: str | list = user
        if image_data_url:
            content = [{"type": "text", "text": user}, {"type": "image_url", "image_url": {"url": image_data_url}}]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        response_format = {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}}
        last_error: Exception | None = None
        for _ in range(2):
            text = self._create(messages, response_format=response_format, temperature=temperature)
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                last_error = e
        raise LLMOutputError(f"LLM の出力が JSON になりませんでした: {last_error}")

    def chat_messages(self, messages: list[dict], temperature: float = 0.5) -> str:
        """会話の履歴をそのまま送る。"""
        return self._create(messages, temperature=temperature)

    def stream_messages(self, messages: list[dict], temperature: float = 0.5) -> Iterator[str]:
        """会話の履歴を送り、返ってくる文を少しずつ返す（チャットのエージェント用）。"""
        for chunk in self.stream_chunks(messages, temperature=temperature):
            text = chunk.choices[0].delta.content if chunk.choices else None
            if text:
                yield text

    def chat_text(self, system: str, user: str, temperature: float = 0.5) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return self._create(messages, temperature=temperature)
