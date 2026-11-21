"""チャットのエージェント（契約の 0 段だけ）。OpenAI 互換のモデルを包む。

    uv run uvicorn --factory my_llm_app.agents.chat:create_app --port 8102

- 依頼の文（または input.messages の会話の履歴）をモデルに送り、返ってきた文を message.delta で流す
- 作業・質問・一時停止はない（0 段）。止める（cancel）だけはできる
- 調査エージェントと同じゲートウェイ・同じ画面に載ることを確かめるための、いちばん小さいエージェント
  （汎用性の合格の条件。docs/telemetry_enhancement_plan.md 4.0）。Open WebUI のタイトル生成などの裏処理にも使う
"""

from __future__ import annotations

import time

from my_llm_app.agentkit.runs import Cancelled, Done, Run, RunManager
from my_llm_app.agentkit.service import agent_app

MANIFEST = {
    "id": "chat", "name": "チャット", "version": "1.0.0", "protocol": "1",
    "description": "モデルとそのまま話す（調べない）",
    "levels": [0],
    "extensions": [],
    "capabilities": ["cancel"],
    "model_roles": [{"role": "chat", "title": "会話", "needs": []}],
}


class ChatRuns(RunManager):
    def __init__(self, llm) -> None:
        super().__init__()
        self.llm = llm

    def work(self, run: Run, payload: tuple[str, object]) -> Done | Cancelled:
        messages = run.input.get("messages") or [{"role": "user", "content": run.request}]
        parts = []
        for delta in self.llm.stream_messages(messages):
            if run.cancelled:
                return Cancelled()
            parts.append(delta)
            run.emit("message.delta", text=delta)
        return Done("".join(parts))


_status: dict = {}


def health(settings) -> dict:
    if _status and time.time() - _status["at"] < 20:
        return _status["value"]
    from my_llm_app.modelgw.status import model_check
    model, llm = model_check(settings)
    value = {"model": model, "checks": {"llm": llm}}
    _status.update(at=time.time(), value=value)
    return value


def create_app(llm=None, settings=None, key: str | None = None):
    from my_llm_app.config import load_settings

    settings = settings or load_settings()
    if llm is None:
        from my_llm_app.llm import LLM
        llm = LLM(settings)
    return agent_app(MANIFEST, ChatRuns(llm), lambda: health(settings), key=key)
