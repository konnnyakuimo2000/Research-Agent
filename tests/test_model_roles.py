"""M2: 役割ごとのモデル。窓口の [roles]・能力の確かめ、エージェントの役割での頼み方、依頼ごとの選び直し。

最後のテストは三層をすべて同じプロセスの中に立て、1 つの調査の中で役割ごとに別のモデルに送られることを確かめる。
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from research_agent.agent.nodes import Nodes
from research_agent.agents import research
from research_agent.config import Settings
from research_agent.gateway.app import create_app as gateway_app
from research_agent.gateway.models import ModelCatalog, ModelChoiceError, effective_models
from research_agent.gateway.registry import AgentClient, AgentEntry, Registry
from research_agent.llm import LLM
from research_agent.modelgw.app import create_app as modelgw_app
from research_agent.modelgw.registry import ConfigError, ModelSpec, load_catalog

ROLES = {"decide": "big", "extract": "big", "vision": "big", "report": "big"}


def spec(id_, upstream, caps):
    return ModelSpec(id_, id_.upper(), "openai", "http://llm.test/v1", "k", upstream, caps, 2, 0, {})


class Upstream:
    def __init__(self):
        self.sent = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.sent.append(body)
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": body["model"]}}]})


def model_gateway(roles=None):
    up = Upstream()
    app = modelgw_app(specs=[spec("big", "big-upstream", ["json_schema", "vision"]),
                             spec("small", "small-upstream", ["json_schema"])],
                      default="big", key="", roles=roles if roles is not None else {**ROLES, "report": "small"},
                      http_client=httpx.Client(transport=httpx.MockTransport(up)))
    return app, TestClient(app), up


def ask(c, model, **extra):
    return c.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "x"}], **extra})


# ---- 窓口: 役割と能力 ----

def test_roles_resolve_to_their_models_and_are_listed():
    app, c, up = model_gateway()
    assert ask(c, "role:report").json()["x_model_id"] == "small"
    assert ask(c, "role:extract").json()["x_model_id"] == "big"
    assert ask(c, "role:unknown").json()["x_model_id"] == "big"           # 書いていない役割は既定
    assert [b["model"] for b in up.sent] == ["small-upstream", "big-upstream", "big-upstream"]
    listed = c.get("/v1/models").json()
    assert listed["roles"]["report"] == "small" and listed["default"] == "big"


def test_requests_needing_missing_capabilities_are_refused_before_sending():
    app, c, up = model_gateway()
    image = [{"role": "user", "content": [{"type": "text", "text": "x"},
                                          {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
    r = c.post("/v1/chat/completions", json={"model": "small", "messages": image})
    assert r.status_code == 400 and "vision" in r.json()["detail"]
    assert c.post("/v1/chat/completions", json={"model": "big", "messages": image}).status_code == 200
    schema = {"type": "json_schema", "json_schema": {"name": "o", "schema": {}}}
    app2 = modelgw_app(specs=[spec("plain", "p", [])], default="plain", key="",
                       http_client=httpx.Client(transport=httpx.MockTransport(Upstream())))
    assert ask(TestClient(app2), "plain", response_format=schema).status_code == 400
    assert len(up.sent) == 1                                              # 断ったものは上流に送らない


def test_roles_must_point_to_listed_models(tmp_path, monkeypatch):
    monkeypatch.setenv("X_URL", "http://x/v1")
    p = tmp_path / "models.toml"
    p.write_text('[[models]]\nid = "a"\nbase_url_env = "X_URL"\nmodel = "m"\n[roles]\nreport = "a"\n', encoding="utf-8")
    assert load_catalog(p).model_for_role("report") == "a" and load_catalog(p).model_for_role("x") == "a"
    p.write_text('[[models]]\nid = "a"\nbase_url_env = "X_URL"\nmodel = "m"\n[roles]\nreport = "zzz"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_catalog(p)


def test_the_shipped_models_toml_is_valid(monkeypatch):
    from research_agent.config import REPO_ROOT
    monkeypatch.setenv("VL_BASE_URL", "http://x/v1")
    catalog = load_catalog(REPO_ROOT / "config" / "models.toml")
    roles = {r["role"] for r in research.MANIFEST["model_roles"]} | {"chat"}
    assert roles <= set(catalog.roles)                                    # どの役割にもモデルが決まっている


# ---- エージェント: 役割で頼む ----

def test_llm_for_role_names_the_model_to_ask_for():
    gw = LLM(Settings("http://g/v1", "default", "k", "", llm_gateway=True))
    assert gw.for_role("extract").model == "role:extract"
    assert gw.for_role("extract", {"extract": "small"}).model == "small"
    assert gw.for_role("report", {"extract": "small"}).model == "role:report"
    direct = LLM(Settings("http://llm/v1", "qwen", "k", ""))
    assert direct.for_role("extract").model == "qwen"                     # 窓口を使わないときは設定のモデル


def test_nodes_use_the_run_s_choice_and_fall_back_for_fake_llms():
    llm = LLM(Settings("http://g/v1", "default", "k", "", llm_gateway=True))
    n = Nodes(Settings("http://g/v1", "default", "k", ""), llm)
    assert n._llm("vision", {"models": {"vision": "big"}}).model == "big"
    assert n._llm("decide", {}).model == "role:decide"
    fake = SimpleNamespace(chat_json=lambda *a, **k: {})
    assert Nodes(Settings("http://g/v1", "m", "k", ""), fake)._llm("decide", {}) is fake


# ---- ゲートウェイ: 選び直しの確かめ ----

CATALOG = {"default": "big", "roles": ROLES,
           "data": [{"id": "big", "title": "BIG", "capabilities": ["json_schema", "vision"]},
                    {"id": "small", "title": "SMALL", "capabilities": ["json_schema"]}]}


def test_effective_models_fill_defaults_and_check_needs():
    m = research.MANIFEST
    assert effective_models(m, {}, CATALOG) == ROLES
    assert effective_models(m, {"report": "small"}, CATALOG)["report"] == "small"
    with pytest.raises(ModelChoiceError, match="vision"):
        effective_models(m, {"vision": "small"}, CATALOG)                 # 画像を読めないモデルは画像の確認に選べない
    with pytest.raises(ModelChoiceError, match="ない役割"):
        effective_models(m, {"cooking": "big"}, CATALOG)
    with pytest.raises(ModelChoiceError, match="ありません"):
        effective_models(m, {"report": "nope"}, CATALOG)
    assert effective_models(m, {}, None) == {}                            # 窓口を使わない構成: 選び直さないなら通す
    with pytest.raises(ModelChoiceError, match="窓口"):
        effective_models(m, {"report": "big"}, None)


# ---- 三層をつないで: 1 つの調査の中で、役割ごとに別のモデルへ ----

class S(TypedDict, total=False):
    request: str
    models: dict
    answer: str


def roles_graph(llm: LLM):
    """読み取り（extract）と報告（report）で 1 回ずつモデルを呼ぶ、いちばん小さい調査。"""
    def read(state: S) -> dict:
        llm.for_role("extract", state.get("models")).chat_text("s", "読む")
        return {}

    def respond(state: S) -> dict:
        return {"answer": llm.for_role("report", state.get("models")).chat_text("s", "書く")}

    g = StateGraph(S)
    g.add_node("read_pages", read)
    g.add_node("respond", respond)
    g.add_edge(START, "read_pages")
    g.add_edge("read_pages", "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=InMemorySaver())


def test_one_research_run_uses_a_different_model_per_role(tmp_path):
    mg_app, mg, up = model_gateway(roles=ROLES)                           # 既定はすべて big
    agent_llm = LLM(Settings("http://testserver/v1", "default", "k", "", llm_timeout=30, llm_gateway=True),
                    http_client=mg)
    agent = research.create_app(graph=roles_graph(agent_llm), settings=Settings("http://x/v1", "m", "k", "http://s"),
                                key="")
    registry = Registry([AgentClient(AgentEntry("research", "http://testserver", ""), TestClient(agent))])
    web = TestClient(gateway_app(registry=registry, runs_dir=tmp_path, api_key="",
                                 catalog=ModelCatalog("http://testserver/v1", http=mg)))

    assert {m["id"] for m in web.get("/api/models").json()["models"]} == {"big", "small"}
    bad = web.post("/api/runs", json={"request": "x", "models": {"vision": "small"}})
    assert bad.status_code == 400 and "vision" in bad.json()["detail"]   # 能力が足りないモデルは、始める前に断る

    run = web.post("/api/runs", json={"request": "水星", "models": {"report": "small"}}).json()
    assert run["models"] == {**ROLES, "report": "small"}                  # 実際に使う対応を記録する
    end = time.time() + 5
    while web.get(f"/api/runs/{run['id']}/result").status_code != 200 and time.time() < end:   # 終わって結果を保存するまで
        time.sleep(0.02)
    assert [b["model"] for b in up.sent] == ["big-upstream", "small-upstream"]   # 読み取りは big、報告は small
    assert web.get(f"/api/runs/{run['id']}/result").json()["report"] == "small-upstream"
    assert web.get("/api/runs").json()["runs"][0]["models"]["report"] == "small"
