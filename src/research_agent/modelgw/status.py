"""エージェントの health に出す「モデルにつながるか」。

窓口を使うときは窓口の /health を見る（既定のモデルの状態）。使わないときは LLM サーバーの /models を直接見る。
"""

from __future__ import annotations

import time

from research_agent.config import Settings


def model_check(settings: Settings) -> tuple[str, dict]:
    """(画面に出すモデル名, 状態の欄)。"""
    from research_agent.tools import http as http_tools

    started = time.time()
    if getattr(settings, "llm_gateway", False):
        root = settings.llm_base_url.rstrip("/").removesuffix("/v1")
        try:
            h = http_tools.get(root + "/health", timeout=8, cache=False, public=False).json()
            m = h["models"][h["default"]]
            check = {"ok": bool(m.get("ok")), **({"seconds": m["seconds"]} if "seconds" in m else {}),
                     **({"error": m["error"]} if "error" in m else {})}
            return m.get("model", ""), {"title": "既定モデルの一覧 API", "hint": "VPN を確認", **check}
        except Exception as e:  # noqa: BLE001 - つながらない理由を画面に出す
            return "", {"title": "モデルの窓口", "hint": "モデルの窓口につながらない", "ok": False, "error": type(e).__name__}
    try:
        r = http_tools.get(settings.llm_base_url.rstrip("/") + "/models", timeout=4, cache=False, public=False)
        check = {"ok": r.status_code == 200, "seconds": round(time.time() - started, 1)}
    except Exception as e:  # noqa: BLE001
        check = {"ok": False, "error": type(e).__name__}
    return settings.llm_model, {"title": "既定モデルの一覧 API", "hint": "VPN を確認", **check}
