// チャットの吹き出しを、本物のイベントの描き手（panels/chat.ts）から描いて確かめる
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import ChatLog from "./ChatLog.svelte";
import { hooks, resetChat, runMeta } from "../../panels/chat.ts";
import { dispatch } from "../../core/events.ts";
import { ctx } from "../../core/ctx.ts";
import { live } from "../../stores/state.ts";

let app: ReturnType<typeof mount>;
const onMeta = vi.fn();
const chat = () => document.getElementById("chat")!;
const send = (type: string, data: Record<string, unknown> = {}) => { dispatch({ type, ...data }); flushSync(); };

beforeEach(() => {
  document.body.innerHTML = `<section id="chatpane" class="empty"><div id="chat"></div></section>`;
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ runs: [] }) })));
  ctx.replay = false; ctx.runId = "r1"; ctx.runAgent = "chat"; ctx.AGENTS = { chat: { levels: [0] } as any };   // 作業記録のないエージェント
  resetChat();
  app = mount(ChatLog, { target: chat(), props: { onMeta } });
  flushSync();
});
afterEach(() => { unmount(app); vi.unstubAllGlobals(); });

describe("チャット", () => {
  it("依頼・状態の一行・報告・保存の印を順に出す", () => {
    send("run.start", { request: "東京タワーの高さ" });
    expect(document.getElementById("chatpane")!.classList.contains("empty")).toBe(false);
    expect(chat().querySelector(".u")!.textContent).toBe("東京タワーの高さ");
    expect(chat().querySelector(".status .txt")!.textContent).toBe("始めた");
    send("status", { text: "検索している" });                      // 流れの図のないエージェントでは吹き出しに出す
    expect(chat().querySelector(".status .txt")!.textContent).toBe("検索している");
    send("run.end", { report: "## 答え\n- 高さは 333 m [1]\n- https://example.org/x" });
    expect(chat().querySelector(".status")).toBeNull();
    expect(chat().querySelector(".body h4")!.textContent).toBe("答え");
    const cite = chat().querySelector(".cite") as HTMLButtonElement;
    expect(cite.textContent).toBe("1");
    expect(cite.disabled).toBe(true);                                // 出典を見せる拡張がない
    expect(chat().querySelector(".body a")!.getAttribute("href")).toBe("https://example.org/x");
    expect(chat().querySelector(".ractions button")!.textContent).toBe("報告をコピー");
    send("run.saved");
    expect(chat().querySelector(".saved")!.textContent).toBe("報告と成果物を保存した");
  });

  it("番号で答える質問は、ライブでは押すと答えを送り、再生では読むだけ", () => {
    const sent: string[] = [];
    const before = hooks.sendAnswer;
    hooks.sendAnswer = (a: string) => { sent.push(a); };
    try {
      ctx.replay = true;
      send("question", { question: "どれ？\n1. 塔\n2. 山", choices: true });
      const replayBtn = chat().querySelector(".opts button") as HTMLButtonElement;
      expect(replayBtn.disabled).toBe(true);
      ctx.replay = false;
      send("question", { question: "どれ？\n1. 塔\n2. 山\n（ライブ）", choices: true });
      send("run.waiting", { question: "どれ？\n1. 塔\n2. 山\n（ライブ）" });
      let waiting: string | null = null; live.subscribe((l) => { waiting = l.waitingRun; })();
      expect(waiting).toBe("r1");
      const buttons = chat().querySelectorAll(".opts button");
      (buttons[buttons.length - 1] as HTMLButtonElement).click();
      expect(sent).toEqual(["2"]);
    } finally { hooks.sendAnswer = before; }
  });

  it("文の続きを流し、止めたら止めたと出す", () => {
    send("run.start", { request: "x" });
    send("message.delta", { text: "途中" });
    send("message.delta", { text: "まで" });
    expect(chat().querySelector(".a .body p")!.textContent).toBe("途中まで");
    send("run.cancelled");
    expect(chat().textContent).toContain("止めました。");
  });

  it("頭の行（使ったモデル・設定・費用）を押すと詳細を開く", () => {
    runMeta.set({ models: "応答したモデル: LAB 3 回", modelsTitle: "", settings: "設定: 画像も探す: いいえ", cost: "費用 $0.01" }); flushSync();
    const meta = document.getElementById("runMeta") as HTMLButtonElement;
    expect(meta.textContent).toContain("LAB 3 回");
    meta.click();
    expect(onMeta).toHaveBeenCalledWith(meta);
    resetChat(); flushSync();
    expect(document.getElementById("runMeta")).toBeNull();
  });
});
