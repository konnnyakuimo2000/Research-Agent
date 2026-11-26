// 履歴の一覧・行の「…」のメニュー・最近の依頼
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import RunList from "./RunList.svelte";
import RunMenu from "./RunMenu.svelte";
import Recent from "./Recent.svelte";
import { agents, catalog, openRunId, runMenu, runs } from "../../stores/state.ts";
import type { AgentInfo, RunSummary } from "../../types/api.ts";

const agent = (id: string, name: string, def: boolean) =>
  ({ id, name, default: def, ok: true, model_roles: [], levels: [0], extensions: [], capabilities: [] }) as unknown as AgentInfo;
const run = (id: string, request: string, status = "done", extra: Partial<RunSummary> = {}) =>
  ({ id, request, status, agent: "research", models: {}, settings: {}, effective_settings: null, created: 0, question: "", events: 0, seconds: null, ...extra }) as RunSummary;
const RUNS = [run("20261125-140246-818c", "はやぶさについて", "running"), run("20261125-134500-aaaa", "東京タワーの高さ"),
              run("20261125-130000-bbbb", "はやぶさについて", "done", { agent: "chat" }), run("20261124-120000-cccc", "富士山"),
              run("20261124-110000-dddd", "松田教授")];

const apps: ReturnType<typeof mount>[] = [];
const onOpen = vi.fn(), onDetail = vi.fn(), onDelete = vi.fn(), onPick = vi.fn();
beforeEach(() => {
  document.body.innerHTML = `<div id="list"></div><div id="menu"></div><div id="recent"></div>`;
  agents.set({ research: agent("research", "調査", true), chat: agent("chat", "チャット", false) });
  catalog.set(null);
  runs.set({ list: RUNS, error: "", loaded: true });
  openRunId.set(null); runMenu.set(null);
  apps.push(mount(RunList, { target: document.getElementById("list")!, props: { onOpen } }));
  apps.push(mount(RunMenu, { target: document.getElementById("menu")!, props: { onDetail, onDelete } }));
  apps.push(mount(Recent, { target: document.getElementById("recent")!, props: { onPick } }));
  flushSync();
});
afterEach(() => { apps.splice(0).forEach((a) => unmount(a)); vi.clearAllMocks(); });

describe("履歴", () => {
  it("行に日時・状態・既定でないエージェントを出し、開いている依頼に印を付ける", () => {
    const rows = [...document.querySelectorAll(".run")];
    expect(rows).toHaveLength(5);
    expect(rows[0].querySelector(".r2")!.textContent).toBe("11/25 14:02 · 調べている");
    expect(rows[2].querySelector(".r2")!.textContent).toBe("11/25 13:00 · 完了 · チャット");
    openRunId.set("20261125-134500-aaaa"); flushSync();
    expect(document.querySelector(".run.on")!.getAttribute("data-id")).toBe("20261125-134500-aaaa");
    (rows[1] as HTMLButtonElement).click();
    expect(onOpen).toHaveBeenCalledWith(RUNS[1]);
  });

  it("「…」で詳細と削除。動いている調査は削除できない", async () => {
    const more = document.querySelectorAll(".more");
    (more[0] as HTMLButtonElement).click(); flushSync();
    expect(document.getElementById("runMenu")).not.toBeNull();
    expect(more[0].getAttribute("aria-expanded")).toBe("true");
    expect(document.querySelector(".mnote")!.textContent).toContain("終わってから削除できます（調べている）");
    (document.querySelector("[data-act=delete]") as HTMLButtonElement).click(); flushSync();
    expect(onDelete).not.toHaveBeenCalled();
    (more[1] as HTMLButtonElement).click(); flushSync();               // 別の行の「…」に切り替わる
    (document.querySelector("[data-act=delete]") as HTMLButtonElement).click(); flushSync();
    expect(onDelete).toHaveBeenCalledWith(RUNS[1], more[1]);
    expect(document.getElementById("runMenu")).toBeNull();
    (more[1] as HTMLButtonElement).click(); flushSync();
    document.body.click(); flushSync();                                // 外を押すと閉じる
    expect(document.getElementById("runMenu")).toBeNull();
  });

  it("最近の依頼は、同じ依頼を 1 つにまとめて 3 つまで", () => {
    const ex = [...document.querySelectorAll(".ex")].map((b) => b.textContent);
    expect(ex).toEqual(["はやぶさについて", "東京タワーの高さ", "富士山"]);
    (document.querySelector(".ex") as HTMLButtonElement).click();
    expect(onPick).toHaveBeenCalledWith("はやぶさについて");
  });

  it("一覧を読めなければ、そう出す", () => {
    runs.set({ list: [], error: "HTTP 503", loaded: true }); flushSync();
    expect(document.getElementById("list")!.textContent).toContain("一覧を読めません: HTTP 503");
  });
});
