"""調査エージェントを CLI で実行する（従来の呼び方）。中身は research_agent.cli（research-agent コマンドと同じ）。

    uv run scripts/agent.py "マーキュリーについて調べて" --verbose
"""

import sys

from research_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
