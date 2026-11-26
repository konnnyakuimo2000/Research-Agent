"""設定画面（.claude/ui-gateway-settings-plan.md）の画面側。本物のモジュールを Node で動かす（tests/ui_harness.py）。
パネルの描き方（Svelte の部品）は webui/src/components/settings/settings.test.ts（Vitest。tests/test_webui_vitest.py が呼ぶ）。

- 値の優先順位: この依頼だけ → ブラウザの既定値 → 名乗りの既定。「この依頼だけ」は送ったら消え、次に持ち越さない
- プリセットの名前（標準・すばやく・カスタム）と、送る設定（既定と違うものだけ）
- モデル: 使えない・一覧から消えたモデルは送らず、理由と代わりに使うモデルを出す
- 履歴の詳細: 開始時に効いた値を出し、前の版の記録は「開始時の既定値は記録なし」。「この設定を使う」で読み込む
- ペインの幅: 窓に収める
"""

from __future__ import annotations

from tests.ui_harness import run_js

SETUP = """
  import { ctx } from 'UI/core/ctx.ts';
  import * as S from 'UI/panels/settings.ts';
  import * as M from 'UI/panels/models.ts';
  ctx.AGENTS = { research: { id: "research", name: "調査", ok: true, default: true,
    settings_schema: { type: "object", properties: {
      max_seconds: { type: "integer", title: "時間の上限（秒）", default: 900, minimum: 420, maximum: 3600 },
      max_pages: { type: "integer", title: "読むページの数", default: 12, minimum: 2, maximum: 30 },
      images: { type: "boolean", title: "画像も探す", default: true } } },
    presets: { quick: { title: "すばやく", values: { max_seconds: 420, max_pages: 6 } } },
    model_roles: [{ role: "extract", title: "読み取り", needs: ["json_schema"] }, { role: "vision", title: "画像", needs: ["json_schema", "vision"] }] } };
  globalThis.fetch = async (url) => ({ ok: true, json: async () => url.startsWith("/api/models") ? {
    available: true, default: "lab", roles: {},
    models: [{ id: "lab", title: "LAB", capabilities: ["json_schema", "vision"], available: true, price: {} },
             { id: "text", title: "TEXT", capabilities: ["json_schema"], available: true, price: {} },
             { id: "claude", title: "Claude", capabilities: ["json_schema", "vision"], available: true, external: true,
               price: { input_per_mtok: 3, output_per_mtok: 15 } }] } : { agent: "research", ok: true, checks: {} } });
  await M.loadModels();
  S.initSettings("research");
"""


def test_presets_and_what_is_sent():
    out = run_js(SETUP + """
      const p = (v) => S.presetName("research", v);
      out({ std: p({}), same: p({ max_seconds: 900 }), quick: p({ max_seconds: 420, max_pages: 6, images: true }),
            custom: p({ images: false }), body: S.requestBody(), head: S.summary() });
    """)
    assert (out["std"], out["same"], out["quick"], out["custom"]) == ("標準", "標準", "すばやく", "カスタム")
    assert out["body"] == {"agent": "research", "settings": {}, "models": {}}
    assert out["head"]["agent"] == "調査" and out["head"]["preset"] == "標準" and out["head"]["oneTime"] == ""


def test_apply_is_for_one_request_and_save_is_for_every_request():
    out = run_js(SETUP + """
      S.openPanel("research");
      S.draftState().settings = { preset: "quick", values: { max_seconds: 420, max_pages: 6, images: true } };
      S.applyOnce();
      const once = { body: S.requestBody(), head: S.summary(), open: S.panelOpen() };
      S.consumeOneTime();                                       // 送った
      const next = S.requestBody();
      S.openPanel("research");
      S.draftState().settings = { preset: "", values: { images: false } };
      S.saveDefault();
      const saved = S.requestBody(); S.consumeOneTime();
      const still = S.requestBody();
      S.openPanel("research"); S.resetDraft();
      const reset = S.draftState(), unchanged = S.requestBody();
      out({ once, next, saved, still, reset, unchanged });
    """)
    assert out["once"]["body"]["settings"] == {"max_seconds": 420, "max_pages": 6}
    assert out["once"]["head"]["preset"] == "すばやく" and out["once"]["head"]["oneTime"] == "この依頼だけ"
    assert out["once"]["open"] is False                                  # 適用したらチャットに戻る
    assert out["next"]["settings"] == {}                                 # 次の依頼に持ち越さない
    assert out["saved"]["settings"] == {"images": False} == out["still"]["settings"]   # 保存は毎回効く
    assert out["reset"]["settings"] == {"preset": "", "values": {}}      # 標準に戻すのは編集中の値だけ
    assert out["unchanged"]["settings"] == {"images": False}             # 適用・保存するまで変わらない


def test_a_past_run_is_loaded_for_one_request_and_then_dropped():
    out = run_js(SETUP + """
      const run = { id: "r1", agent: "research", status: "done", request: "x", created: 0,
        settings: { images: false }, effective_settings: { max_seconds: 900, max_pages: 12, images: false, removed: 1 },
        models: { extract: "text", vision: "text" } };
      S.useRun(run);
      const once = { body: S.requestBody(), head: S.summary() };
      S.consumeOneTime();
      out({ once, after: S.requestBody(), afterHead: S.summary() });
    """)
    once = out["once"]
    assert once["body"]["settings"] == {"images": False}                 # 既定と違うものだけ・名乗りにない項目は落とす
    assert once["body"]["models"] == {"extract": "text"}                 # 画像を読めないモデルは送らない
    assert once["head"]["oneTime"] == "過去の設定" and once["head"]["preset"] == "カスタム"
    assert once["head"]["warnings"] == ["画像: TEXT は使えない（画像に要る vision に対応していない）→ LAB を使う"]
    assert out["after"] == {"agent": "research", "settings": {}, "models": {}}   # 次の依頼に持ち越さない
    assert out["afterHead"]["oneTime"] == "" and out["afterHead"]["warnings"] == []


def test_models_that_disappeared_or_are_external_are_explained():
    out = run_js(SETUP + """
      out({ gone: M.invalidChoices({ all: "old-model", roles: {} }, "research"),
            ext: M.usesExternal({ all: "claude", roles: {} }, "research"),
            label: M.choiceLabel({ all: "", roles: { extract: "text" } }, "research") });
    """)
    assert out["gone"] == ["読み取り: old-model は使えない（一覧にない）→ LAB を使う",
                           "画像: old-model は使えない（一覧にない）→ LAB を使う"]
    assert out["ext"] is True and out["label"] == "役割ごとのモデル"


def test_run_details_show_what_applied_at_the_start():
    out = run_js(SETUP + """
      const now = S.runSettingRows({ id: "r1", agent: "research", settings: { images: false },
        effective_settings: { max_seconds: 900, max_pages: 12, images: false } });
      const old = S.runSettingRows({ id: "r0", agent: "research", settings: { max_pages: 6 }, effective_settings: null });
      out({ now, old });
    """)
    now = {r["title"]: (r["value"], r["mark"]) for r in out["now"]}
    old = {r["title"]: (r["value"], r["mark"]) for r in out["old"]}
    assert now == {"時間の上限（秒）": ("900", "既定"), "読むページの数": ("12", "既定"), "画像も探す": ("いいえ", "変更")}
    assert old == {"時間の上限（秒）": ("記録なし", ""), "読むページの数": ("6", "変更"), "画像も探す": ("記録なし", "")}


def test_connection_cases_are_told_apart():
    out = run_js(SETUP + """
      const reply = (v) => { globalThis.fetch = async () => ({ ok: true, json: async () => v }); };
      // 検索（SearXNG）だけが不調: エージェントは応答し、検索の確認だけが失敗
      reply({ agent: "research", ok: true, model: "qwen", checks: {
        llm: { title: "既定モデルの一覧 API", ok: true, seconds: 0.2 },
        search: { title: "検索（SearXNG の healthz）", ok: false, error: "ConnectError", hint: "検索につながらない" } } });
      await S.loadAgentStatus("research");
      const searchOnly = S.connectionProblem();
      // エージェントにつながらない
      globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({ detail: "応答なし" }) });
      await S.loadAgentStatus("research");
      const agentDown = S.connectionProblem();
      // モデルの窓口だけが不調（状態を読めない）
      const gatewayDown = await M.loadModelStatus();
      out({ searchOnly, agentDown, gatewayDown });
    """)
    assert out["searchOnly"] == "検索につながらない"
    assert out["agentDown"] == "エージェントにつながらない"
    assert out["gatewayDown"] is None


def test_pane_widths_fit_the_window():
    out = run_js("""
      import { fitWidths } from 'UI/panels/layout.js';
      out({ wide: fitWidths({ side: 300, chat: 600 }, 1600), narrow: fitWidths({ side: 300, chat: 900 }, 1300),
            tiny: fitWidths({ side: 50, chat: 100 }, 1300), none: fitWidths({ side: null, chat: null }, 1300) });
    """)
    assert out["wide"] == {"side": 300, "chat": 600}
    assert out["narrow"] == {"side": 300, "chat": 640}                   # 1300 - 300 - 作業記録の 360
    assert out["tiny"] == {"side": 160, "chat": 280}
    assert out["none"] == {"side": None, "chat": None}


def test_the_header_follows_the_stores_without_wiring():
    """チャットの上の要約は、設定のストアを読むだけで変わる（前は app.js への hooks.summaryChanged の手配線）。"""
    out = run_js(SETUP + """
      const seen = [];
      S.headState.subscribe((s) => s && seen.push([s.preset, s.oneTime]));
      S.openPanel("research");
      S.draftState().settings = { preset: "quick", values: { max_seconds: 420, max_pages: 6, images: true } };
      S.applyOnce();
      const applied = seen.at(-1);
      S.consumeOneTime();
      out({ first: seen[0], applied, last: seen.at(-1) });
    """)
    assert out == {"first": ["標準", ""], "applied": ["すばやく", "この依頼だけ"], "last": ["標準", ""]}
