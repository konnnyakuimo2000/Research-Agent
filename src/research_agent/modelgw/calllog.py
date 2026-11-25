"""LLM とのやり取りの記録（調査ごと）。窓口に届いた呼び出しの、送った全文と応答を残す。

- 調査の id（X-Model-Budget）がある呼び出しだけを、<dir>/<調査の id>.jsonl に 1 呼び出し 1 行で書く
- 残すもの: 時刻・役割・モデル（窓口の id と上流の名前）・秒数・トークン・送ったメッセージの全文・出力の形
  （JSON スキーマ）・温度など・応答の全文（流しながらの応答はつないだもの）・失敗の理由
- 画像（data URL）は大きいので、中身を「[画像 image/jpeg 34 KB]」に置き換える。鍵や接続先は残さない
- 読んだページの本文も入るので、runs/ の中（git の外）に置く。依頼を削除したら、この記録も消す
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)
SAFE = re.compile(r"^[A-Za-z0-9-]{1,80}$")
PARAMS = ("temperature", "top_p", "max_tokens", "max_completion_tokens", "seed", "stop")


def _image(url: str) -> str:
    m = re.match(r"data:([^;,]+)?(;base64)?,(.*)$", url or "", re.S)
    if not m:
        return f"[画像 {url[:200]}]"
    size = len(m.group(3)) * (3 / 4 if m.group(2) else 1)
    return f"[画像 {m.group(1) or ''} {size / 1024:.0f} KB]".replace("  ", " ")


def plain_messages(messages: list) -> list[dict]:
    """送ったメッセージを、文字だけの形にする（画像は大きさの印に）。"""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append(str(p.get("text", "")))
                elif p.get("type") == "image_url":
                    parts.append(_image((p.get("image_url") or {}).get("url", "")))
                else:
                    parts.append(f"[{p.get('type', '?')}]")
            content = "\n".join(parts)
        out.append({"role": str(m.get("role", "")), "content": "" if content is None else str(content)})
    return out


def output_format(body: dict) -> dict | None:
    rf = body.get("response_format")
    if not isinstance(rf, dict):
        return None
    if rf.get("type") == "json_schema":
        js = rf.get("json_schema") or {}
        return {"type": "json_schema", "name": js.get("name", ""), "schema": js.get("schema")}
    return {"type": rf.get("type", "")}


class CallLog:
    def __init__(self, dir_: Path | None) -> None:
        self.dir = dir_
        self.lock = threading.Lock()

    def path(self, budget: str) -> Path | None:
        if not self.dir or not budget or not SAFE.match(budget):
            return None
        return self.dir / f"{budget}.jsonl"

    def add(self, budget: str | None, entry: dict) -> None:
        p = self.path(budget or "")
        if p is None:
            return
        try:
            with self.lock:
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"at": time.time(), **entry}, ensure_ascii=False, default=str) + "\n")
        except OSError as e:                     # 記録できなくても、呼び出しは止めない
            log.warning("LLM の記録を書けません: %s", e)

    def read(self, budget: str) -> list[dict] | None:
        p = self.path(budget)
        if p is None or not p.exists():
            return None
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def delete(self, budget: str) -> bool:
        p = self.path(budget)
        if p is None or not p.exists():
            return False
        p.unlink()
        return True
