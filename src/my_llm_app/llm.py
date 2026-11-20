"""LLM サーバー (OpenAI 互換 API) の呼び出し。"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, OpenAI

from my_llm_app.config import Settings

# 研究室サーバーは共有で同時処理数 4。こちらからの同時リクエストはそれ以下に抑える
_MAX_CONCURRENCY = 2
_semaphore = threading.Semaphore(_MAX_CONCURRENCY)
# 研究室サーバーが一時的に応答しなくなることがあるため、間隔を広げながらやり直す
_RETRY_WAITS = (10, 30, 60)

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


class LLM:
    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self.model = settings.llm_model
        # やり直しはこのクラス（_create）だけで行う。SDK のやり直し（既定 2 回）と重なると、
        # 1 回の呼び出しで最大 12 回送ってしまい、待った時間も数えられないため、SDK 側は 0 回にする
        self.client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout,
            max_retries=0,
            http_client=http_client,
        )
        # 接続できずに待った秒数の合計（失敗した呼び出しの時間 + やり直し前の待ち）。
        # 調査の時間の上限から除くために使う（サーバーが止まっている間に上限を使い切らないように）
        self._waited = 0.0
        self._lock = threading.Lock()

    @property
    def waited(self) -> float:
        with self._lock:
            return self._waited

    def _add_wait(self, seconds: float) -> None:
        with self._lock:
            self._waited += seconds

    def _create(self, messages: list[dict], **kwargs: Any) -> str:
        for attempt in range(len(_RETRY_WAITS) + 1):
            started = time.perf_counter()
            try:
                with _semaphore:
                    r = self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        # 思考モード OFF（応答速度を優先）
                        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                        **kwargs,
                    )
            except (APIConnectionError, APIStatusError) as e:
                if not _transient(e):
                    raise                       # 400・401 などは、やり直しても同じ
                self._add_wait(time.perf_counter() - started)
                if attempt == len(_RETRY_WAITS):
                    raise
                wait = _RETRY_WAITS[attempt]
                log.warning("LLM に接続できません（%s）。%d 秒後にやり直します", type(e).__name__, wait)
                time.sleep(wait)
                self._add_wait(wait)
                continue
            usage = r.usage
            log.info("llm: %.1fs (prompt %s, completion %s tokens)", time.perf_counter() - started,
                     usage.prompt_tokens if usage else "?", usage.completion_tokens if usage else "?")
            return r.choices[0].message.content or ""
        raise RuntimeError("unreachable")

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
        """会話の履歴をそのまま送る（Open WebUI のタイトル生成などの裏処理を中継するため）。"""
        return self._create(messages, temperature=temperature)

    def chat_text(self, system: str, user: str, temperature: float = 0.5) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return self._create(messages, temperature=temperature)
