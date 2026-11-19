from unittest.mock import patch

from openai import APIConnectionError

from my_llm_app.agent.graph import LLM_OUTAGE, timed
from my_llm_app.agent.guard import elapsed, time_over
from my_llm_app.llm import LLM


class Settings:
    llm_model, llm_base_url, llm_api_key, llm_timeout = "m", "http://localhost:1/v1", "k", 1


def test_connection_wait_is_counted():
    llm = LLM(Settings())
    calls = {"n": 0}

    def create(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise APIConnectionError(request=None)
        class R:
            usage = None
            choices = [type("C", (), {"message": type("M", (), {"content": "{}"})()})()]
        return R()

    with patch.object(llm.client.chat.completions, "create", side_effect=create), \
         patch("my_llm_app.llm.time.sleep"):
        assert llm.chat_json("s", "u", {}) == {}
    assert llm.waited >= 10          # やり直し前の 10 秒の待ちを数えた（sleep は止めてある）


def test_timed_records_wait_and_reports_outage():
    class FakeLLM:
        waited = 0.0

    llm = FakeLLM()

    def node(state):
        llm.waited += 500                # このノードの間に 500 秒待った（実際には待たない）
        return {"x": 1}

    with patch("my_llm_app.agent.graph.time.perf_counter", side_effect=[0.0, 600.0]):
        out = timed("decide", node, llm)({"failures": []})
    assert out["trace"] == [{"node": "decide", "seconds": 600.0, "llm_wait": 500.0}]
    assert out["failures"] == [LLM_OUTAGE]


def test_time_limit_excludes_llm_wait():
    # 2900 秒かかったが、うち 2860 秒は LLM サーバーにつながらず待っていた → まだ調べられる
    s = {"trace": [{"node": "decide", "seconds": 2900, "llm_wait": 2860}]}
    assert elapsed(s) == 40 and not time_over(s)
    assert time_over({"trace": [{"node": "read_pages", "seconds": 700}]})
