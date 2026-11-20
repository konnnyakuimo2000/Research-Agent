"""テレメトリーのイベント（docs/telemetry_design.md 6 章）。

ノードの中から「作業の始まり・途中・終わり」をイベントとして送る。送り先は LangGraph の custom stream
（`graph.stream(..., stream_mode=["updates", "custom"])` で受け取れる）。受け取った側（サーバー・CLI）が
通し番号と時刻を付けて画面に流し、`runs/<実行の ID>.events.jsonl` に書く（Recorder）。

- グラフの外（テスト・ノードを直接呼ぶとき）では何も送らない
- `ThreadPoolExecutor` のスレッドの中では get_stream_writer() が使えない（実行の文脈が引き継がれない）ので、
  ノードの最初に emitter() で取り出し、スレッドで動く関数には引数で渡す
- イベントにページ本文は入れない（大きいため）。事実・引用・判定の理由は入れる
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from my_llm_app.tiers import page_tier

log = logging.getLogger(__name__)


class Emitter:
    """イベントを送る。writer がなければ何もしない。

    LangGraph の writer は、呼ばれたときに実行の文脈（contextvars）を読む。スレッドの中ではその文脈がないので、
    作ったときの文脈を写し取っておき、送るたびにその写しの中で呼ぶ（スレッドから送っても届くように）。
    """

    def __init__(self, writer: Callable[[dict], None] | None = None) -> None:
        self._writer = writer
        self._context = contextvars.copy_context()

    def __bool__(self) -> bool:
        return self._writer is not None

    def __call__(self, type_: str, **data) -> None:
        if self._writer is None:
            return
        try:
            self._context.copy().run(self._writer, {"type": type_, "wall": time.time(), **data})
        except Exception:  # noqa: BLE001 - 画面への送信の失敗で調査を止めない
            log.debug("telemetry event %s was not sent", type_, exc_info=True)


def emitter() -> Emitter:
    """今のノードの送り先。グラフの外なら何もしない Emitter。"""
    try:
        from langgraph.config import get_stream_writer
        return Emitter(get_stream_writer())
    except Exception:  # noqa: BLE001 - グラフの外（テスト・CLI でノードを直接呼んだとき）
        return Emitter()


def host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def page_info(url: str, state: dict | None = None) -> dict:
    """ページを表すイベントの共通の中身。"""
    return {"url": url, "host": host(url), "tier": page_tier(url, state)}


class Recorder:
    """受け取ったイベントに通し番号と時刻（調査の開始からの秒数）を付け、jsonl に書く。

    画面に流す側（サーバー）は events を読む。新しいイベントは condition で知らせる。
    """

    def __init__(self, run_id: str, path: Path | None = None) -> None:
        self.run_id = run_id
        self.path = path
        self.events: list[dict] = []
        self.started = time.time()
        self.condition = threading.Condition()
        self.finished = False
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, event: dict) -> dict:
        with self.condition:
            wall = event.pop("wall", time.time())
            e = {"seq": len(self.events) + 1, "t": round(wall - self.started, 2), **event}
            self.events.append(e)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
            self.condition.notify_all()
            return e

    def finish(self) -> None:
        with self.condition:
            self.finished = True
            self.condition.notify_all()

    def wait_after(self, seq: int, timeout: float) -> list[dict]:
        """seq より後のイベント。なければ timeout 秒まで待つ。"""
        with self.condition:
            if len(self.events) <= seq and not self.finished:
                self.condition.wait(timeout)
            return self.events[seq:]
