"""Web API の鍵を画面から入れる（modelgw/keys.py）。前は Claude だけを config/models.toml に書き、鍵がないと
「ANTHROPIC_API_KEY が設定されていません」と出していた。

- 鍵の形から提供元を判別し（Anthropic・OpenAI・Google Gemini・OpenRouter）、その提供元のモデル一覧で確かめる
- 受け付けられた鍵だけを残し、選んだモデルを窓口に足す。鍵は応答に出さない（末尾 4 文字だけ）
- 窓口を立ち上げ直しても使える。外すとモデルも消える
提供元の API は偽物（httpx.MockTransport）。
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from research_agent.gateway.app import create_app as create_gateway
from research_agent.gateway.models import ModelCatalog
from research_agent.gateway.registry import Registry
from research_agent.modelgw.app import create_app
from research_agent.modelgw.keys import KeyProblem, detect, list_models
from research_agent.modelgw.registry import ModelSpec

KEYS = {"anthropic": "sk-ant-api03-TESTKEY-anthropic-1234", "openai": "sk-proj-TESTKEY-openai-5678",
        "gemini": "AIzaTESTKEY-gemini-9012", "openrouter": "sk-or-v1-TESTKEY-openrouter-3456"}


class Providers:
    """4 つの提供元の偽物。正しい鍵だけを受け付ける。会話の呼び出しも受ける。"""

    def __init__(self):
        self.chats = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, auth = str(request.url), request.headers.get("authorization", "")
        if url.startswith("https://api.anthropic.com/v1/models"):
            if request.headers.get("x-api-key") != KEYS["anthropic"]:
                return httpx.Response(401, json={"error": {"message": "invalid x-api-key"}})
            return httpx.Response(200, json={"data": [{"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"},
                                                      {"id": "claude-new-6", "display_name": "Claude New 6"}]})
        if url.startswith("https://api.openai.com/v1/models"):
            if auth != f"Bearer {KEYS['openai']}":
                return httpx.Response(401, json={"error": {"message": "Incorrect API key"}})
            return httpx.Response(200, json={"data": [{"id": "gpt-x"}, {"id": "text-embedding-3-large"}, {"id": "whisper-1"}]})
        if url.startswith("https://generativelanguage.googleapis.com/v1beta/openai/models"):
            if auth != f"Bearer {KEYS['gemini']}":
                return httpx.Response(400, json={"error": {"message": "API key not valid"}})
            return httpx.Response(200, json={"data": [{"id": "models/gemini-x-flash"}, {"id": "models/text-embedding-004"}]})
        if url == "https://openrouter.ai/api/v1/key":
            return httpx.Response(200 if auth == f"Bearer {KEYS['openrouter']}" else 401, json={"data": {}})
        if url == "https://openrouter.ai/api/v1/models":
            return httpx.Response(200, json={"data": [
                {"id": "vendor/model-a", "name": "Vendor: Model A", "context_length": 128000,
                 "architecture": {"input_modalities": ["text", "image"]}, "supported_parameters": ["structured_outputs"],
                 "pricing": {"prompt": "0.000002", "completion": "0.00001"}},
                {"id": "vendor/text-only", "name": "Vendor: Text", "architecture": {"input_modalities": ["text"]},
                 "supported_parameters": [], "pricing": {"prompt": "0", "completion": "0"}}]})
        if url.endswith("/chat/completions"):
            body = json.loads(request.content)
            self.chats.append((url, auth, body))
            return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                                             "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                                             "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}})
        return httpx.Response(404)


def http(up=None):
    return httpx.Client(transport=httpx.MockTransport(up or Providers()))


def lab():
    return ModelSpec("lab", "LAB", "openai", "http://lab.test/v1", "k", "qwen", ["json_schema", "vision"], 2)


def gateway(tmp_path, up=None):
    up = up or Providers()
    app = create_app(specs=[lab()], default="lab", key="", http_client=http(up), keys_file=tmp_path / ".secrets" / "keys.json")
    return TestClient(app), up


def test_the_provider_is_told_from_the_key():
    assert {p: detect(k) for p, k in KEYS.items()} == {p: p for p in KEYS}
    assert detect("hello") is None and detect("") is None


@pytest.mark.parametrize("provider", list(KEYS))
def test_each_provider_checks_the_key_with_its_model_list(provider):
    found_provider, models = list_models(KEYS[provider], http())
    assert found_provider == provider
    names = [m["model"] for m in models]
    assert not any("embed" in n or "whisper" in n for n in names)             # 会話に使えないモデルは外す
    if provider == "anthropic":
        opus = next(m for m in models if m["model"] == "claude-opus-5-5")
        assert opus["capabilities"] == ["json_schema", "vision"] and opus["price"]["input_per_mtok"] == 4.0
        assert next(m for m in models if m["model"] == "claude-new-6")["price"] is None   # 値段が分からないものは画面で入れる
    if provider == "gemini":
        assert names == ["gemini-x-flash"]                                     # 「models/」は外す
    if provider in ("openai", "gemini"):
        assert models[0]["capabilities"] is None                               # 提供元が教えない
    if provider == "openrouter":
        a = next(m for m in models if m["model"] == "vendor/model-a")
        assert a["capabilities"] == ["json_schema", "vision"] and a["price"] == {"input_per_mtok": 2.0, "output_per_mtok": 10.0}
        assert next(m for m in models if m["model"] == "vendor/text-only")["capabilities"] == []


@pytest.mark.parametrize("provider", list(KEYS))
def test_a_wrong_key_is_refused_with_the_reason(provider):
    wrong = KEYS[provider][:-4] + "XXXX"
    with pytest.raises(KeyProblem) as e:
        list_models(wrong, http())
    assert "受け付けませんでした" in str(e.value) or "読めませんでした" in str(e.value)
    with pytest.raises(KeyProblem, match="判別できません"):
        list_models("not-a-key", http())


def test_keys_are_added_used_listed_and_removed(tmp_path):
    c, up = gateway(tmp_path)
    r = c.post("/v1/keys/check", json={"key": KEYS["openai"]})
    assert r.status_code == 200 and r.json()["provider"] == "openai" and [m["model"] for m in r.json()["models"]] == ["gpt-x"]
    assert c.get("/v1/keys").json()["keys"] == []                              # 確かめるだけでは残さない
    # 値段がないと足さない（費用を数えられないため）
    r = c.post("/v1/keys", json={"key": KEYS["openai"], "models": [{"model": "gpt-x", "capabilities": ["json_schema"]}]})
    assert r.status_code == 400 and "値段" in r.json()["detail"]
    # この鍵の一覧にないモデルは足さない
    r = c.post("/v1/keys", json={"key": KEYS["openai"], "models": [{"model": "other", "price": {"input_per_mtok": 1, "output_per_mtok": 2}}]})
    assert r.status_code == 400 and "使えないモデル" in r.json()["detail"]
    r = c.post("/v1/keys", json={"key": KEYS["openai"], "models": [
        {"model": "gpt-x", "capabilities": ["json_schema", "vision", "bogus"], "price": {"input_per_mtok": 1.25, "output_per_mtok": 10}}]})
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["provider"] == "openai" and entry["key"] == "…5678" and entry["models"][0]["id"] == "openai-gpt-x"
    listed = {m["id"]: m for m in c.get("/v1/models").json()["data"]}
    gpt = listed["openai-gpt-x"]
    assert gpt["external"] is True and gpt["available"] is True and gpt["capabilities"] == ["json_schema", "vision"]
    assert gpt["price"] == {"input_per_mtok": 1.25, "output_per_mtok": 10.0} and gpt["title"] == "OpenAI gpt-x"
    # 足したモデルで呼べる（鍵はその提供元にだけ送る。temperature は OpenAI に送らない）
    r = c.post("/v1/chat/completions", json={"model": "openai-gpt-x", "temperature": 0.2, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["x_model_cost"] == pytest.approx((1000 * 1.25 + 100 * 10) / 1e6)
    url, auth, body = up.chats[-1]
    assert url == "https://api.openai.com/v1/chat/completions" and auth == f"Bearer {KEYS['openai']}"
    assert body["model"] == "gpt-x" and "temperature" not in body
    # 鍵そのものは、どの応答にも出さない
    for text in (c.get("/v1/keys").text, c.get("/v1/models").text, c.get("/health").text, c.get("/v1/usage").text):
        assert KEYS["openai"] not in text
    # 外すと、モデルも消える
    r = c.delete(f"/v1/keys/{entry['id']}")
    assert r.status_code == 200 and r.json()["removed"] == ["openai-gpt-x"]
    assert "openai-gpt-x" not in {m["id"] for m in c.get("/v1/models").json()["data"]}
    assert c.delete(f"/v1/keys/{entry['id']}").status_code == 404


def test_keys_survive_a_restart_and_claude_goes_through_the_claude_converter(tmp_path):
    c, _ = gateway(tmp_path)
    r = c.post("/v1/keys", json={"key": KEYS["anthropic"], "models": [
        {"model": "claude-opus-5-5", "title": "Claude Opus 5.5", "capabilities": ["json_schema", "vision"],
         "price": {"input_per_mtok": 4, "output_per_mtok": 20}}]})
    assert r.status_code == 200, r.text
    saved = (tmp_path / ".secrets" / "keys.json").read_text(encoding="utf-8")
    assert KEYS["anthropic"] in saved                                          # 窓口の置き場にだけ残る
    c2, _ = gateway(tmp_path)                                                  # 立ち上げ直し
    m = {x["id"]: x for x in c2.get("/v1/models").json()["data"]}["anthropic-claude-opus-5-5"]
    assert m["provider"] == "anthropic" and m["title"] == "Claude Opus 5.5" and m["available"] is True
    assert c2.app.state.models["anthropic-claude-opus-5-5"].llm.client is not None   # Claude の変換の部品を使う


def test_the_web_gateway_passes_keys_through_without_keeping_them(tmp_path):
    mgw, _ = gateway(tmp_path)
    catalog = ModelCatalog("http://testserver/v1", http=mgw)
    gw = TestClient(create_gateway(registry=Registry([]), runs_dir=tmp_path / "runs", api_key="", resume=False, catalog=catalog))
    r = gw.post("/api/models/keys/check", json={"key": KEYS["gemini"]})
    assert r.status_code == 200 and r.json()["provider_title"] == "Google Gemini"
    r = gw.post("/api/models/keys", json={"key": KEYS["gemini"], "models": [
        {"model": "gemini-x-flash", "capabilities": ["json_schema"], "price": {"input_per_mtok": 0.3, "output_per_mtok": 2.5}}]})
    assert r.status_code == 200, r.text
    assert "gemini-gemini-x-flash" in {m["id"] for m in gw.get("/api/models").json()["models"]}   # 一覧を読み直している
    assert gw.post("/api/models/keys/check", json={"key": "nope"}).json()["detail"].startswith("鍵の形から")
    assert gw.delete("/api/models/keys/" + r.json()["id"]).status_code == 200
    stored = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in (tmp_path / "runs").rglob("*") if p.is_file())
    assert KEYS["gemini"] not in stored                                        # ゲートウェイには残さない
