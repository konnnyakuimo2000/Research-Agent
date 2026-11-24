"""M3: Web API のモデル。Claude の変換の部品、費用の記録と上限、鍵のないモデル、調査ごとの費用の数え方。

Claude の API は呼ばない（Anthropic の SDK の HTTP を偽の応答に差し替える）。
"""

from __future__ import annotations

import json

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from research_agent import llm as llm_module
from research_agent.agents import research
from research_agent.config import Settings
from research_agent.gateway.models import ModelChoiceError, effective_models
from research_agent.llm import BUDGET_HEADER, LLM
from research_agent.modelgw.anthropic_provider import FALLBACK_BETA, strict_schema, to_anthropic
from research_agent.modelgw.app import create_app
from research_agent.modelgw.registry import ModelSpec, load_catalog
from research_agent.modelgw.spend import Spend


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)


def claude_spec(**kw):
    base = dict(id="claude", title="Claude", provider="anthropic", base_url="", api_key="k", model="claude-opus-5-5",
                capabilities=["json_schema", "vision"], max_concurrency=4, external=True,
                price={"input_per_mtok": 4.0, "output_per_mtok": 20.0})
    return ModelSpec(**{**base, **kw})


def lab_spec():
    return ModelSpec("lab", "LAB", "openai", "http://llm.test/v1", "k", "qwen", ["json_schema"], 2)


def message(text="こんにちは", inp=1000, out=500, read=0):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                      "cache_creation_input_tokens": 0}}


def events(parts, inp=100, out=20):
    evs = [("message_start", {"type": "message_start", "message": {
               "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": inp, "output_tokens": 1}}}),
           ("content_block_start", {"type": "content_block_start", "index": 0,
                                    "content_block": {"type": "text", "text": ""}})]
    evs += [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": p}}) for p in parts]
    evs += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": out}}),
            ("message_stop", {"type": "message_stop"})]
    return "".join(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" for name, data in evs)


class Claude:
    """Claude の API の代わり。届いた本文と見出しを覚え、replies の順に答える（尽きたら最後のもの）。"""

    def __init__(self, *replies):
        self.replies = list(replies) or [httpx2.Response(200, json=message())]
        self.sent: list[dict] = []
        self.paths: list[str] = []

    def __call__(self, request):
        self.sent.append(json.loads(request.content))
        self.paths.append(str(request.url))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def gateway(*replies, specs=None, limits=None, spend_file=None):
    up = Claude(*replies)
    lab = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": "qwen",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "lab"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}})))
    app = create_app(specs=specs or [claude_spec(), lab_spec()], default="claude", key="", http_client=lab,
                     limits=limits, spend_file=spend_file,
                     anthropic_http=httpx2.Client(transport=httpx2.MockTransport(up)))
    return TestClient(app), up


def ask(c, model="claude", budget=None, **extra):
    headers = {BUDGET_HEADER: budget} if budget else {}
    return c.post("/v1/chat/completions", headers=headers,
                  json={"model": model, "messages": [{"role": "system", "content": "短く"},
                                                     {"role": "user", "content": "x"}], **extra})


# ---- 変換 ----

def test_requests_are_converted_to_the_messages_api():
    msgs = [{"role": "system", "content": "あなたは調べる係"},
            {"role": "user", "content": [{"type": "text", "text": "この画像は？"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
            {"role": "assistant", "content": ""}]
    schema = {"type": "object", "properties": {"names": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
                                               "kind": {"type": "string", "enum": ["a", "b"]}}, "required": ["names"]}
    p = to_anthropic(msgs, {"temperature": 0.2, "response_format": {"type": "json_schema",
                                                                    "json_schema": {"name": "o", "schema": schema}},
                            "extra_body": {"chat_template_kwargs": {"x": 1}, "output_config": {"effort": "low"}}},
                     claude_spec(fallbacks="default"))
    assert p["system"] == "あなたは調べる係" and "temperature" not in p and p["max_tokens"] == 16000
    assert p["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "この画像は？"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]}]  # 空の発言は送らない
    fmt = p["output_config"]["format"]
    assert p["output_config"]["effort"] == "low" and fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False and "maxItems" not in fmt["schema"]["properties"]["names"]
    assert fmt["schema"]["properties"]["kind"]["enum"] == ["a", "b"]
    assert "extra_body" not in p                                          # ローカルのモデル向けの項目は送らない
    assert p["betas"] == [FALLBACK_BETA] and p["fallbacks"] == "default"


def test_strict_schema_closes_nested_objects():
    s = strict_schema({"type": "array", "minItems": 2, "items": {"type": "object", "properties": {
        "n": {"type": "integer", "minimum": 0}}}})
    assert "minItems" not in s and s["items"]["additionalProperties"] is False and "minimum" not in s["items"]["properties"]["n"]


# ---- 窓口を通して ----

def test_claude_answers_in_the_openai_shape_with_cost():
    c, up = gateway()
    r = ask(c)
    assert r.status_code == 200
    out = r.json()
    assert out["choices"][0]["message"]["content"] == "こんにちは"           # 思考の塊は除く
    assert out["usage"]["prompt_tokens"] == 1000 and out["usage"]["completion_tokens"] == 500
    assert out["x_model_cost"] == pytest.approx((1000 * 4 + 500 * 20) / 1e6)
    assert up.sent[0]["model"] == "claude-opus-5-5" and up.sent[0]["system"] == "短く"
    usage = c.get("/v1/usage").json()
    assert usage["models"]["claude"]["cost_usd"] == pytest.approx(0.014)
    assert usage["spend"]["today_usd"] == pytest.approx(0.014)
    assert usage["models"]["lab"]["cost_usd"] == 0


def test_cache_reads_are_priced_lower():
    c, _ = gateway(httpx2.Response(200, json=message(inp=100, out=0, read=10_000)))
    cost = ask(c).json()["x_model_cost"]
    assert cost == pytest.approx((100 * 4 + 10_000 * 0.4) / 1e6)          # 書いていなければ入力の 1 割


def test_claude_streams_through_the_gateway():
    c, _ = gateway(httpx2.Response(200, text=events(["こん", "にちは"]),
                                   headers={"content-type": "text/event-stream"}))
    with c.stream("POST", "/v1/chat/completions",
                  json={"model": "claude", "stream": True, "messages": [{"role": "user", "content": "x"}]}) as r:
        lines = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: {")]
    text = "".join(ch["choices"][0]["delta"].get("content") or "" for ch in lines if ch.get("choices"))
    assert text == "こんにちは"
    assert lines[-1]["usage"]["prompt_tokens"] == 100 and lines[-1]["usage"]["completion_tokens"] == 20
    assert c.get("/v1/usage").json()["models"]["claude"]["cost_usd"] == pytest.approx((100 * 4 + 20 * 20) / 1e6)


def test_overloaded_claude_is_retried_by_the_gateway_and_then_reported():
    busy = httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    c, up = gateway(busy, httpx2.Response(200, json=message("やっと")))
    assert ask(c).json()["choices"][0]["message"]["content"] == "やっと"   # 529 はやり直す
    assert len(up.sent) == 2
    c2, up2 = gateway(httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                              "message": "bad"}}))
    r = ask(c2)
    assert r.status_code == 400 and r.json()["error"]["gateway"] is True and len(up2.sent) == 1   # 400 はやり直さない


def test_tools_are_refused_rather_than_silently_dropped():
    c, up = gateway()
    r = ask(c, tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}])
    assert r.status_code == 400 and not up.sent


def test_models_without_a_key_are_listed_but_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("X_URL", "http://x/v1")
    p = tmp_path / "models.toml"
    p.write_text('[[models]]\nid = "lab"\nbase_url_env = "X_URL"\nmodel = "m"\n'
                 '[[models]]\nid = "claude"\nprovider = "anthropic"\napi_key_env = "ANTHROPIC_API_KEY"\n'
                 'model = "claude-opus-5-5"\n', encoding="utf-8")
    spec = {s.id: s for s in load_catalog(p).specs}["claude"]
    assert spec.unavailable and spec.external and not spec.public()["available"]
    c, up = gateway(specs=[spec, lab_spec()])
    r = ask(c)
    assert r.status_code == 503 and r.json()["error"]["gateway"] is True and not up.sent
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert not {s.id: s for s in load_catalog(p).specs}["claude"].unavailable


def test_the_shipped_models_toml_lists_web_api_models(monkeypatch):
    from research_agent.config import REPO_ROOT
    monkeypatch.setenv("VL_BASE_URL", "http://x/v1")
    catalog = load_catalog(REPO_ROOT / "config" / "models.toml")
    claude = [s for s in catalog.specs if s.provider == "anthropic"]
    assert claude and all(s.external and s.price for s in claude)
    assert catalog.limits.get("daily_usd") and catalog.limits.get("run_usd")


# ---- 費用の上限 ----

def test_run_limit_stops_only_that_run_and_spares_unpriced_models():
    c, up = gateway(limits={"run_usd": 0.02})
    assert ask(c, budget="run-a").status_code == 200                      # 0.014
    assert ask(c, budget="run-a").status_code == 200                      # 0.028（送る前は 0.014 なので通る）
    over = ask(c, budget="run-a")
    assert over.status_code == 402 and over.json()["error"]["gateway"] is True
    assert "この調査" in over.json()["error"]["message"] and len(up.sent) == 2
    assert ask(c, budget="run-b").status_code == 200                      # ほかの調査は止めない
    assert ask(c, model="lab", budget="run-a").status_code == 200        # 値段のないモデルは止めない
    run = c.get("/v1/usage/runs/run-a").json()
    assert run["usd"] == pytest.approx(0.028) and run["models"] == {"claude": pytest.approx(0.028)}
    assert c.get("/v1/usage").json()["models"]["claude"]["refused"] == 1


def test_daily_limit_survives_a_restart(tmp_path):
    f = tmp_path / "spend.json"
    c, _ = gateway(limits={"daily_usd": 0.01}, spend_file=f)
    assert ask(c).status_code == 200
    c2, up2 = gateway(limits={"daily_usd": 0.01}, spend_file=f)           # 立ち上げ直しても今日の分を覚えている
    r = ask(c2)
    assert r.status_code == 402 and "今日" in r.json()["error"]["message"] and not up2.sent


def test_spend_forgets_yesterday(tmp_path, monkeypatch):
    s = Spend({"daily_usd": 1.0})
    s.add("claude", "r", 2.0)
    assert s.refusal("r")
    monkeypatch.setattr("research_agent.modelgw.spend.today", lambda: "2999-01-01")
    assert s.refusal("r") == "" and s.as_dict()["today_usd"] == 0


# ---- エージェントとゲートウェイ ----

def test_agents_send_the_run_id_so_the_gateway_can_count_per_run():
    seen = []

    def handler(request):
        seen.append(request.headers.get(BUDGET_HEADER))
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": "{}"}}]})
    llm = LLM(Settings("http://g/v1", "default", "k", "", llm_gateway=True),
              http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    llm.for_role("extract", None, "run-1").chat_json("s", "u", {"type": "object"})
    llm.for_role("report").chat_text("s", "u")
    assert seen == ["run-1", None]
    direct = LLM(Settings("http://llm/v1", "qwen", "k", ""))
    assert direct.for_role("extract", None, "run-1").headers is None   # 窓口を使わないときは送らない


def test_unavailable_models_cannot_be_chosen():
    catalog = {"default": "lab", "roles": {}, "data": [
        {"id": "lab", "capabilities": ["json_schema", "vision"]},
        {"id": "claude", "title": "Claude", "capabilities": ["json_schema", "vision"], "available": False,
         "unavailable": "ANTHROPIC_API_KEY が設定されていません"}]}
    with pytest.raises(ModelChoiceError, match="使えません"):
        effective_models(research.MANIFEST, {"report": "claude"}, catalog)
    assert effective_models(research.MANIFEST, {}, catalog)["report"] == "lab"


# ---- 画面（M4）: モデルの一覧・状態と使用量 ----

def test_the_screen_gets_models_with_status_usage_and_spend(tmp_path):
    from research_agent.gateway.app import create_app as gateway_app
    from research_agent.gateway.models import ModelCatalog
    from research_agent.gateway.registry import Registry

    no_key = claude_spec(id="claude-off", unavailable="ANTHROPIC_API_KEY が設定されていません")
    mg, _ = gateway(specs=[claude_spec(), no_key, lab_spec()], limits={"daily_usd": 5.0})
    ask(mg, budget="r1")
    web = TestClient(gateway_app(registry=Registry([]), runs_dir=tmp_path, api_key="",
                                 catalog=ModelCatalog("http://testserver/v1", http=mg)))
    listed = {m["id"]: m for m in web.get("/api/models").json()["models"]}
    assert listed["claude"]["external"] and listed["claude"]["available"] and listed["claude"]["price"]["input_per_mtok"] == 4.0
    assert listed["claude-off"]["available"] is False and "ANTHROPIC_API_KEY" in listed["claude-off"]["unavailable"]
    assert listed["lab"]["external"] is False
    st = web.get("/api/models/status").json()
    assert st["available"] and st["models"]["claude"]["calls"] == 1
    assert st["models"]["claude"]["cost_usd"] == pytest.approx(0.014)
    assert st["models"]["claude-off"]["ok"] is False                       # 鍵のないモデルは、つながらないと出す
    assert st["spend"]["today_usd"] == pytest.approx(0.014) and st["spend"]["limits"]["daily_usd"] == 5.0


def test_model_status_without_a_model_gateway(tmp_path):
    from research_agent.gateway.app import create_app as gateway_app
    from research_agent.gateway.registry import Registry
    web = TestClient(gateway_app(registry=Registry([]), runs_dir=tmp_path, api_key="", catalog=None))
    assert web.get("/api/models/status").json() == {"available": False, "models": {}, "spend": {}}
