"""イベントの形（docs/telemetry_enhancement_plan.md 4.3）。

エージェントの中では、今までどおり平らなイベント（{"seq", "t", "type", ...中身}）を作る。契約の API で外に出すときに、
次の封筒の形にする。ゲートウェイと画面は、この形だけを知っている。

    {"v": 1, "seq": 42, "t": 61.3, "run_id": "…", "task_id": "", "type": "research/v1:page.done", "data": {...}}

- 0 段・1 段のイベント（CORE_TYPES）は、名前をそのまま使う
- それ以外は、エージェントが名乗った拡張（例: research/v1）の名前を前に付ける
- 封筒の形になる前の記録（今までの runs/*.events.jsonl）も、同じ関数で読み替える
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator

VERSION = 1

LEVEL0_TYPES = {"run.start", "run.end", "run.error", "run.cancelled", "message.delta", "status"}
LEVEL1_TYPES = {"task.created", "task.start", "task.end", "step.start", "step.end", "question", "answer",
                "run.waiting", "run.paused", "run.resumed", "artifact"}
CORE_TYPES = LEVEL0_TYPES | LEVEL1_TYPES
# 終わりの状態になるイベント
TERMINAL_TYPES = {"run.end", "run.error", "run.cancelled"}

_EXT_TYPE = re.compile(r"^([a-z0-9-]+/v\d+):(.+)$")


def to_v1(event: dict, run_id: str, extension: str = "") -> dict:
    """平らなイベントを封筒の形にする。すでに封筒の形なら、run_id だけ合わせて返す。"""
    if event.get("v"):
        return {**event, "run_id": run_id}
    data = {k: v for k, v in event.items() if k not in ("seq", "t", "type", "task_id", "wall")}
    type_ = str(event.get("type", ""))
    if type_ == "run.end" and "report" not in data:
        data["report"] = data.pop("answer", "")           # 今までの記録は「answer」に報告を入れていた
    if type_ not in CORE_TYPES and extension and not _EXT_TYPE.match(type_):
        type_ = f"{extension}:{type_}"
    return {"v": VERSION, "seq": event.get("seq", 0), "t": event.get("t", 0), "run_id": run_id,
            "task_id": event.get("task_id", ""), "type": type_, "data": data}


def split_type(type_: str) -> tuple[str, str]:
    """('research/v1', 'page.done') のように、拡張の名前と、その中の名前に分ける。共通のイベントは拡張が空。"""
    m = _EXT_TYPE.match(type_)
    return (m.group(1), m.group(2)) if m else ("", type_)


def flat(env: dict) -> dict:
    """封筒の形を、拡張の中の名前の平らなイベントに戻す（ゲートウェイの中で、拡張の部品が使う）。"""
    return {"seq": env.get("seq", 0), "t": env.get("t", 0), "type": split_type(env.get("type", ""))[1],
            **(env.get("data") or {})}


def sse(env: dict) -> str:
    return f"id: {env['seq']}\ndata: {json.dumps(env, ensure_ascii=False, default=str)}\n\n"


def sse_signal(name: str, data: dict | None = None) -> str:
    """イベント以外の知らせ（end: 終わった / rest: 答えを待って休んでいる / missing: ない）。"""
    return f"event: {name}\ndata: {json.dumps(data or {}, ensure_ascii=False)}\n\n"


def read_sse(lines: Iterable[str]) -> Iterator[tuple[str, dict]]:
    """SSE の行を (種類, 中身) にする。種類は、ふつうのイベントなら "message"。

    生存の合図（": ping" などの注釈の行）は ("ping", {}) にする（受け手が、接続が生きているかを数えるため）。
    """
    name, data = "message", []
    for line in lines:
        if line == "":
            if data:
                try:
                    yield name, json.loads("\n".join(data))
                except json.JSONDecodeError:
                    pass
            name, data = "message", []
        elif line.startswith(":"):
            yield "ping", {}
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        try:
            yield name, json.loads("\n".join(data))
        except json.JSONDecodeError:
            pass
