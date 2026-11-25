"""1 つの調査の、LLM とのやり取り（送った全文と応答）をテキストで保存する。

    uv run python scripts/llm_log.py <調査の ID> [-o 保存先.txt] [--url http://localhost:8100]

ID は画面の履歴の詳細、または GET /api/runs で分かる。記録はモデルの窓口が調査ごとに残したもの
（記録を始める前の調査にはない）。AGENT_API_KEY を設定していれば、その鍵で読む。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="調査の LLM とのやり取りをテキストで保存する")
    ap.add_argument("run_id")
    ap.add_argument("-o", "--out", help="保存先（なければ runs/<ID>-llm.txt）")
    ap.add_argument("--url", default=os.getenv("GATEWAY_URL", "http://localhost:8100"))
    args = ap.parse_args(argv)
    key = os.getenv("AGENT_API_KEY", "")
    r = httpx.get(f"{args.url.rstrip('/')}/api/runs/{args.run_id}/llm-log", timeout=60, trust_env=False,
                  headers={"Authorization": f"Bearer {key}"} if key else {})
    if r.status_code != 200:
        detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
        print(f"読めません（{r.status_code}）: {detail}", file=sys.stderr)
        return 1
    out = Path(args.out or Path("runs") / f"{args.run_id}-llm.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(r.text, encoding="utf-8")
    print(f"{out}（{len(r.text):,} 文字）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
