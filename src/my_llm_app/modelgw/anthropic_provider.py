"""Claude（Anthropic の Messages API）の変換の部品（docs/telemetry_enhancement_plan.md 6.3）。

窓口の中では、どのモデルも OpenAI の SDK と同じ呼び方（client.chat.completions.create）で扱う。
この部品はその形を受けて Anthropic の公式 SDK で送り、応答と失敗を OpenAI の形にそろえて返す。
そのため、やり直し・待ち時間の数え方・同時に送る数の上限（my_llm_app.llm.LLM）がそのまま効く。

変換するもの
- system の発言 → system。画像（data URL・URL）→ Claude の image。json_schema → output_config.format
- temperature・top_p は送らない（今の Claude は受け付けない）。max_tokens は必ず送る（依頼になければモデルの設定）
- 応答の文（思考の塊は除く）・終わった理由・トークン数（キャッシュの分を含む）
- 失敗 → OpenAI の SDK の例外（状態コードはそのまま。429・5xx・接続できないものは窓口がやり直す）
"""

from __future__ import annotations

import copy
import re
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import openai
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from my_llm_app.modelgw.registry import ModelSpec

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# 送らない項目（今の Claude は受け付けない・OpenAI 互換のローカルのモデル向けのもの）
DROPPED = {"temperature", "top_p", "seed", "presence_penalty", "frequency_penalty", "n", "logprobs", "top_logprobs",
           "user", "stream_options", "parallel_tool_calls", "chat_template_kwargs"}
# SDK に名前で渡す項目（それ以外の extra_body の項目は、そのまま本文に入れる）
NAMED = {"thinking", "speed", "inference_geo", "metadata", "service_tier", "top_k"}
FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "refusal": "content_filter",
          "tool_use": "tool_calls", "pause_turn": "stop"}
# JSON スキーマのうち、Claude の構造化出力が受け付けない制約（外しても、窓口の呼び出し側が中身を確かめる）
UNSUPPORTED_SCHEMA_KEYS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                           "minLength", "maxLength", "maxItems", "uniqueItems", "pattern"}
DATA_URL = re.compile(r"^data:(?P<type>[\w/+.-]+);base64,(?P<data>.*)$", re.S)


class UnsupportedRequest(ValueError):
    """Claude に変換できない依頼（ツールの呼び出しなど）。窓口は 400 で断る。"""


def strict_schema(schema: dict) -> dict:
    """Claude の構造化出力が受け付ける形にする（object には additionalProperties: false、使えない制約は外す）。"""
    def fix(node: Any) -> Any:
        if isinstance(node, list):
            return [fix(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k in UNSUPPORTED_SCHEMA_KEYS or (k == "minItems" and isinstance(v, int) and v > 1):
                continue
            out[k] = fix(v) if k not in ("enum", "const", "required") else v
        if out.get("type") == "object" or "properties" in out:
            out["additionalProperties"] = False
        return out
    return fix(copy.deepcopy(schema))


def _image(url: str) -> dict:
    m = DATA_URL.match(url)
    if m:
        return {"type": "image", "source": {"type": "base64", "media_type": m["type"], "data": m["data"]}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _content(content: Any) -> str | list[dict]:
    if isinstance(content, str) or content is None:
        return content or ""
    parts = []
    for p in content:
        kind = p.get("type") if isinstance(p, dict) else None
        if kind == "text":
            parts.append({"type": "text", "text": p.get("text", "")})
        elif kind == "image_url":
            image = p.get("image_url") or {}
            parts.append(_image(image.get("url", "") if isinstance(image, dict) else str(image)))
        else:
            raise UnsupportedRequest(f"Claude に渡せない発言の部品です: {kind}")
    return parts


def to_anthropic(messages: list[dict], kwargs: dict, spec: ModelSpec) -> dict:
    """OpenAI の形の依頼を、Anthropic の SDK の messages.create の引数にする。"""
    if kwargs.get("tools") or kwargs.get("tool_choice"):
        raise UnsupportedRequest("この窓口の Claude の部品は、ツールの呼び出しにまだ対応していません")
    system, out = [], []
    for m in messages:
        role = m.get("role")
        if role in ("system", "developer"):
            text = m.get("content")
            system.append(text if isinstance(text, str) else
                          "\n".join(p.get("text", "") for p in text or [] if isinstance(p, dict)))
        elif role in ("user", "assistant"):
            content = _content(m.get("content"))
            if content:                                   # 空の発言は Claude が受け付けない
                out.append({"role": role, "content": content})
        else:
            raise UnsupportedRequest(f"Claude に渡せない発言です（role {role}）")
    params: dict[str, Any] = {
        "model": spec.model, "messages": out,
        "max_tokens": int(kwargs.get("max_completion_tokens") or kwargs.get("max_tokens") or spec.max_tokens)}
    if system:
        params["system"] = "\n\n".join(s for s in system if s)
    stop = kwargs.get("stop")
    if stop:
        params["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    extra = {k: v for k, v in (kwargs.get("extra_body") or {}).items() if k not in DROPPED}
    output_config = dict(extra.pop("output_config", None) or {})
    rf = kwargs.get("response_format") or {}
    if isinstance(rf, dict) and rf.get("type") == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema") or {}
        output_config["format"] = {"type": "json_schema", "schema": strict_schema(schema)}
    if output_config:
        params["output_config"] = output_config
    for k in list(extra):
        if k in NAMED:
            params[k] = extra.pop(k)
    if extra:
        params["extra_body"] = extra
    if kwargs.get("extra_headers"):
        params["extra_headers"] = kwargs["extra_headers"]
    if spec.fallbacks:                                    # 断られたら、同じ呼び出しの中で代わりのモデルが答える
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = spec.fallbacks
    return params


def _usage(u: Any) -> dict:
    """Claude のトークン数を OpenAI の形にする（prompt_tokens はキャッシュの分も含めた入力の合計）。"""
    inp = getattr(u, "input_tokens", 0) or 0
    read = getattr(u, "cache_read_input_tokens", 0) or 0
    write = getattr(u, "cache_creation_input_tokens", 0) or 0
    out = getattr(u, "output_tokens", 0) or 0
    return {"prompt_tokens": inp + read + write, "completion_tokens": out, "total_tokens": inp + read + write + out,
            "prompt_tokens_details": {"cached_tokens": read}, "x_cache_write_tokens": write}


def to_openai(message: Any) -> ChatCompletion:
    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    return ChatCompletion.model_validate({
        "id": message.id, "object": "chat.completion", "created": int(time.time()), "model": message.model,
        "choices": [{"index": 0, "finish_reason": FINISH.get(message.stop_reason or "", "stop"),
                     "message": {"role": "assistant", "content": text}}],
        "usage": _usage(message.usage)})


def _openai_error(e: Exception) -> Exception:
    """Anthropic の SDK の失敗を、OpenAI の SDK の失敗にする（窓口のやり直しと失敗の返し方をそろえるため）。"""
    if isinstance(e, anthropic.APIStatusError):
        return openai.APIStatusError(e.message, response=e.response, body=e.body)
    if isinstance(e, anthropic.APITimeoutError):
        return openai.APITimeoutError(request=e.request)
    if isinstance(e, anthropic.APIConnectionError):
        return openai.APIConnectionError(message=str(e), request=e.request)
    return e


class _Completions:
    def __init__(self, owner: "AnthropicChat") -> None:
        self.owner = owner

    def create(self, model: str | None = None, messages: list[dict] | None = None, stream: bool = False,
               **kwargs: Any) -> Any:
        spec = self.owner.spec
        params = to_anthropic(messages or [], kwargs, spec)
        if model:
            params["model"] = model
        api = self.owner.client.beta.messages if "betas" in params else self.owner.client.messages
        if not stream:
            try:
                return to_openai(api.create(**params))
            except anthropic.AnthropicError as e:
                raise _openai_error(e) from e
        try:
            events = api.create(stream=True, **params)
        except anthropic.AnthropicError as e:
            raise _openai_error(e) from e
        return self._chunks(events)

    @staticmethod
    def _chunks(events: Any) -> Iterator[ChatCompletionChunk]:
        """Claude の流しながらの応答を、OpenAI の流しながらの応答のかたまりにする。"""
        head: dict = {"id": "", "model": "", "created": int(time.time())}
        usage: Any = None
        stop = None

        def chunk(delta: dict, finish: str | None = None, extra: dict | None = None) -> ChatCompletionChunk:
            return ChatCompletionChunk.model_validate({
                **head, "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **(extra or {})})
        try:
            for ev in events:
                kind = getattr(ev, "type", "")
                if kind == "message_start":
                    head.update(id=ev.message.id, model=ev.message.model)
                    usage = ev.message.usage
                    yield chunk({"role": "assistant", "content": ""})
                elif kind == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                    yield chunk({"content": ev.delta.text})
                elif kind == "message_delta":
                    stop = getattr(ev.delta, "stop_reason", None) or stop
                    merged = {k: getattr(usage, k, 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                                 "cache_creation_input_tokens")}
                    for k, v in (ev.usage.model_dump() if ev.usage else {}).items():
                        if v is not None and k in merged | {"output_tokens": 0}:
                            merged[k] = v
                    usage = SimpleNamespace(**merged)
            yield chunk({}, FINISH.get(stop or "", "stop"), {"usage": _usage(usage) if usage else None})
        except anthropic.AnthropicError as e:
            raise _openai_error(e) from e


class _Chat:
    def __init__(self, owner: "AnthropicChat") -> None:
        self.completions = _Completions(owner)


class AnthropicChat:
    """OpenAI の SDK の client と同じ呼び方（.chat.completions.create）で Claude を呼ぶ。"""

    def __init__(self, spec: ModelSpec, http_client: httpx2.Client | None = None) -> None:
        self.spec = spec
        # やり直しは窓口（LLM）だけで行うので、SDK のやり直しは 0 回にする
        self.client = anthropic.Anthropic(api_key=spec.api_key, base_url=spec.base_url or None, max_retries=0,
                                          timeout=spec.timeout, http_client=http_client)
        self.chat = _Chat(self)
