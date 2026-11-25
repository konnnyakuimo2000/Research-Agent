from unittest.mock import patch

from openai import APIConnectionError

from research_agent.agent.graph import LLM_OUTAGE, timed
from research_agent.agent.guard import elapsed, time_over
from research_agent.llm import LLM


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
         patch("research_agent.llm.time.sleep"):
        assert llm.chat_json("s", "u", {}) == {}
    assert llm.waited >= 10          # やり直し前の 10 秒の待ちを数えた（sleep は止めてある）


def test_timed_records_wait_and_reports_outage():
    from research_agent.llm import WAIT_METER

    def node(state):
        WAIT_METER.get().add(500)        # このノードの間に 500 秒待った（実際には待たない）
        return {"x": 1}

    # 時計: ノードの始まり 0 秒、待ちを記録した時 300 秒、ノードの終わり 600 秒
    with patch("research_agent.agent.graph.time.perf_counter", side_effect=[0.0, 300.0, 600.0]):
        out = timed("decide", node)({"failures": []})
    assert out["trace"] == [{"node": "decide", "seconds": 600.0, "llm_wait": 500.0}]
    assert out["failures"] == [LLM_OUTAGE]


def test_time_limit_excludes_llm_wait():
    # 2900 秒かかったが、うち 2860 秒は LLM サーバーにつながらず待っていた → まだ調べられる
    s = {"trace": [{"node": "decide", "seconds": 2900, "llm_wait": 2860}]}
    assert elapsed(s) == 40 and not time_over(s)
    assert time_over({"trace": [{"node": "read_pages", "seconds": 700}]})
