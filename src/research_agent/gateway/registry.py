"""つなぐエージェントの一覧（config/agents.toml）と、契約の API を呼ぶ部品。

エージェントを足すときは、サービスを足して config/agents.toml に 1 項目足す。
接続先は環境変数（url_env）で替えられる（Docker の中と、この PC で動かすときで違うため）。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from research_agent.agentkit import protocol

log = logging.getLogger(__name__)

MANIFEST_TTL = 60.0
# エージェントは 15 秒ごとに ping を送る。その 4 倍待っても何も来なければ、つなぎ直す
TIMEOUT = httpx.Timeout(15, read=60)


@dataclass
class AgentEntry:
    id: str
    url: str
    key: str = ""
    openai_model: str = ""      # OpenAI 互換の入口（Open WebUI）に出すモデル名。空なら出さない
    background: bool = False    # Open WebUI の裏処理（タイトル生成など）を受け持つ


@dataclass
class AgentClient:
    entry: AgentEntry
    http: httpx.Client
    _manifest: dict | None = None
    _manifest_at: float = 0.0
    error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def id(self) -> str:
        return self.entry.id

    def _url(self, path: str) -> str:
        return self.entry.url.rstrip("/") + "/agent/v1" + path

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.entry.key}"} if self.entry.key else {}

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        r = self.http.request(method, self._url(path), json=body, headers=self._headers())
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise AgentError(r.status_code, str(detail))
        return r.json()

    def manifest(self, refresh: bool = False) -> dict | None:
        """名乗り。つながらなければ、前に受け取ったもの（なければ None）。"""
        with self._lock:
            if self._manifest and not refresh and time.time() - self._manifest_at < MANIFEST_TTL:
                return self._manifest
        try:
            m = self._call("GET", "/manifest")
            with self._lock:
                self._manifest, self._manifest_at, self.error = m, time.time(), ""
        except (httpx.HTTPError, AgentError) as e:
            self.error = f"{type(e).__name__}: {e}"
        return self._manifest

    def health(self) -> dict:
        return self._call("GET", "/health")

    def start(self, input: dict, settings: dict | None = None, models: dict | None = None) -> dict:
        return self._call("POST", "/runs", {"input": input, "settings": settings or {}, "models": models or {}})

    def answer(self, run_id: str, text: str) -> dict:
        return self._call("POST", f"/runs/{run_id}/input", {"kind": "answer", "text": text})

    def control(self, run_id: str, action: str) -> dict:
        return self._call("POST", f"/runs/{run_id}/control", {"action": action})

    def result(self, run_id: str) -> dict:
        return self._call("GET", f"/runs/{run_id}/result")

    def events(self, run_id: str, after: int = 0) -> Iterator[tuple[str, dict]]:
        """イベントの流れ。("message", 封筒) と、("end" | "rest" | "missing", 中身) を返す。"""
        with self.http.stream("GET", self._url(f"/runs/{run_id}/events"), params={"after": after},
                              headers=self._headers()) as r:
            if r.status_code >= 400:
                raise AgentError(r.status_code, r.read().decode(errors="ignore"))
            yield from protocol.read_sse(r.iter_lines())


class AgentError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status} {detail}")
        self.status, self.detail = status, detail


def load_entries(path: Path) -> list[AgentEntry]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = []
    for a in data.get("agents", []):
        url = os.getenv(a.get("url_env", ""), "") or a.get("url", "")
        key = os.getenv(a.get("key_env", ""), "")
        out.append(AgentEntry(a["id"], url, key, a.get("openai_model", ""), bool(a.get("background", False))))
    return out


class Registry:
    def __init__(self, clients: list[AgentClient]) -> None:
        self.clients = {c.id: c for c in clients}
        self.default = clients[0].id if clients else ""

    @classmethod
    def from_file(cls, path: Path, http: httpx.Client | None = None) -> "Registry":
        http = http or httpx.Client(trust_env=False, timeout=TIMEOUT)
        return cls([AgentClient(e, http) for e in load_entries(path)])

    def get(self, agent_id: str) -> AgentClient:
        client = self.clients.get(agent_id or self.default)
        if client is None:
            raise KeyError(agent_id)
        return client

    def by_openai_model(self, model: str) -> AgentClient | None:
        return next((c for c in self.clients.values() if c.entry.openai_model and c.entry.openai_model == model), None)

    def background(self) -> AgentClient | None:
        return next((c for c in self.clients.values() if c.entry.background), None)
