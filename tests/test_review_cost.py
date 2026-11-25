"""再レビュー（.claude/review-fixes.md）の 4: 有料のストリームの費用を記録し、送る直前にも予算を当てる。

- OpenAI 互換の上流は、stream_options.include_usage を頼んだときだけ最後に使用量を返す。頼んでいなかったので、
  有料のチャットを流しても費用が 0 ドルだった。使用量が返らないときは「未計測」として、0 ドルと区別する
- 予算の判定は送る前に 1 回だけで、費用は送信枠を返した後に足していたので、同じ調査から同時に来た呼び出しが
  すべて判定を通った（同時送信 1・予算 1 回分・3 件同時 → 3 件とも送られた）
上流は偽物（httpx.MockTransport）で再現する。
"""

from __future__ import annotations

import json
import threading
import time

import httpx
from fastapi.testclient import TestClient

from research_agent.modelgw.app import create_app
from research_agent.modelgw.registry import ModelSpec

PRICE = {"input_per_mtok": 4.0, "output_per_mtok": 20.0}
COST = 1000 * 4.0 / 1e6 + 500 * 20.0 / 1e6                   # 1 回の費用（0.014 ドル）


def paid(concurrency=4):
    return ModelSpec("paid", "Paid", "openai", "http://paid.test/v1", "k", "gpt-x", ["json_schema"], concurrency,
                     price=PRICE, external=True)


def lab():
    return ModelSpec("lab", "LAB", "openai", "http://lab.test/v1", "k", "qwen", ["json_schema"], 4)


class Upstream:
    """OpenAI 互換の偽の上流。usage=True なら、頼まれたときだけストリームの最後に使用量を返す。"""

    def __init__(self, usage=True, delay=0.0):
        self.usage, self.delay, self.requests, self.lock = usage, delay, [], threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        with self.lock:
            self.requests.append(body)
        time.sleep(self.delay)
        used = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
        if not body.get("stream"):
            return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                                             "choices": [{"index": 0, "finish_reason": "stop",
                                                          "message": {"role": "assistant", "content": "ok"}}],
                                             "usage": used})
        chunk = lambda delta, finish=None, usage=None: {  # noqa: E731
            "id": "x", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if delta is not None else [],
            **({"usage": usage} if usage else {})}
        parts = [chunk({"role": "assistant", "content": "こん"}), chunk({"content": "にちは"}, "stop")]
        if self.usage and (body.get("stream_options") or {}).get("include_usage"):
            parts.append(chunk(None, usage=used))
        text = "".join(f"data: {json.dumps(p, ensure_ascii=False)}\n\n" for p in parts) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text.encode())


def gateway(up: Upstream, specs, limits=None):
    app = create_app(specs=specs, default=specs[0].id, key="", limits=limits,
                     http_client=httpx.Client(transport=httpx.MockTransport(up)))
    return TestClient(app)


def stream(c, model="paid", budget="r1"):
    r = c.post("/v1/chat/completions", headers={"X-Model-Budget": budget},
               json={"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 200, r.text
    return r.text


def test_paid_streams_are_charged():
    up = Upstream()
    c = gateway(up, [paid()])
    stream(c)
    stream(c)
    assert all(b["stream_options"]["include_usage"] for b in up.requests)
    usage = c.get("/v1/usage").json()
    assert usage["spend"]["today_usd"] == round(2 * COST, 6) and usage["models"]["paid"]["cost_usd"] == round(2 * COST, 6)
    assert c.get("/v1/usage/runs/r1").json()["usd"] == round(2 * COST, 6)
    assert usage["models"]["paid"]["unmeasured"] == 0


def test_streams_without_usage_are_unmeasured_not_free():
    c = gateway(Upstream(usage=False), [paid()])
    stream(c)
    usage = c.get("/v1/usage").json()
    assert usage["spend"]["today_usd"] == 0 and usage["models"]["paid"]["unmeasured"] == 1
    assert usage["spend"]["unmeasured_today"] == 1 and c.get("/v1/usage/runs/r1").json()["unmeasured"] == 1


def test_free_models_are_not_asked_for_usage_and_stay_free():
    up = Upstream()
    c = gateway(up, [lab()])
    stream(c, model="lab")
    assert "stream_options" not in up.requests[0]
    usage = c.get("/v1/usage").json()
    assert usage["spend"]["today_usd"] == 0 and usage["models"]["lab"]["unmeasured"] == 0


def test_the_run_budget_holds_against_parallel_calls():
    up = Upstream(delay=0.3)
    c = gateway(up, [paid(concurrency=1)], limits={"run_usd": COST})
    codes = []

    def call():
        r = c.post("/v1/chat/completions", headers={"X-Model-Budget": "r1"},
                   json={"model": "paid", "messages": [{"role": "user", "content": "hi"}]})
        codes.append(r.status_code)

    threads = [threading.Thread(target=call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(up.requests) == 1                                      # 上流へ送ったのは 1 件だけ
    assert sorted(codes) == [200, 402, 402]
    assert c.get("/v1/usage/runs/r1").json()["usd"] == round(COST, 6)


def test_free_models_still_run_in_parallel():
    up = Upstream(delay=0.3)
    c = gateway(up, [lab()])
    started = time.time()
    threads = [threading.Thread(target=lambda: c.post("/v1/chat/completions", headers={"X-Model-Budget": "r1"},
                                                      json={"model": "lab", "messages": [{"role": "user", "content": "hi"}]}))
               for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(up.requests) == 3 and time.time() - started < 0.85      # 1 件ずつなら 0.9 秒かかる
