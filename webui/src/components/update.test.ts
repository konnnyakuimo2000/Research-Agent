// 画面の新しい版の知らせ: 勝手に読み直さず、「再読み込み」で読み直す。「あとで」で消える
import { describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import UpdateNotice from "./UpdateNotice.svelte";
import { update } from "../stores/state.ts";

describe("新しい版の知らせ", () => {
  it("届くまでは出さず、届いたら再読み込みか、あとでを選べる", () => {
    document.body.innerHTML = `<div id="root"></div>`;
    update.set({ ready: false, apply: () => {} });
    const app = mount(UpdateNotice, { target: document.getElementById("root")! });
    flushSync();
    expect(document.querySelector(".update")).toBeNull();
    const apply = vi.fn();
    update.set({ ready: true, apply }); flushSync();
    expect(document.querySelector(".update")!.textContent).toContain("画面の新しい版があります");
    (document.querySelector(".update .primary") as HTMLButtonElement).click();
    expect(apply).toHaveBeenCalledOnce();
    [...document.querySelectorAll<HTMLButtonElement>(".update button")].find((b) => b.textContent === "あとで")!.click(); flushSync();
    expect(document.querySelector(".update")).toBeNull();
    unmount(app);
  });
});
