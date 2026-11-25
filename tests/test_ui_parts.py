"""画面の部品化（X3。docs/telemetry_enhancement_plan.md 4.2・8.1・D-1・D-2）。

- 設定の欄はエージェントの settings_schema から作る。ゲートウェイは同じ形で確かめてからエージェントへ渡す
- 調査エージェントは、時間の上限・読むページの数・画像を探すかを依頼ごとの設定で変えられる（既定は今までの値）
- 画面は、名乗りの extensions にある拡張の部品（ui/extensions/<名前>/）だけを読み込む
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from research_agent.agent.coverage import MISSING
from research_agent.agent.guard import research_step, time_over
from research_agent.agent.state import MAX_PAGES, MAX_SECONDS, PRESETS, SETTINGS, setting
from research_agent.agents import research
from research_agent.gateway.settings import SettingsError, check_settings
from test_gateway import Stack, _envs, wait_for

UI = Path(__file__).resolve().parents[1] / "src" / "research_agent" / "ui"
SCHEMA = {"type": "object", "properties": SETTINGS}


# ---- ゲートウェイの確かめ ----

def test_settings_are_checked_against_the_schema():
    assert check_settings(SCHEMA, {"max_pages": 6, "images": False}) == {"max_pages": 6, "images": False}
    assert check_settings(SCHEMA, {}) == {}                        # 既定はエージェントが使う（足さない）
    assert check_settings(SCHEMA, {"max_seconds": 600.0}) == {"max_seconds": 600}
    for bad, why in [({"max_pages": 100}, "範囲"), ({"max_pages": "6"}, "整数"), ({"max_pages": 2.5}, "整数"),
                     ({"images": "yes"}, "はい・いいえ"), ({"color": "red"}, "ありません"), ({"max_pages": True}, "整数")]:
        with pytest.raises(SettingsError, match=why):
            check_settings(SCHEMA, bad)


def test_enum_and_array_settings():
    schema = {"properties": {"style": {"type": "string", "enum": ["a", "b"]},
                             "dbs": {"type": "array", "items": {"enum": ["ORCID", "NDL"]}}}}
    assert check_settings(schema, {"style": "b", "dbs": ["NDL"]}) == {"style": "b", "dbs": ["NDL"]}
    with pytest.raises(SettingsError):
        check_settings(schema, {"style": "c"})
    with pytest.raises(SettingsError):
        check_settings(schema, {"dbs": ["Google"]})


# ---- 調査エージェントの設定 ----

def test_research_manifest_names_its_settings_and_presets():
    m = research.MANIFEST
    assert set(m["settings_schema"]["properties"]) == {"max_seconds", "max_pages", "images"}
    assert m["presets"] == PRESETS
    for p in PRESETS.values():                                        # プリセットも自分の形で確かめられる
        check_settings(m["settings_schema"], p["values"])
    # 既定の値は、今までの定数のまま（設定しなければ調査は変わらない）
    assert SETTINGS["max_seconds"]["default"] == MAX_SECONDS and SETTINGS["max_pages"]["default"] == MAX_PAGES


def test_setting_falls_back_to_the_default_and_stays_in_range():
    assert setting({}, "max_pages") == MAX_PAGES
    assert setting({"settings": {"max_pages": 6}}, "max_pages") == 6
    assert setting({"settings": {"max_pages": 999}}, "max_pages") == 30
    assert setting({"settings": {"max_pages": "x"}}, "max_pages") == MAX_PAGES
    assert setting({"settings": {"images": False}}, "images") is False


def test_time_limit_follows_the_setting():
    trace = [{"seconds": 200}]
    assert not time_over({"trace": trace})                            # 既定の 900 秒なら、まだ続けられる
    assert time_over({"trace": trace, "settings": {"max_seconds": 480}})   # 480 - 報告の 300 秒 < 200 秒


def test_page_limit_follows_the_setting():
    pages = [{"url": f"https://ex.org/{i}", "ok": True} for i in range(6)]
    state = {"request": "x", "pages_read": pages, "reading_list": ["https://ex.org/new"], "verified": [{"text": "a"}],
             "coverage": [{"aspect": "経歴", "status": MISSING}], "conflicts_checked_at": 1, "images_checked": True}
    assert (research_step(state, {"action": "respond"}) or {}).get("action") == "read_pages"
    state["settings"] = {"max_pages": 6}
    assert (research_step(state, {"action": "respond"}) or {}).get("action") != "read_pages"


def _settings_graph():
    """受け取った設定を、そのまま答えにする（エージェントからグラフまで設定が届くか）。"""
    class T(TypedDict, total=False):
        request: str
        settings: dict
        answer: str

    g = StateGraph(T)
    g.add_node("respond", lambda s: {"answer": repr(sorted((s.get("settings") or {}).items()))})
    g.add_edge(START, "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=InMemorySaver())


def test_settings_travel_from_the_screen_api_to_the_graph(tmp_path):
    s = Stack(tmp_path, graph=_settings_graph())
    agents = {a["id"]: a for a in s.c.get("/api/agents").json()["agents"]}
    assert "max_pages" in agents["research"]["settings_schema"]["properties"] and agents["research"]["presets"]
    assert agents["chat"]["settings_schema"] == {} and agents["chat"]["presets"] == {}

    r = s.c.post("/api/runs", json={"request": "x", "agent": "research", "settings": {"max_pages": 6, "images": False}})
    assert r.status_code == 200 and r.json()["settings"] == {"max_pages": 6, "images": False}
    run_id = r.json()["id"]
    assert wait_for(lambda: s.gw.store.get(run_id)["status"] == "done")
    report = [e for e in _envs(s.c.get(f"/api/runs/{run_id}/events").text) if e["type"] == "run.end"][-1]["data"]["report"]
    assert report == "[('images', False), ('max_pages', 6)]"
    assert next(x for x in s.c.get("/api/runs").json()["runs"] if x["id"] == run_id)["settings"] == {"max_pages": 6, "images": False}

    for bad in ({"max_pages": 100}, {"unknown": 1}):
        assert s.c.post("/api/runs", json={"request": "x", "agent": "research", "settings": bad}).status_code == 400
    assert s.c.post("/api/runs", json={"request": "x", "agent": "research", "settings": [1]}).status_code == 400
    # 設定の形を持たないエージェントには、設定を送れない
    assert s.c.post("/api/runs", json={"request": "x", "agent": "chat", "settings": {"max_pages": 6}}).status_code == 400


# ---- 画面の部品 ----

def test_every_extension_an_agent_names_has_screen_parts():
    for m in (research.MANIFEST,):
        for name in m["extensions"]:
            slug = re.sub(r"[^A-Za-z0-9]+", "-", name)
            index = UI / "extensions" / slug / "index.js"
            assert index.exists(), index
            assert f'registerExtension("{name}"' in index.read_text(encoding="utf-8")


def test_common_screen_parts_do_not_know_the_research_extension():
    """共通の部品（core・views・panels）は 0・1 段だけを知る。調査の言葉（網羅表・ページ・画像）は拡張の中だけ。"""
    common = [p for d in ("core", "views", "panels") for p in (UI / d).glob("*.js")] + [UI / "app.js"]
    for p in common:
        text = re.sub(r"(^|\s)//.*", "", p.read_text(encoding="utf-8"))   # コメントの例示は除く
        for word in ("research/v1", "research-v1", "page.done", "coverage", "網羅表", "aspects"):
            assert word not in text, (p.name, word)


def test_modules_import_only_files_that_exist():
    for p in UI.rglob("*.js"):
        for target in re.findall(r'from\s+"(\.[^"]+)"', p.read_text(encoding="utf-8")):
            assert (p.parent / target).resolve().exists(), (p, target)
    assert '<script type="module" src="app.js">' in (UI / "index.html").read_text(encoding="utf-8")
