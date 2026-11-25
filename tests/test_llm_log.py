"""調査ごとの LLM とのやり取りの記録（modelgw/calllog.py・gateway/llm_log.py）。

- 窓口に届いた呼び出しの、送った全文と応答を、調査の id ごとに残す（流しながらの応答はつなぐ・失敗も残す）
- 画像は大きさの印に置き換える。調査の id のない呼び出し・おかしな id は残さない
- ゲートウェイがテキストにして返す（/api/runs/{id}/llm-log）。依頼を削除したら記録も消す
"""

from __future__ import annotations

import base64
import json

import httpx
from fastapi.testclient import TestClient

from research_agent.gateway.app import create_app as create_gateway
from research_agent.gateway.models import ModelCatalog
from research_agent.gateway.registry import Registry
from research_agent.llm import BUDGET_HEADER, ROLE_HEADER
from research_agent.modelgw.app import create_app
from research_agent.modelgw.registry import ModelSpec

SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}}}
IMAGE = "data:image/jpeg;base64," + base64.b64encode(b"x" * 3072).decode()


def upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body["messages"][-1]["content"] == "fail":
        return httpx.Response(400, json={"error": {"message": "bad request"}})
    if not body.get("stream"):
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "qwen-2026",
                                         "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"action": "search"}'}}],
                                         "usage": {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128}})
    chunks = [{"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "qwen-2026",
               "choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}]} for t in ("東京タワーは", "333 m です。")]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content="".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n")


def model_gateway(tmp_path):
    spec = ModelSpec("lab", "LAB", "openai", "http://lab.test/v1", "secret-upstream-key", "qwen", ["json_schema", "vision"], 2)
    app = create_app(specs=[spec], default="lab", key="", http_client=httpx.Client(transport=httpx.MockTransport(upstream)),
                     log_dir=tmp_path / "llm_logs")
    return TestClient(app)


def ask(c, content, budget="run-1", role="decide", **extra):
    headers = {**({BUDGET_HEADER: budget} if budget else {}), **({ROLE_HEADER: role} if role else {})}
    return c.post("/v1/chat/completions", headers=headers, json={"model": "role:" + (role or "decide"), "messages": [
        {"role": "system", "content": "あなたは調査の判断をする。"}, {"role": "user", "content": content}], **extra})


def test_every_call_of_a_run_is_kept_in_full(tmp_path):
    c = model_gateway(tmp_path)
    assert ask(c, "次の行動を選んで", temperature=0.2,
               response_format={"type": "json_schema", "json_schema": {"name": "decision", "schema": SCHEMA}}).status_code == 200
    assert ask(c, [{"type": "text", "text": "この画像は東京タワー？"}, {"type": "image_url", "image_url": {"url": IMAGE}}],
               role="vision").status_code == 200
    assert ask(c, "報告を書いて", role="report", stream=True).status_code == 200
    assert ask(c, "fail", role="extract").status_code == 400
    assert ask(c, "調査の外", budget=None).status_code == 200                # 調査の id がない呼び出しは残さない
    assert ask(c, "おかしな id", budget="../etc").status_code == 200
    calls = c.get("/v1/logs/run-1").json()["calls"]
    assert [x["role"] for x in calls] == ["decide", "vision", "report", "extract"]
    d = calls[0]
    assert d["messages"] == [{"role": "system", "content": "あなたは調査の判断をする。"}, {"role": "user", "content": "次の行動を選んで"}]
    assert d["response"] == '{"action": "search"}' and d["usage"] == {"prompt_tokens": 120, "completion_tokens": 8}
    assert d["format"] == {"type": "json_schema", "name": "decision", "schema": SCHEMA} and d["params"] == {"temperature": 0.2}
    assert d["model"] == "lab" and d["upstream"] == "qwen-2026"
    assert calls[1]["messages"][1]["content"] == "この画像は東京タワー？\n[画像 image/jpeg 3 KB]"   # 画像の中身は残さない
    assert calls[2]["response"] == "東京タワーは333 m です。" and calls[2]["stream"] is True
    assert "400" in calls[3]["error"] and calls[3]["response"] is None
    raw = (tmp_path / "llm_logs" / "run-1.jsonl").read_text(encoding="utf-8")
    assert "secret-upstream-key" not in raw and IMAGE[30:80] not in raw
    assert [p.name for p in (tmp_path / "llm_logs").iterdir()] == ["run-1.jsonl"]
    assert c.get("/v1/logs/other").status_code == 404
    assert c.delete("/v1/logs/run-1").json()["deleted"] is True and c.get("/v1/logs/run-1").status_code == 404


def test_the_gateway_returns_the_log_as_text_and_deletes_it_with_the_run(tmp_path):
    mgw = model_gateway(tmp_path)
    ask(mgw, "次の行動を選んで", budget="agent-run-9",
        response_format={"type": "json_schema", "json_schema": {"name": "decision", "schema": SCHEMA}})
    ask(mgw, "報告を書いて", budget="agent-run-9", role="report", stream=True)
    gw = TestClient(create_gateway(registry=Registry([]), runs_dir=tmp_path / "runs", api_key="", resume=False,
                                   catalog=ModelCatalog("http://testserver/v1", http=mgw)))
    store = gw.app.state.gateway.store
    store.create("r1", "research", "1", "agent-run-9", "東京タワーの高さ")
    store.update("r1", status="done")
    r = gw.get("/api/runs/r1/llm-log")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert text.startswith("LLM とのやり取りの記録\n依頼: 東京タワーの高さ")
    assert "呼び出し: 2 件（失敗 0 件） · 判断 1 / 報告 1" in text and "トークン: 入力 120 · 出力 8" in text
    assert "#1 " in text and "役割: 判断（decide）" in text and "出力の形: json_schema（decision）" in text
    assert "---- 送った: system\nあなたは調査の判断をする。" in text and "---- 応答\n{\"action\": \"search\"}" in text
    assert "#2 " in text and "---- 応答\n東京タワーは333 m です。" in text
    assert gw.get("/api/runs/r1/llm-log?download=1").headers["content-disposition"] == 'attachment; filename="r1-llm.txt"'
    store.create("r0", "research", "1", "agent-old", "古い依頼")
    assert "記録はありません" in gw.get("/api/runs/r0/llm-log").json()["detail"]
    assert gw.get("/api/runs/nope/llm-log").status_code == 404
    assert gw.delete("/api/runs/r1").status_code == 200                       # 依頼を消すと、窓口の記録も消える
    assert mgw.get("/v1/logs/agent-run-9").status_code == 404
