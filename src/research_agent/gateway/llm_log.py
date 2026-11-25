"""1 つの調査の、LLM とのやり取りの記録をテキストにする（モデルの窓口の /v1/logs/{id} の中身から）。

呼び出しごとに、時刻・役割・モデル・秒数・トークン・送った全文（system・user など）・出力の形・応答の全文を、
送った順に並べる。読むためのもので、機械で読み直す形ではない（それは窓口の JSONL）。
"""

from __future__ import annotations

import json
import time

ROLE_TITLES = {"decide": "判断", "extract": "ページの読み取り", "vision": "画像の確認", "report": "報告", "chat": "チャット"}
LINE = "=" * 78


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(ts)) if ts else ""   # 時間帯も書く（Docker の中は UTC）


def render(rec: dict, calls: list[dict], role_titles: dict | None = None) -> str:
    titles = {**ROLE_TITLES, **(role_titles or {})}
    tin = sum((c.get("usage") or {}).get("prompt_tokens") or 0 for c in calls)
    tout = sum((c.get("usage") or {}).get("completion_tokens") or 0 for c in calls)
    failed = sum(1 for c in calls if c.get("error"))
    by_role: dict[str, int] = {}
    for c in calls:
        by_role[c.get("role") or "-"] = by_role.get(c.get("role") or "-", 0) + 1
    head = [
        "LLM とのやり取りの記録",
        f"依頼: {rec.get('request', '')}",
        f"調査の ID: {rec.get('id', '')}（エージェント {rec.get('agent', '')} の依頼 {rec.get('agent_run', '')}）",
        f"始めた時刻: {_when(rec.get('created') or 0)} · 状態: {rec.get('status', '')}",
        f"呼び出し: {len(calls)} 件（失敗 {failed} 件） · "
        + " / ".join(f"{titles.get(r, r)} {n}" for r, n in by_role.items()),
        f"トークン: 入力 {tin:,} · 出力 {tout:,}（使用量が返った呼び出しの合計）",
        "画像は中身の代わりに「[画像 種類 大きさ]」と書いている。",
    ]
    out = ["\n".join(head)]
    for i, c in enumerate(calls, 1):
        u = c.get("usage") or {}
        tokens = f"入力 {u.get('prompt_tokens')} · 出力 {u.get('completion_tokens')} トークン" if u else "トークン不明"
        role = c.get("role") or "-"
        parts = [LINE,
                 f"#{i}  {_when(c.get('at') or 0)}  役割: {titles.get(role, role)}（{role}）",
                 f"モデル: {c.get('model', '')}（上流 {c.get('upstream', '')}） · {c.get('seconds', '?')} 秒 · {tokens}"
                 + (" · 流しながら" if c.get("stream") else "")]
        if c.get("params"):
            parts.append("設定: " + ", ".join(f"{k}={v}" for k, v in c["params"].items()))
        fmt = c.get("format")
        if fmt:
            parts.append(f"出力の形: {fmt.get('type')}" + (f"（{fmt.get('name')}）" if fmt.get("name") else ""))
            if fmt.get("schema") is not None:
                parts.append(json.dumps(fmt["schema"], ensure_ascii=False))
        for m in c.get("messages") or []:
            parts += [f"---- 送った: {m.get('role', '')}", m.get("content", "")]
        if c.get("error"):
            parts += ["---- 失敗", c["error"]]
        if c.get("response") is not None and (c.get("response") or not c.get("error")):
            parts += ["---- 応答", c.get("response") or "（空）"]
        out.append("\n".join(parts))
    if not calls:
        out.append("（呼び出しはありません）")
    return "\n\n".join(out) + "\n"
