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


def _chrome() -> bool:
    import os
    paths = [r"C:\Program Files\Google\Chrome\Application\chrome.exe", "/usr/bin/google-chrome",
             "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    return bool(os.getenv("PW_BUNDLED")) or any(Path(p).exists() for p in paths)


def test_webui_end_to_end():
    """画面を通しで動かす（Playwright。webui/e2e/）。/api の返事はテストの中で決めるので、Docker も LLM も要らない。"""
    npm = shutil.which("npm")
    if not npm or not (WEBUI / "node_modules" / "@playwright").exists():
        pytest.skip("Node（npm）か webui/node_modules がない（cd webui; npm ci）")
    if not _chrome():
        pytest.skip("Chrome がない（npx playwright install chromium の後、PW_BUNDLED=1 で動かす）")
    r = subprocess.run([npm, "run", "e2e", "--silent"], cwd=WEBUI, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, f"Playwright が失敗:\n{r.stdout[-4000:]}\n{r.stderr[-2000:]}"
