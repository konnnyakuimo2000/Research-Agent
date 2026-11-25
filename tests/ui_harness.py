"""画面の JavaScript（ES モジュール）を Node で動かす道具。ブラウザの部品（document など）は最小限の偽物にする。

使い方: run_js("import { x } from 'UI/core/util.js'; out(x(1));") → out() に渡した値（JSON）。
Node がなければ、そのテストは飛ばす。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "src" / "research_agent" / "ui"

# どんな要素を求められても返す偽物（属性・メソッドの呼び出しを受け流す）
_FAKE_DOM = r"""
const handler = {
  get(t, p) {
    if (p === Symbol.toPrimitive) return () => "";
    if (p in t) return t[p];
    if (p === "classList") return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
    if (p === "style") return t.style ||= {};
    if (p === "dataset") return t.dataset ||= {};
    if (["clientWidth", "clientHeight", "offsetWidth", "offsetHeight", "scrollHeight"].includes(p)) return 800;
    if (["hidden"].includes(p)) return false;
    if (p === "then") return undefined;
    return (...a) => fake();
  },
  set(t, p, v) { t[p] = v; return true; },
};
function fake() { return new Proxy({}, handler); }
globalThis.document = fake();
globalThis.window = fake();
globalThis.location = { search: "" };
globalThis.requestAnimationFrame = () => 0;
globalThis.CSS = { escape: (s) => s };
globalThis.out = (v) => console.log("__OUT__" + JSON.stringify(v));
"""


def run_js(body: str) -> object:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node がない")
    base = UI.as_uri() + "/"
    # 偽物は --import で先に読み込む（静的な import は、本文のコードより先に実行されるため）
    with tempfile.TemporaryDirectory() as d:
        setup = Path(d) / "fake_dom.mjs"
        setup.write_text(_FAKE_DOM, encoding="utf-8")
        r = subprocess.run([node, "--import", setup.as_uri(), "--input-type=module", "-e",
                            body.replace("'UI/", f"'{base}")],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("__OUT__")]
    assert r.returncode == 0 and lines, f"Node の実行に失敗: {r.stderr[-2000:]}"
    return json.loads(lines[-1][len("__OUT__"):])
