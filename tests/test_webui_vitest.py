"""画面の部品のテスト（Vitest。webui/src/**/*.test.ts）を pytest からも回す。

Svelte の部品は Node だけでは描けないので、jsdom で描いて確かめるテストは Vitest に書く。
`pytest` 1 回で画面も確かめられるように、ここから `npm test` を呼ぶ。Node か webui/node_modules がなければ飛ばす
（`cd webui; npm ci` で入る）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parents[1] / "webui"


def test_webui_component_tests_pass():
    npm = shutil.which("npm")
    if not npm:
        pytest.skip("Node（npm）がない")
    if not (WEBUI / "node_modules" / ".bin").exists():
        pytest.skip("webui/node_modules がない（cd webui; npm ci）")
    r = subprocess.run([npm, "test", "--silent"], cwd=WEBUI, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, f"Vitest が失敗:\n{r.stdout[-4000:]}\n{r.stderr[-2000:]}"
