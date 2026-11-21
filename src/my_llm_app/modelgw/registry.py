"""モデルの一覧（config/models.toml）。

接続先・鍵・モデル名は環境変数（.env）から読む。リポジトリには書かない。
"""

from __future__ import annotations

import os
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from my_llm_app import config  # noqa: F401 - .env を読み込む

PROVIDERS = {"openai"}    # OpenAI 互換（llama.cpp・Ollama・vLLM・OpenAI）。M3 で "anthropic" などを足す


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

    def public(self, default: bool = False) -> dict:
        """一覧に出す形（接続先と鍵は出さない）。"""
        return {"id": self.id, "object": "model", "created": 0, "owned_by": "model-gateway", "title": self.title,
                "provider": self.provider, "upstream_model": self.model, "capabilities": self.capabilities,
                "context_tokens": self.context_tokens, "max_concurrency": self.max_concurrency, "default": default}


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

    def model_for_role(self, role: str) -> str:
        return self.roles.get(role) or self.default


def load_catalog(path: Path) -> Catalog:
    specs, default = load(path)
    roles = {str(k): str(v) for k, v in tomllib.loads(path.read_text(encoding="utf-8")).get("roles", {}).items()}
    ids = {s.id for s in specs}
    for role, model in roles.items():
        if model not in ids:
            raise ConfigError(f"役割 {role} のモデル {model} が一覧にありません")
    return Catalog(specs, default, roles)


def load(path: Path) -> tuple[list[ModelSpec], str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    specs = []
    for item in data.get("models", []):
        provider = item.get("provider", "openai")
        if provider not in PROVIDERS:
            raise ConfigError(f"{item.get('id')}: provider {provider} にはまだ対応していません")
        spec = ModelSpec(
            id=item["id"], title=item.get("title", item["id"]), provider=provider,
            base_url=_env_or(item, "base_url"), api_key=_env_or(item, "api_key") or "dummy",
            model=_env_or(item, "model"), capabilities=list(item.get("capabilities", [])),
            max_concurrency=int(item.get("max_concurrency", 2)), context_tokens=int(item.get("context_tokens", 0)),
            extra_body=dict(item.get("extra_body", {})), timeout=float(item.get("timeout", 300)))
        if not spec.base_url:
            raise ConfigError(f"{spec.id}: 接続先がありません（{item.get('base_url_env', 'base_url')} を設定してください）")
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
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        with self.lock:
            return {"calls": self.calls, "errors": self.errors, "active": self.active, "sending": self.sending,
                    "peak_sending": self.peak_sending,
                    "seconds": round(self.seconds, 1), "waited": round(self.waited, 1),
                    "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}
