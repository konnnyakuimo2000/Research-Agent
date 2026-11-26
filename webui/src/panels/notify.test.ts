// 調査の知らせ: 許可・出す条件（ライブで画面が後ろのとき）・文面・設定の「表示」の切り替え
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import { messageFor, notifyFor, notifyState, setNotify } from "./notify.ts";
import DisplayTab from "../components/settings/DisplayTab.svelte";

const shown: { title: string; options: NotificationOptions }[] = [];
let permission: NotificationPermission = "default";
class FakeNotification {
  onclick: (() => void) | null = null;
  constructor(title: string, options: NotificationOptions) { shown.push({ title, options }); }
  close() {}
  static get permission() { return permission; }
  static requestPermission = vi.fn();
}
const setHidden = (hidden: boolean) => {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  vi.spyOn(document, "hasFocus").mockReturnValue(!hidden);
};

beforeEach(() => {
  shown.length = 0; permission = "default"; localStorage.clear();
  FakeNotification.requestPermission = vi.fn(async () => { permission = "granted"; return permission; });
  vi.stubGlobal("Notification", FakeNotification);
  window.matchMedia ||= ((q: string) => ({ matches: false, media: q })) as any;
  notifyState.set("off");
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("調査の知らせ", () => {
  it("文面: 答え待ちは質問の 1 行目、終わり・エラーは依頼文とともに", () => {
    expect(messageFor({ type: "run.waiting", question: "どれ？\n1. 塔" }, "東京タワー")).toEqual({ title: "答えを待っています", body: "「東京タワー」: どれ？" });
    expect(messageFor({ type: "run.end" }, "東京タワー")).toEqual({ title: "報告ができました", body: "「東京タワー」" });
    expect(messageFor({ type: "run.error", error: "LLM が応答しない" }, "")!.body).toBe("調査: LLM が応答しない");
    expect(messageFor({ type: "step.start" }, "x")).toBeNull();
  });

  it("入れたときに許可を求め、画面が後ろにあるときだけ出す", async () => {
    expect(await notifyFor({ type: "run.end" }, "r1", "x")).toBe(false);          // 入れていない
    expect(await setNotify(true)).toBe("on");
    expect(FakeNotification.requestPermission).toHaveBeenCalledOnce();
    setHidden(false);
    expect(await notifyFor({ type: "run.end" }, "r1", "x")).toBe(false);          // 見えている
    setHidden(true);
    expect(await notifyFor({ type: "run.end" }, "r1", "東京タワー")).toBe(true);
    expect(shown[0]).toMatchObject({ title: "報告ができました", options: { tag: "run-r1", data: { url: "/ui/?run=r1" } } });
    expect(await notifyFor({ type: "step.end" }, "r1", "x")).toBe(false);         // 知らせることがない
    expect(localStorage.getItem("notify")).toBe("true");
  });

  it("断られたら denied と出し、切ったら出さない", async () => {
    FakeNotification.requestPermission.mockImplementationOnce(async () => { permission = "denied"; return permission; });
    expect(await setNotify(true)).toBe("denied");
    expect(await setNotify(false)).toBe("off");
    setHidden(true);
    expect(await notifyFor({ type: "run.end" }, "r1", "x")).toBe(false);
  });

  it("設定の「表示」の切り替えで入れられる", async () => {
    document.body.innerHTML = `<div id="root"></div>`;
    const app = mount(DisplayTab, { target: document.getElementById("root")! });
    flushSync();
    const box = document.getElementById("notifyToggle") as HTMLInputElement;
    expect(box.checked).toBe(false);
    box.click();
    await vi.waitFor(() => { flushSync(); expect(document.body.textContent).toContain("画面が後ろにあるとき"); });
    expect(box.checked).toBe(true);
    // 許可の確認を閉じた（default のまま）: 切り替えは戻る
    box.click();
    await vi.waitFor(() => { flushSync(); expect(box.checked).toBe(false); });
    permission = "default";
    FakeNotification.requestPermission = vi.fn(async () => "default" as NotificationPermission);
    box.click();
    await vi.waitFor(() => { flushSync(); expect(FakeNotification.requestPermission).toHaveBeenCalled(); });
    await vi.waitFor(() => expect(box.checked).toBe(false));
    unmount(app);
  });
});
