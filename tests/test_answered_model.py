"""応答したモデル: 窓口が調査ごと・役割ごとに、実際に答えたモデルを数え、画面がそれを出す。

前は画面に「役割に割り当てたモデル」しか出せなかった（割り当てと、実際に答えたモデルが同じとは限らない。
上流が別のモデル名で答える、役割の既定を窓口の設定で変えた、など）。上流は偽物（httpx.MockTransport）。
"""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from research_agent.llm import BUDGET_HEADER, ROLE_HEADER, RoleLLM
from research_agent.modelgw.app import create_app
from research_agent.modelgw.registry import ModelSpec
from tests.ui_harness import run_js


def spec(id_, model):
    return ModelSpec(id_, id_.upper(), "openai", f"http://{id_}.test/v1", "k", model, ["json_schema"], 4)


def upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body["messages"][0]["content"] == "fail":
        return httpx.Response(400, json={"error": {"message": "bad request"}})
    # 上流は、頼まれた名前ではなく、版の付いた名前で答える
    answered = body["model"] + "-2026-11"
    if not body.get("stream"):
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": answered,
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": "ok"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    chunk = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": answered,
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())


def gateway():
    app = create_app(specs=[spec("big", "qwen"), spec("small", "gemma")], default="big", key="",
                     roles={"report": "small"}, http_client=httpx.Client(transport=httpx.MockTransport(upstream)))
    return TestClient(app)


def ask(c, model, role=None, text="hi", stream=False, budget="r1"):
    headers = {BUDGET_HEADER: budget, **({ROLE_HEADER: role} if role else {})}
    return c.post("/v1/chat/completions", headers=headers,
                  json={"model": model, "messages": [{"role": "user", "content": text}], "stream": stream})


def answered(c, budget="r1"):
    return {(a["role"], a["model"]): a for a in c.get(f"/v1/usage/runs/{budget}").json()["answered"]}


def test_the_gateway_counts_who_answered_each_role():
    c = gateway()
    assert ask(c, "role:extract", "extract").status_code == 200        # 既定（big）
    assert ask(c, "role:extract", "extract").status_code == 200
    assert ask(c, "role:report", "report", stream=True).status_code == 200   # 役割の既定（small）・流しながら
    assert ask(c, "big", "vision").status_code == 200                  # 選び直し（id で頼む）は見出しの役割で数える
    assert ask(c, "role:decide").status_code == 200                    # 見出しがなくても role:<役割> から分かる
    a = answered(c)
    assert a[("extract", "big")]["calls"] == 2 and a[("extract", "big")]["upstream"] == ["qwen-2026-11"]
    assert a[("report", "small")]["calls"] == 1 and a[("report", "small")]["upstream"] == ["gemma-2026-11"]
    assert a[("vision", "big")]["calls"] == 1 and a[("decide", "big")]["calls"] == 1
    assert answered(c, "other") == {}                                  # 別の調査には混ざらない


def test_failed_calls_are_counted_apart():
    c = gateway()
    assert ask(c, "role:extract", "extract", text="fail").status_code == 400
    a = answered(c)[("extract", "big")]
    assert a["calls"] == 0 and a["failed"] == 1


def test_the_agent_names_its_role():
    r = RoleLLM(object(), "role:extract", "extract", budget="run-1")   # type: ignore[arg-type]
    assert r.headers == {BUDGET_HEADER: "run-1", ROLE_HEADER: "extract"}
    assert RoleLLM(object(), "m", "extract").headers is None          # 窓口を使わないときは見出しを付けない


def test_the_screen_shows_the_answering_models():
    out = run_js("""
      import { ctx } from 'UI/core/ctx.ts';
      import { answeredMeta } from 'UI/panels/models.js';
      ctx.AGENTS = { research: { model_roles: [{ role: "extract", title: "抽出" }, { role: "report", title: "報告" },
                                               { role: "decide", title: "判断" }] } };
      const a = answeredMeta([
        { role: "decide", model: "big", upstream: ["qwen-2026-11"], calls: 3, failed: 0 },
        { role: "extract", model: "big", upstream: ["qwen-2026-11"], calls: 5, failed: 1 },
        { role: "report", model: "small", upstream: ["gemma-2026-11"], calls: 1, failed: 0 }], "research");
      const one = answeredMeta([{ role: "extract", model: "big", upstream: [], calls: 2, failed: 0 }], "research");
      out({ a, one, none: answeredMeta([], "research") });
    """)
    assert out["a"]["text"] == "応答したモデル: 判断・抽出 big 8 回（失敗 1） / 報告 small 1 回"
    assert "上流の名前 qwen-2026-11" in out["a"]["title"] and "上流の名前 gemma-2026-11" in out["a"]["title"]
    assert out["one"]["text"] == "応答したモデル: big 2 回"            # 1 つのモデルだけなら役割は省く
    assert out["none"] is None                                         # 数えていない記録は割り当てのまま
