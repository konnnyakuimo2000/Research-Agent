// 設定のパネル（Svelte の部品）を jsdom で描いて確かめる。状態と操作のテストは tests/test_ui_settings.py（Node）
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import SettingsPanel from "./SettingsPanel.svelte";
import { agents, catalog } from "../../stores/state.ts";
import { closePanel, draftState, initSettings, openPanel, panelOpen, setTab } from "../../panels/settings.ts";
import { loadAgentStatus } from "../../panels/settings.ts";
import { loadModelStatus } from "../../panels/models.ts";
import type { AgentInfo, RunSummary } from "../../types/api.ts";

const AGENT: AgentInfo = {
  id: "research", name: "調査", version: "1", description: "", levels: [0, 1], extensions: [], capabilities: [], graph: null,
  settings_schema: { type: "object", properties: {
    max_seconds: { type: "integer", title: "時間の上限（秒）", default: 900, minimum: 420, maximum: 3600 },
    max_pages: { type: "integer", title: "読むページの数", default: 12, minimum: 2, maximum: 30 },
    images: { type: "boolean", title: "画像も探す", default: true } } },
  presets: { quick: { title: "すばやく", values: { max_seconds: 420, max_pages: 6 } } },
  model_roles: [{ role: "extract", title: "読み取り", needs: ["json_schema"] }, { role: "vision", title: "画像", needs: ["json_schema", "vision"] }],
  ok: true, error: null, default: true,
};
const CATALOG = {
  available: true, default: "lab", roles: {},
  models: [{ id: "lab", title: "LAB", capabilities: ["json_schema", "vision"], available: true, price: {} },
           { id: "text", title: "TEXT", capabilities: ["json_schema"], available: true, price: {} },
           { id: "claude", title: "Claude", capabilities: ["json_schema", "vision"], available: true, external: true,
             price: { input_per_mtok: 3, output_per_mtok: 15 } }],
};
const reply = (v: unknown, ok = true) => vi.stubGlobal("fetch", vi.fn(async () => ({ ok, status: ok ? 200 : 503, json: async () => v })));
const body = () => document.getElementById("setBody")!;
const settle = () => new Promise((r) => setTimeout(r, 0)).then(() => flushSync());

let app: ReturnType<typeof mount>;
beforeEach(() => {
  document.body.innerHTML = `<textarea id="input"></textarea><div id="root"></div>`;
  window.matchMedia ||= ((q: string) => ({ matches: false, media: q })) as any;   // jsdom にはない
  reply({ agent: "research", ok: true, checks: {} });
  agents.set({ research: AGENT });
  catalog.set(CATALOG);
  initSettings("research");
  app = mount(SettingsPanel, { target: document.getElementById("root")! });
});
afterEach(() => { closePanel(); flushSync(); unmount(app); vi.unstubAllGlobals(); });

describe("設定のパネル", () => {
  it("開くと見出しにフォーカスし、Esc で閉じて開いたボタンに戻る", () => {
    const btn = document.createElement("button"); document.body.appendChild(btn);
    openPanel("research", btn); flushSync();
    expect(document.activeElement?.id).toBe("setTitle");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); flushSync();
    expect(panelOpen()).toBe(false);
    expect(document.getElementById("setPanel")).toBeNull();
    expect(document.activeElement).toBe(btn);
  });

  it("プリセットと欄を変えると、編集中の値とプリセットの名前が変わる", () => {
    openPanel("research"); flushSync();
    (document.querySelector('.pre[data-p="quick"]') as HTMLButtonElement).click(); flushSync();
    expect(document.querySelector(".pre.on")!.textContent).toBe("すばやく");
    expect(draftState()!.settings.values).toMatchObject({ max_seconds: 420, max_pages: 6 });
    const pages = document.getElementById("set-max_pages") as HTMLInputElement;
    pages.value = "100"; pages.dispatchEvent(new Event("change", { bubbles: true })); flushSync();
    expect(draftState()!.settings.values.max_pages).toBe(30);                  // 範囲に収める
    expect(document.querySelector(".pre.custom")).not.toBeNull();              // プリセットから外れた
    expect(document.querySelector('label[for="set-max_pages"] .chg')).not.toBeNull();
  });

  it("モデルのタブは、外部に送ることを料金とともに知らせる", () => {
    openPanel("models"); flushSync();
    (document.querySelector('.opt[data-all="claude"]') as HTMLButtonElement).click(); flushSync();
    const html = body().innerHTML;
    expect(html).toContain("最終的な割り当て");
    expect(html).toContain("依頼文と読んだページを送ります");
    expect(html).toContain("入力 $3・出力 $15");
    expect((document.querySelector('.opt[data-all="text"]') as HTMLButtonElement).disabled).toBe(true);   // 画像の役割に使えない
  });

  it("履歴の詳細は、開始時に効いた設定を出す（前の版の記録は、変えた項目だけ）", async () => {
    reply({ answered: [], usd: 0 });
    const run = (r: Partial<RunSummary>) => ({ status: "done", request: "x", created: 0, question: "", events: 0, seconds: null, ...r }) as RunSummary;
    openPanel("run", null, run({ id: "r1", agent: "research", request: "東京タワー",
      settings: { images: false }, effective_settings: { max_seconds: 900, max_pages: 12, images: false }, models: { extract: "gone" } }));
    flushSync();
    const now = body().textContent!;
    expect(now).toContain("開始時に適用した調査の設定");
    expect(now).toContain("いいえ"); expect(now).toContain("900"); expect(now).toContain("既定");
    expect(now).toContain("gone（今は一覧にない）");
    closePanel(); flushSync();
    openPanel("run", null, run({ id: "r0", agent: "research", request: "古い", settings: { max_pages: 6 }, effective_settings: null, models: {} }));
    flushSync();
    const old = body().textContent!;
    expect(old).toContain("開始時の既定値は記録なし"); expect(old).toContain("記録なし"); expect(old).toContain("6");
    await settle();
    expect(document.getElementById("runAnswered")!.textContent).toBe("記録なし");
  });

  it("接続のタブは、エージェント・検索・モデルの窓口の不調を分けて出す", async () => {
    openPanel("status"); flushSync();
    reply({ agent: "research", ok: true, model: "qwen", checks: {
      llm: { title: "既定モデルの一覧 API", ok: true, seconds: 0.2 },
      search: { title: "検索（SearXNG の healthz）", ok: false, error: "ConnectError", hint: "検索につながらない" } } });
    await loadAgentStatus("research"); flushSync();
    expect(body().textContent).toContain("検索（SearXNG の healthz）");
    expect(body().textContent).toContain("ConnectError");
    expect(body().textContent).toContain("0.2 秒で応答");
    reply({ detail: "応答なし" }, false);
    await loadModelStatus(); flushSync();
    expect(body().textContent).toContain("モデルの窓口につながらない");
    reply({ available: true, models: { claude: { ok: true, seconds: 0.4, calls: 3, unmeasured: 1, cost_usd: 0.02 } },
            spend: { day: "2026-11-25", today_usd: 0.02, unmeasured_today: 1, limits: { daily_usd: 5, run_usd: 1 } } });
    await loadModelStatus(); flushSync();
    expect(body().textContent).toContain("未計測 1 件（費用に含まない）");
    expect(body().textContent).toContain("上限到達後に有料呼び出しを停止");
    expect(body().textContent).toContain("1 日 $5.00");
  });

  it("タブを変えても、パネルは開いたまま", () => {
    openPanel("research"); flushSync();
    setTab("display"); flushSync();
    expect(body().textContent).toContain("幅を既定に戻す");
    expect(document.querySelector('[data-tab="display"]')!.getAttribute("aria-selected")).toBe("true");
  });
});
