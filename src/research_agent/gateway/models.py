"""モデルの窓口の一覧（ゲートウェイから見る）と、依頼ごとのモデルの選び直しの確かめ（6.3）。

- 一覧は窓口の /v1/models（30 秒覚えておく）。窓口の場所は MODEL_GATEWAY_URL（なければ選び直しはできない）
- 依頼ごとの選び直し（役割 → モデル）は、エージェントの名乗りの model_roles（役割と必要な能力）と、
  窓口のモデルの能力（capabilities）を照らして、受け付ける前に確かめる
- 記録には、選び直していない役割も含めて「実際に使う対応」を残す（あとで比べるため）
- 使えないモデル（Web API の鍵がないなど）は、選び直しにも役割の既定にも使えない
- 調査ごとの費用は、窓口の /v1/usage/runs/{エージェントの依頼の id} から読む
- Web API の鍵（画面の 設定 › API キー）は、窓口の /v1/keys にそのまま渡す。鍵はゲートウェイに残さない
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

import httpx

CACHE_SECONDS = 30.0


class ModelChoiceError(ValueError):
    """選べないモデル（ない・能力が足りない・ない役割）。"""


class GatewayReply(Exception):
    """窓口が断った・つながらない（status と detail を画面へ返す）。"""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass
class ModelCatalog:
    url: str
    key: str = ""
    http: httpx.Client | None = None
    _value: dict | None = None
    _at: float = 0.0
    error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_env(cls) -> "ModelCatalog | None":
        url = os.getenv("MODEL_GATEWAY_URL", "")
        return cls(url, os.getenv("MODEL_GATEWAY_KEY", ""), httpx.Client(trust_env=False, timeout=10)) if url else None

    def get(self, refresh: bool = False) -> dict | None:
        """{default, roles, data: [モデル]}。つながらなければ前に受け取ったもの（なければ None）。"""
        with self._lock:
            if self._value and not refresh and time.time() - self._at < CACHE_SECONDS:
                return self._value
        try:
            headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
            r = (self.http or httpx.Client(trust_env=False, timeout=10)).get(self.url.rstrip("/") + "/models",
                                                                              headers=headers)
            r.raise_for_status()
            with self._lock:
                self._value, self._at, self.error = r.json(), time.time(), ""
        except (httpx.HTTPError, ValueError) as e:
            self.error = f"{type(e).__name__}: {e}"
        return self._value

    def _read(self, path: str, root: bool = False) -> dict | None:
        """窓口から読む（つながらなければ None）。root は /v1 の外（/health）。"""
        base = self.url.rstrip("/")
        if root and base.endswith("/v1"):
            base = base[:-3]
        try:
            headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
            r = (self.http or httpx.Client(trust_env=False, timeout=10)).get(base + path, headers=headers)
            return r.json() if r.status_code < 400 else None
        except (httpx.HTTPError, ValueError):
            return None

    def keys(self, method: str, path: str = "", body: dict | None = None) -> dict:
        """窓口の /v1/keys を呼ぶ。鍵を変えたら、モデルの一覧を読み直す。"""
        try:
            headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
            r = (self.http or httpx.Client(trust_env=False, timeout=10)).request(
                method, self.url.rstrip("/") + "/keys" + path, headers=headers, json=body, timeout=40)
        except httpx.HTTPError as e:
            raise GatewayReply(503, f"モデルの窓口につながりません（{type(e).__name__}）") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            raise GatewayReply(r.status_code, str(data.get("detail") or f"モデルの窓口が断りました（{r.status_code}）"))
        if method != "GET" and not path.endswith("/check"):
            self.get(refresh=True)
        return data

    def call_log(self, agent_run: str) -> list[dict] | None:
        """1 つの調査の LLM とのやり取り（窓口の記録。なければ・つながらなければ None）。"""
        value = self._read(f"/logs/{agent_run}")
        return None if value is None else value.get("calls", [])

    def delete_call_log(self, agent_run: str) -> None:
        """依頼を削除したとき、窓口の記録も消す（つながらなければ何もしない）。"""
        try:
            headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
            (self.http or httpx.Client(trust_env=False, timeout=10)).delete(self.url.rstrip("/") + f"/logs/{agent_run}", headers=headers)
        except httpx.HTTPError:
            pass

    def run_usage(self, agent_run: str) -> dict | None:
        """1 つの調査の費用（つながらなければ None）。"""
        return self._read(f"/usage/runs/{agent_run}")

    def status(self) -> dict | None:
        """モデルごとの接続の状態と使用量、今日の費用と上限（画面のモデルの窓に出す）。つながらなければ None。"""
        usage = self._read("/usage")
        if usage is None:
            return None
        health = self._read("/health", root=True) or {}
        out = {}
        for model_id, u in usage.get("models", {}).items():
            h = health.get("models", {}).get(model_id, {})
            out[model_id] = {"ok": h.get("ok"), "error": h.get("error", ""), "seconds": h.get("seconds"),
                             **{k: u.get(k) for k in ("calls", "errors", "active", "prompt_tokens",
                                                      "completion_tokens", "cost_usd", "refused", "unmeasured")}}
        return {"models": out, "spend": usage.get("spend", {})}


def effective_models(manifest: dict, requested: dict | None, catalog: dict | None) -> dict[str, str]:
    """役割ごとに実際に使うモデル。選べないものがあれば ModelChoiceError。

    窓口の一覧がない（窓口を使わない構成）ときは、選び直しはできない（空の対応を返す）。
    """
    requested = {str(k): str(v) for k, v in (requested or {}).items() if v}
    roles = {r["role"]: r for r in manifest.get("model_roles", [])}
    unknown = set(requested) - set(roles)
    if unknown:
        raise ModelChoiceError(f"このエージェントにない役割です: {', '.join(sorted(unknown))}")
    if catalog is None:
        if requested:
            raise ModelChoiceError("モデルの窓口につながらないため、モデルを選び直せません")
        return {}
    models = {m["id"]: m for m in catalog.get("data", [])}
    out = {}
    for role, spec in roles.items():
        model_id = requested.get(role) or catalog.get("roles", {}).get(role) or catalog.get("default", "")
        model = models.get(model_id)
        if model is None:
            raise ModelChoiceError(f"{spec.get('title', role)}（{role}）のモデル {model_id} はありません")
        missing = set(spec.get("needs", [])) - set(model.get("capabilities", []))
        if missing:
            raise ModelChoiceError(f"{spec.get('title', role)}（{role}）には {', '.join(sorted(missing))} が要りますが、"
                                   f"{model.get('title', model_id)} は対応していません")
        if model.get("available") is False:
            raise ModelChoiceError(f"{spec.get('title', role)}（{role}）のモデル {model.get('title', model_id)} は"
                                   f"使えません（{model.get('unavailable', '')}）")
        out[role] = model_id
    return out
