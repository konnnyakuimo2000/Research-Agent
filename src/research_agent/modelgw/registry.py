"""モデルの一覧（config/models.toml）。

接続先・鍵・モデル名は環境変数（.env）から読む。リポジトリには書かない。
Web API のモデル（鍵が要るもの）は、鍵がなければ「使えない」として一覧に残す（窓口は止めない）。
"""

from __future__ import annotations

import os
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from research_agent import config  # noqa: F401 - .env を読み込む

PROVIDERS = {"openai", "anthropic"}    # OpenAI 互換（llama.cpp・Ollama・vLLM・OpenAI の Web API）と Claude
EXTERNAL_PROVIDERS = {"anthropic"}     # 会社のサーバーに送るもの（OpenAI 互換は external = true で示す）


@dataclass
class ModelSpec:
    id: str
    title: str
    provider: str
    base_url: str
    api_key: str
    model: str
    capabilities: list[str] = field(default_factory=list)
    max_concurrency: int = 2
    context_tokens: int = 0
    extra_body: dict = field(default_factory=dict)
    timeout: float = 300.0
    price: dict = field(default_factory=dict)        # 100 万トークンあたりのドル（input・output・cache_read・cache_write）
    external: bool = False                           # 依頼文や読んだページが、外部の会社に送られる
    max_tokens: int = 16000                          # 出力の上限（Claude は必ず要る。依頼に書いてあればそちら）
    drop_params: list[str] = field(default_factory=list)   # このモデルが受け付けない項目（temperature など）
    fallbacks: object = None                         # Claude: 断られたときの代わりのモデル（"default" など）
    unavailable: str = ""                            # 使えない理由（鍵がないなど）。空なら使える

    def public(self, default: bool = False) -> dict:
        """一覧に出す形（接続先と鍵は出さない）。"""
        return {"id": self.id, "object": "model", "created": 0, "owned_by": "model-gateway", "title": self.title,
                "provider": self.provider, "upstream_model": self.model, "capabilities": self.capabilities,
                "context_tokens": self.context_tokens, "max_concurrency": self.max_concurrency, "default": default,
                "external": self.external, "price": self.price, "available": not self.unavailable,
                **({"unavailable": self.unavailable} if self.unavailable else {})}

    def cost(self, usage: dict) -> float:
        """1 回の呼び出しの費用（ドル）。値段を書いていないモデルは 0。"""
        if not self.price:
            return 0.0
        p = self.price
        inp = float(p.get("input_per_mtok", 0))
        return (usage.get("prompt_tokens", 0) * inp
                + usage.get("completion_tokens", 0) * float(p.get("output_per_mtok", 0))
                + usage.get("cache_read_tokens", 0) * float(p.get("cache_read_per_mtok", inp * 0.1))
                + usage.get("cache_write_tokens", 0) * float(p.get("cache_write_per_mtok", inp * 1.25))) / 1_000_000


class ConfigError(ValueError):
    pass


def _env_or(item: dict, key: str) -> str:
    env = item.get(f"{key}_env", "")
    return (os.getenv(env, "") if env else "") or str(item.get(key, ""))


@dataclass
class Catalog:
    specs: list[ModelSpec]
    default: str
    roles: dict[str, str] = field(default_factory=dict)
    limits: dict = field(default_factory=dict)       # 費用の上限 {daily_usd, run_usd}（値段のあるモデルの合計）

    def model_for_role(self, role: str) -> str:
        return self.roles.get(role) or self.default


def load_catalog(path: Path) -> Catalog:
    specs, default = load(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    roles = {str(k): str(v) for k, v in data.get("roles", {}).items()}
    limits = {k: float(v) for k, v in data.get("limits", {}).items() if k in ("daily_usd", "run_usd")}
    ids = {s.id for s in specs}
    for role, model in roles.items():
        if model not in ids:
            raise ConfigError(f"役割 {role} のモデル {model} が一覧にありません")
    return Catalog(specs, default, roles, limits)


def load(path: Path) -> tuple[list[ModelSpec], str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    specs = []
    for item in data.get("models", []):
        provider = item.get("provider", "openai")
        if provider not in PROVIDERS:
            raise ConfigError(f"{item.get('id')}: provider {provider} にはまだ対応していません")
        key = _env_or(item, "api_key")
        spec = ModelSpec(
            id=item["id"], title=item.get("title", item["id"]), provider=provider,
            base_url=_env_or(item, "base_url"), api_key=key or "dummy",
            model=_env_or(item, "model"), capabilities=list(item.get("capabilities", [])),
            max_concurrency=int(item.get("max_concurrency", 2)), context_tokens=int(item.get("context_tokens", 0)),
            extra_body=dict(item.get("extra_body", {})), timeout=float(item.get("timeout", 300)),
            price={k: float(v) for k, v in item.get("price", {}).items()},
            external=bool(item.get("external", provider in EXTERNAL_PROVIDERS)),
            max_tokens=int(item.get("max_tokens", 16000)), drop_params=list(item.get("drop_params", [])),
            fallbacks=item.get("fallbacks"))
        if provider == "openai" and not spec.base_url:
            raise ConfigError(f"{spec.id}: 接続先がありません（{item.get('base_url_env', 'base_url')} を設定してください）")
        if not spec.model:
            raise ConfigError(f"{spec.id}: モデル名がありません（model か model_env）")
        if item.get("key_required", spec.external) and not key:
            # Web API の鍵がない: 一覧には残し、選べない・送らないようにする（窓口は止めない）
            spec.unavailable = f"{item.get('api_key_env', 'api_key')} が設定されていません"
        specs.append(spec)
    if not specs:
        raise ConfigError("モデルが 1 つもありません")
    default = data.get("default") or specs[0].id
    if default not in {s.id for s in specs}:
        raise ConfigError(f"既定のモデル {default} が一覧にありません")
    return specs, default


@dataclass
class Usage:
    """モデルごとの使用量（窓口を立ち上げてから）。"""
    calls: int = 0
    errors: int = 0
    active: int = 0          # 窓口が受けて、まだ終わっていない数（上限で待っているものを含む）
    sending: int = 0         # 今モデルに送っている数（max_concurrency まで）
    peak_sending: int = 0
    seconds: float = 0.0
    waited: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0        # 費用（ドル。値段を書いたモデルだけ）
    refused: int = 0         # 費用の上限で断った数
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        with self.lock:
            return {"calls": self.calls, "errors": self.errors, "active": self.active, "sending": self.sending,
                    "peak_sending": self.peak_sending,
                    "seconds": round(self.seconds, 1), "waited": round(self.waited, 1),
                    "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                    "cost_usd": round(self.cost, 6), "refused": self.refused}
