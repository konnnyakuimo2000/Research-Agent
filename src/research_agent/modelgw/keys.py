"""Web API の鍵（画面の 設定 › API キー から入力する）。

- 鍵の形から提供元を判別する（Anthropic・OpenAI・Google Gemini・OpenRouter）。判別しただけでは使わず、その提供元の
  モデル一覧 API に鍵を送って、受け付けられるか確かめる（やり直さない。PROBE_SECONDS で打ち切る）
- 確かめた後、利用者が選んだモデルだけを窓口に足す。能力（画像・JSON スキーマ）と値段は、分かるものは一覧 API や
  既知の値から埋め、分からないものは利用者が画面で確かめて入れる（値段のない外部モデルは費用を数えられないため、
  値段は必ず受け取る。0 も可）
- 鍵は窓口だけが持つ（MODEL_KEYS_FILE。既定は runs/.secrets/model_keys.json。git の外）。画面には末尾 4 文字だけを返す
- 前は config/models.toml に Claude だけを書き、鍵がないと「ANTHROPIC_API_KEY が設定されていません」と出していた
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

import httpx

from research_agent.modelgw.registry import ModelSpec

log = logging.getLogger(__name__)

PROBE_SECONDS = 10.0
CAPABILITIES = ("json_schema", "vision")

# 提供元。provider は窓口の送り方（"anthropic" = Claude の変換、"openai" = OpenAI 互換）
PROVIDERS: dict[str, dict] = {
    "anthropic": {"title": "Anthropic（Claude）", "provider": "anthropic", "base_url": "",
                  "models_url": "https://api.anthropic.com/v1/models"},
    "openai": {"title": "OpenAI", "provider": "openai", "base_url": "https://api.openai.com/v1"},
    "gemini": {"title": "Google Gemini", "provider": "openai",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "openrouter": {"title": "OpenRouter", "provider": "openai", "base_url": "https://openrouter.ai/api/v1"},
}
# 鍵の頭の形（長いものから見る。sk-ant- と sk-or- は sk- より先）
PREFIXES = (("sk-ant-", "anthropic"), ("sk-or-", "openrouter"), ("AIza", "gemini"), ("sk-", "openai"))
# 会話に使えないモデル（埋め込み・音声・画像生成など）は一覧から外す
NOT_CHAT = re.compile(r"embed|tts|whisper|dall-e|moderation|transcribe|realtime|audio|imagen|veo|aqa|"
                      r"image-gen|gpt-image|davinci|babbage|computer-use|-search", re.I)
# 既知の値段（100 万トークンあたりのドル）。一覧 API が値段を返さない提供元で、画面の欄に先に入れておく
PRICE_HINTS = {
    "claude-opus-5-5": {"input_per_mtok": 4.0, "output_per_mtok": 20.0, "cache_read_per_mtok": 0.20},
    "claude-haiku-5-5": {"input_per_mtok": 0.10, "output_per_mtok": 0.50, "cache_read_per_mtok": 0.01},
}


class KeyProblem(ValueError):
    """鍵を判別できない・受け付けられない・つながらない（画面にそのまま出す文）。"""


def detect(key: str) -> str | None:
    key = (key or "").strip()
    for prefix, provider in PREFIXES:
        if key.startswith(prefix):
            return provider
    return None


def mask(key: str) -> str:
    return "…" + key[-4:] if len(key) > 8 else "…"


def _get(http: httpx.Client, url: str, title: str, headers: dict, params: dict | None = None) -> dict:
    try:
        r = http.get(url, headers=headers, params=params, timeout=PROBE_SECONDS)
    except httpx.HTTPError as e:
        raise KeyProblem(f"{title} につながりませんでした（{type(e).__name__}）") from None
    if r.status_code in (401, 403):
        raise KeyProblem(f"{title} が鍵を受け付けませんでした（{r.status_code}）")
    if r.status_code >= 400:
        raise KeyProblem(f"{title} のモデル一覧を読めませんでした（{r.status_code}）")
    try:
        return r.json()
    except ValueError:
        raise KeyProblem(f"{title} の応答を読めませんでした") from None


def list_models(key: str, http: httpx.Client) -> tuple[str, list[dict]]:
    """鍵を判別し、その提供元で使える会話のモデル（[{model, title, capabilities, price, context_tokens}]）を返す。
    capabilities・price が None のものは、提供元が教えないため、利用者が画面で入れる。"""
    key = (key or "").strip()
    provider = detect(key)
    if provider is None:
        raise KeyProblem("鍵の形から提供元を判別できません（Anthropic・OpenAI・Google Gemini・OpenRouter の鍵に対応）")
    p = PROVIDERS[provider]
    title, out = p["title"], []
    if provider == "anthropic":
        data = _get(http, p["models_url"], title, {"x-api-key": key, "anthropic-version": "2023-06-01"}, {"limit": 100})
        for m in data.get("data", []):
            out.append({"model": m["id"], "title": m.get("display_name") or m["id"], "capabilities": list(CAPABILITIES),
                        "price": PRICE_HINTS.get(m["id"]), "context_tokens": 0})
    elif provider == "openrouter":
        _get(http, p["base_url"] + "/key", title, {"Authorization": f"Bearer {key}"})   # 一覧は鍵なしでも読めるので、鍵はここで確かめる
        data = _get(http, p["base_url"] + "/models", title, {"Authorization": f"Bearer {key}"})
        for m in data.get("data", []):
            arch, params, pr = m.get("architecture") or {}, m.get("supported_parameters") or [], m.get("pricing") or {}
            caps = (["json_schema"] if "structured_outputs" in params or "response_format" in params else []) \
                + (["vision"] if "image" in (arch.get("input_modalities") or []) else [])
            try:
                price = {"input_per_mtok": round(float(pr["prompt"]) * 1e6, 6),
                         "output_per_mtok": round(float(pr["completion"]) * 1e6, 6)}
            except (KeyError, TypeError, ValueError):
                price = None
            out.append({"model": m["id"], "title": m.get("name") or m["id"], "capabilities": caps, "price": price,
                        "context_tokens": int(m.get("context_length") or 0)})
    else:                                                    # OpenAI・Gemini（OpenAI 互換の一覧は名前だけ）
        data = _get(http, p["base_url"] + "/models", title, {"Authorization": f"Bearer {key}"})
        for m in data.get("data", []):
            name = str(m.get("id", "")).removeprefix("models/")
            if name:
                out.append({"model": name, "title": name, "capabilities": None, "price": PRICE_HINTS.get(name),
                            "context_tokens": 0})
    out = [m for m in out if not NOT_CHAT.search(m["model"])]
    out.sort(key=lambda m: m["model"])
    return provider, out


def clean_choice(m: dict) -> dict:
    """画面から受け取った 1 つのモデルの選択を確かめる（名前・能力・値段）。"""
    model = str(m.get("model", "")).strip()
    if not model:
        raise KeyProblem("モデル名がありません")
    caps = [c for c in (m.get("capabilities") or []) if c in CAPABILITIES]
    price = m.get("price")
    try:
        price = {k: float(v) for k, v in (price or {}).items() if k in ("input_per_mtok", "output_per_mtok", "cache_read_per_mtok")}
    except (TypeError, ValueError):
        raise KeyProblem(f"{model} の値段が数ではありません") from None
    if "input_per_mtok" not in price or "output_per_mtok" not in price or min(price.values()) < 0:
        raise KeyProblem(f"{model} の値段（入力・出力、100 万トークンあたりのドル）を入れてください。無料なら 0")
    return {"model": model, "title": str(m.get("title") or model)[:80], "capabilities": caps, "price": price,
            "context_tokens": int(m.get("context_tokens") or 0)}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")[:60]


class KeyStore:
    """画面から入れた鍵と、その鍵で使うモデル。file を渡すとそこに残す（立ち上げ直しても使える）。"""

    def __init__(self, file: Path | None = None) -> None:
        self.file = file
        self.lock = threading.Lock()
        self.entries: list[dict] = []
        if file and file.exists():
            try:
                self.entries = json.loads(file.read_text(encoding="utf-8")).get("keys", [])
            except (OSError, ValueError) as e:
                log.warning("鍵の記録を読めません（%s）", e)

    def _save(self) -> None:
        if not self.file:
            return
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"keys": self.entries}, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.file)

    def add(self, key: str, provider: str, models: list[dict], taken: set[str]) -> dict:
        """鍵を残し、モデルに窓口の id を付ける（taken = すでにある id。重なれば番号を足す）。"""
        with self.lock:
            used = set(taken) | {m["id"] for e in self.entries for m in e["models"]}
            out = []
            for m in models:
                base = f"{provider}-{_slug(m['model'])}"
                mid, n = base, 2
                while mid in used:
                    mid, n = f"{base}-{n}", n + 1
                used.add(mid)
                out.append({**m, "id": mid, "title": m["title"] if m["title"] != m["model"] else f"{PROVIDERS[provider]['title']} {m['model']}"})
            entry = {"id": f"{provider}-{secrets.token_hex(3)}", "provider": provider, "key": key.strip(),
                     "added": time.time(), "models": out}
            self.entries.append(entry)
            self._save()
            return entry

    def remove(self, entry_id: str) -> dict | None:
        with self.lock:
            for e in self.entries:
                if e["id"] == entry_id:
                    self.entries.remove(e)
                    self._save()
                    return e
        return None

    def public(self) -> list[dict]:
        """画面に返す形（鍵そのものは返さない）。"""
        with self.lock:
            return [{"id": e["id"], "provider": e["provider"], "provider_title": PROVIDERS[e["provider"]]["title"],
                     "key": mask(e["key"]), "added": e.get("added", 0),
                     "models": [{"id": m["id"], "title": m["title"], "model": m["model"]} for m in e["models"]]}
                    for e in self.entries if e.get("provider") in PROVIDERS]

    def specs(self, entry: dict | None = None) -> list[ModelSpec]:
        with self.lock:
            entries = [entry] if entry else list(self.entries)
        return [spec_for(e, m) for e in entries if e.get("provider") in PROVIDERS for m in e["models"]]


def spec_for(entry: dict, m: dict) -> ModelSpec:
    p = PROVIDERS[entry["provider"]]
    return ModelSpec(id=m["id"], title=m["title"], provider=p["provider"], base_url=p["base_url"], api_key=entry["key"],
                     model=m["model"], capabilities=list(m.get("capabilities") or []), max_concurrency=4,
                     context_tokens=int(m.get("context_tokens") or 0), price=dict(m.get("price") or {}), external=True,
                     # OpenAI の新しいモデルは temperature を受け付けない
                     drop_params=["temperature"] if entry["provider"] == "openai" else [])
