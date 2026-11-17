"""テストの前に、エージェントが使う環境が動いているかを確かめる。

使い方:
    uv run scripts/check_env.py

確かめること:
    1. Docker のエンジンと、コンテナ（open-webui, searxng）が動いているか
    2. SearXNG が検索結果を返すか（応答しない検索エンジンも表示する）
    3. 研究室の LLM サーバーにモデルがあり、短い応答を返すか
    4. Wikidata の API に接続できるか

すべて OK なら終了コード 0、どれかが NG なら 1 を返す。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable

import httpx

from my_llm_app.config import USER_AGENT, load_settings
from my_llm_app.llm import LLM

CONTAINERS = ("open-webui", "searxng")


def check_docker() -> str:
    try:
        out = subprocess.run(["docker", "compose", "ps", "--format", "json"], capture_output=True, text=True,
                             timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"docker コマンドを実行できない（{type(e).__name__}）") from e
    if out.returncode != 0:
        raise RuntimeError("Docker のエンジンに接続できない（Docker Desktop が止まっている可能性）")
    lines = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    running = {c["Service"]: c["State"] for c in lines}
    stopped = [name for name in CONTAINERS if running.get(name) != "running"]
    if stopped:
        raise RuntimeError(f"止まっているコンテナ: {', '.join(stopped)}（docker compose up -d で起動）")
    return ", ".join(f"{k}={v}" for k, v in running.items())


def check_searxng(base_url: str) -> str:
    r = httpx.get(f"{base_url}/search", params={"q": "水星", "format": "json"},
                  headers={"User-Agent": USER_AGENT}, timeout=60)
    r.raise_for_status()
    data = r.json()
    n = len(data.get("results", []))
    if n == 0:
        raise RuntimeError(f"検索結果が 0 件（応答しないエンジン: {data.get('unresponsive_engines')}）")
    bad = [e[0] if isinstance(e, list) else e for e in data.get("unresponsive_engines", [])]
    return f"{n} 件" + (f"（応答しないエンジン: {', '.join(bad)}）" if bad else "")


def check_llm() -> str:
    settings = load_settings()
    r = httpx.get(f"{settings.llm_base_url}/models", timeout=15)
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    if settings.llm_model not in ids:
        raise RuntimeError(f"モデル {settings.llm_model} がない（あるもの: {ids}）")
    started = time.perf_counter()
    text = LLM(settings).chat_text("短く答える。", "「OK」とだけ返して", temperature=0)
    return f"{settings.llm_model} が応答（{time.perf_counter() - started:.1f} 秒）: {text.strip()[:20]}"


def check_wikidata() -> str:
    r = httpx.get("https://www.wikidata.org/w/api.php",
                  params={"action": "wbsearchentities", "search": "水星", "language": "ja", "format": "json"},
                  headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return f"{len(r.json().get('search', []))} 件"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = load_settings()
    checks: list[tuple[str, Callable[[], str]]] = [
        ("Docker", check_docker),
        ("SearXNG", lambda: check_searxng(settings.searxng_url)),
        ("LLM サーバー", check_llm),
        ("Wikidata", check_wikidata),
    ]
    ok = True
    for name, fn in checks:
        try:
            print(f"[OK] {name}: {fn()}")
        except Exception as e:  # noqa: BLE001 - どんな失敗も NG として表示する
            ok = False
            print(f"[NG] {name}: {e}")
    print("\n環境は使える状態です" if ok else "\n使えないものがあります。直してからテストしてください")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
