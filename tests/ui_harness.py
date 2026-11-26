"""画面の JavaScript（ES モジュール）を Node で動かす道具。ブラウザの部品（document など）は最小限の偽物にする。

使い方: run_js("import { x } from 'UI/core/util.ts'; out(x(1));") → out() に渡した値（JSON）。
Node がなければ、そのテストは飛ばす。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "webui" / "src"   # 画面のソース（ビルド前。index.html は 1 つ上）

# 画面のモジュールが使うだけのブラウザの部品の偽物。要素はクラスの付け外しと中身（innerHTML・textContent）を覚える。
# 子の要素（querySelector）は、セレクターごとに同じ偽物を返す
_FAKE_DOM = r"""
class FakeEl {
  constructor() { this.className = ""; this.style = { setProperty() {} }; this.dataset = {}; this.children = [];
    this._q = {}; this.innerHTML = ""; this.textContent = ""; this.hidden = false; this.disabled = false; this.title = ""; }
  get classList() {
    const el = this, list = () => el.className.split(/\s+/).filter(Boolean);
    const set = (xs) => { el.className = [...new Set(xs)].join(" "); };
    return {
      add: (...c) => set([...list(), ...c]),
      remove: (...c) => set(list().filter((x) => !c.includes(x))),
      toggle: (c, on) => { const want = on === undefined ? !list().includes(c) : !!on;
        set(want ? [...list(), c] : list().filter((x) => x !== c)); return want; },
      contains: (c) => list().includes(c),
    };
  }
  querySelector(sel) { return (this._q[sel] ||= new FakeEl()); }
  querySelectorAll() { return []; }
  addEventListener() {} removeEventListener() {} dispatchEvent() {}
  appendChild(c) { this.children.push(c); return c; } prepend(c) { this.children.unshift(c); return c; } remove() {}
  insertBefore(c) { this.children.push(c); return c; }
  insertAdjacentHTML() {} setAttribute() {} getAttribute() { return null; } focus() {} closest() { return null; }
  contains() { return false; } setPointerCapture() {} requestSubmit() {}
  getBoundingClientRect() { return { top: 0, bottom: 600, left: 0, right: 800, width: 800, height: 600 }; }
  get offsetWidth() { return 200; } get offsetHeight() { return 50; }
  get clientWidth() { return 800; } get clientHeight() { return 600; } get scrollHeight() { return 0; }
}
const byId = {}, byQuery = {};
globalThis.FakeEl = FakeEl;
globalThis.document = {
  getElementById: (id) => (byId[id] ||= new FakeEl()), createElement: () => new FakeEl(), createElementNS: () => new FakeEl(),
  querySelector: (s) => (byQuery[s] ||= new FakeEl()), querySelectorAll: () => [], addEventListener() {},
};
globalThis.window = { addEventListener() {}, dispatchEvent() {}, matchMedia: () => ({ matches: false }), innerHeight: 900 };
globalThis.location = { search: "" };
globalThis.requestAnimationFrame = () => 0;
globalThis.CSS = { escape: (s) => s };
globalThis.out = (v) => console.log("__OUT__" + JSON.stringify(v));
"""


_STRIPS: dict[str, bool] = {}


def _strips_types(node: str) -> bool:
    if node not in _STRIPS:
        v = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v").split(".")
        major, minor = int(v[0]), int(v[1])
        _STRIPS[node] = major > 22 or (major == 22 and minor >= 18)
    return _STRIPS[node]


def run_js(body: str) -> object:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node がない")
    if not _strips_types(node):
        pytest.skip("Node が古い（画面の .ts をそのまま動かすには 22.18 以上が要る）")
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
