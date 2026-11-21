"""モデルの窓口（model-gateway）: どのモデルに送るか、やり直しと待った秒数、同時実行の上限、流しながらの応答。

上流のモデル（研究室の LLM の代わり）は httpx の模擬トランスポート。エージェント側の LLM は、窓口を
TestClient 越しに呼ぶ（本物のエージェントと同じ使い方）。
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import InternalServerError

from my_llm_app import llm as llm_module
from my_llm_app.config import Settings
from my_llm_app.llm import LLM
from my_llm_app.modelgw.app import create_app
from my_llm_app.modelgw.registry import ModelSpec

REAL_SLEEP = time.sleep     # やり直しの待ちを止める fixture は time.sleep ごと差し替えるので、本物を取っておく


def body(text="はい", usage=True):
    out = {"id": "x", "object": "chat.completion", "created": 0, "model": "qwen",
           "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}
    if usage:
        out["usage"] = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    return out


def sse(parts):
    chunks = [{"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "qwen",
               "choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]} for p in parts]
    return "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"


class Upstream:
    """研究室の LLM の代わり。届いた要求を覚え、replies の順に答える（尽きたら最後のものを繰り返す）。"""

    def __init__(self, *replies, delay=0.0):
        self.replies = list(replies) or [httpx.Response(200, json=body())]
        self.sent: list[dict] = []
        self.delay = delay
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.sent.append(json.loads(request.content))
            self.active += 1
            self.peak = max(self.peak, self.active)
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        REAL_SLEEP(self.delay)
        with self.lock:
            self.active -= 1
        return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)


def gateway(upstream, key="", max_concurrency=2):
    spec = ModelSpec("lab-qwen", "研究室 qwen", "openai", "http://llm.test/v1", "secret", "qwen3.8-27b",
                     ["json_schema", "vision"], max_concurrency, 32768,
                     {"chat_template_kwargs": {"enable_thinking": False}})
    app = create_app(specs=[spec], default="lab-qwen", key=key,
                     http_client=httpx.Client(transport=httpx.MockTransport(upstream)))
    return app, TestClient(app)


def agent_llm(client, model="default", key="k"):
    """エージェントの中の LLM（窓口を呼ぶ形）。"""
    s = Settings("http://testserver/v1", model, key, "", llm_timeout=30, llm_gateway=True)
    return LLM(s, http_client=client)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)


def test_requests_go_to_the_default_model_with_its_settings():
    up = Upstream()
    app, c = gateway(up)
    llm = agent_llm(c)
    llm.call([{"role": "user", "content": "u"}], response_format={"type": "json_schema", "json_schema": {"name": "o", "schema": {}}})
    sent = up.sent[-1]
    assert sent["model"] == "qwen3.8-27b"                                     # 上流のモデル名に置き換える
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}         # モデルごとの癖は窓口が付ける
    assert sent["response_format"]["type"] == "json_schema"
    for name in ("", "default", "lab-qwen", "qwen3.8-27b", "role:extract"):
        r = c.post("/v1/chat/completions", json={"model": name, "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200 and r.json()["x_model_id"] == "lab-qwen" and r.json()["x_model_wait"] == 0
    assert c.post("/v1/chat/completions", json={"model": "nope", "messages": [{"role": "user", "content": "x"}]}).status_code == 404


def test_agent_does_not_add_its_own_model_settings_or_limit():
    llm = agent_llm(httpx.Client())
    assert llm.extra_body == {} and llm.gateway


def test_waits_inside_the_gateway_are_counted_by_the_agent():
    up = Upstream(httpx.Response(503, json={"error": "busy"}), httpx.Response(503, json={"error": "busy"}),
                  httpx.Response(200, json=body("{}")))
    app, c = gateway(up)
    llm = agent_llm(c)
    assert llm.chat_json("s", "u", {}) == {}
    assert len(up.sent) == 3
    assert llm.waited >= 10 + 30                    # 窓口がやり直す前に待った秒数を、エージェントが受け取った


def test_after_the_gateway_gives_up_the_agent_does_not_retry_again():
    up = Upstream(httpx.Response(503, json={"error": "busy"}))
    app, c = gateway(up)
    llm = agent_llm(c)
    with pytest.raises(InternalServerError):
        llm.chat_text("s", "u")
    assert len(up.sent) == 4                        # 窓口の 4 回だけ（エージェントが重ねてやり直すと 16 回になる）
    assert llm.waited >= 10 + 30 + 60
    assert app.state.models["lab-qwen"].usage.as_dict()["errors"] == 1


def serve(app):
    """本物の HTTP サーバーで立てる（TestClient は要求を 1 つずつ処理するので、同時実行を確かめられない）。"""
    import socket

    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    end = time.time() + 10
    while not server.started and time.time() < end:
        time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}/v1"


def test_concurrency_limit_holds_across_agents():
    up = Upstream(delay=0.3)
    app, _ = gateway(up, max_concurrency=2)
    server, url = serve(app)
    s = Settings(url, "default", "k", "", llm_timeout=30, llm_gateway=True)
    agents = [LLM(s, http_client=httpx.Client(trust_env=False)) for _ in range(6)]   # 6 つのエージェントが同時に頼む
    threads = [threading.Thread(target=a.chat_text, args=("s", "u")) for a in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    server.should_exit = True
    assert len(up.sent) == 6 and up.peak == 2       # 上流には同時に 2 件まで（同時に来ても待たせる）
    u = app.state.models["lab-qwen"].usage.as_dict()
    assert u["calls"] == 6 and u["active"] == 0 and u["sending"] == 0 and u["peak_sending"] == 2
    assert u["prompt_tokens"] == 42


def test_streaming_passes_through_and_reports_wait_in_the_first_chunk():
    up = Upstream(httpx.Response(503, json={"error": "busy"}),
                  httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(["こん", "にちは"]).encode()))
    app, c = gateway(up)
    llm = agent_llm(c)
    assert "".join(llm.stream_messages([{"role": "user", "content": "x"}])) == "こんにちは"
    assert up.sent[-1]["stream"] is True and up.sent[-1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert llm.waited >= 10


def test_key_and_listing():
    up = Upstream()
    app, c = gateway(up, key="gw-key")
    assert c.get("/v1/models").status_code == 401
    listed = c.get("/v1/models", headers={"Authorization": "Bearer gw-key"}).json()
    m = listed["data"][0]
    assert listed["default"] == "lab-qwen" and m["capabilities"] == ["json_schema", "vision"] and m["default"]
    assert "secret" not in json.dumps(listed) and "llm.test" not in json.dumps(listed)   # 鍵と接続先は出さない
    assert agent_llm(c, key="gw-key").chat_text("s", "u") == "はい"
    with pytest.raises(Exception):
        agent_llm(c, key="wrong").chat_text("s", "u")


def test_models_toml_reads_connection_from_env(tmp_path, monkeypatch):
    from my_llm_app.modelgw.registry import ConfigError, load
    p = tmp_path / "models.toml"
    p.write_text('default = "a"\n[[models]]\nid = "a"\nbase_url_env = "X_URL"\napi_key_env = "X_KEY"\n'
                 'model_env = "X_MODEL"\nmodel = "fallback"\n', encoding="utf-8")
    monkeypatch.setenv("X_URL", "http://x/v1")
    monkeypatch.setenv("X_KEY", "kk")
    monkeypatch.delenv("X_MODEL", raising=False)
    specs, default = load(p)
    assert (specs[0].base_url, specs[0].api_key, specs[0].model, default) == ("http://x/v1", "kk", "fallback", "a")
    monkeypatch.delenv("X_URL")
    with pytest.raises(ConfigError):
        load(p)


def test_load_settings_prefers_the_gateway(monkeypatch):
    from my_llm_app.config import load_settings
    monkeypatch.setenv("MODEL_GATEWAY_URL", "http://model-gateway:8000/v1")
    monkeypatch.setenv("MODEL_GATEWAY_KEY", "gk")
    s = load_settings()
    assert s.llm_gateway and s.llm_base_url == "http://model-gateway:8000/v1" and s.llm_model == "default"
    assert s.llm_api_key == "gk" and s.llm_timeout > 1200
    monkeypatch.delenv("MODEL_GATEWAY_URL")
    assert not load_settings().llm_gateway
