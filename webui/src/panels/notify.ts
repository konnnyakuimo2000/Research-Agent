// 調査の知らせ（PWA 第 1 段）。開いている調査がライブで、答えを待った・終わった・止まったときに OS の通知を出す。
// - 画面が前に出ているときは出さない（見えているので）。履歴の再生では出さない
// - 設定のパネルの「表示」で入れる。入れたときにブラウザに許可を求める。入れたかどうかはこのブラウザに覚える
// - 通知は Service Worker から出す（Android の Chrome は、ページから new Notification() を出せない）。押すと画面に戻る
//   （public/sw-notify.js）。Service Worker がないとき（開発中など）はページから出す
// - 通知は HTTPS か localhost でしか使えない
import { writable } from "svelte/store";
import { store } from "../core/api.ts";
import type { UIEvent } from "../types/api.ts";

const KEY = "notify";
const ICON = "/ui/icons/icon-192.png";
export type NotifyState = "on" | "off" | "denied" | "unsupported";

export const supported = (): boolean => typeof window !== "undefined" && "Notification" in window && window.isSecureContext !== false;
function initial(): NotifyState {
  if (!supported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  return store.get<boolean>(KEY, false) && Notification.permission === "granted" ? "on" : "off";
}
export const notifyState = writable<NotifyState>(initial());

/** 知らせを入れる・切る。入れるときは許可を求める（断られたら denied） */
export async function setNotify(on: boolean): Promise<NotifyState> {
  let next: NotifyState = "off";
  if (!supported()) next = "unsupported";
  else if (on) {
    const p = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
    next = p === "granted" ? "on" : p === "denied" ? "denied" : "off";
  }
  store.set(KEY, next === "on");
  notifyState.set(next);
  return next;
}

let state: NotifyState = "off";
notifyState.subscribe((v) => { state = v; });

/** そのイベントで知らせること（なければ null）。request は依頼文 */
export function messageFor(e: UIEvent, request: string): { title: string; body: string } | null {
  const what = request ? `「${request.length > 40 ? request.slice(0, 40) + "…" : request}」` : "調査";
  switch (e.type) {
    case "run.waiting": return { title: "答えを待っています", body: `${what}: ${String(e.question || "").split("\n")[0]}` };
    case "run.end": return { title: "報告ができました", body: what };
    case "run.error": return { title: "エラーで止まりました", body: `${what}: ${e.error || ""}` };
    default: return null;
  }
}

/** ライブのイベントを受け取ったときに呼ぶ（app.ts）。runId は開いている依頼 */
export async function notifyFor(e: UIEvent, runId: string | null, request: string): Promise<boolean> {
  if (state !== "on" || !runId) return false;
  if (!document.hidden && document.hasFocus()) return false;           // 見えているので出さない
  const m = messageFor(e, request);
  if (!m) return false;
  const options: NotificationOptions = { body: m.body, tag: `run-${runId}`, icon: ICON, data: { url: `/ui/?run=${encodeURIComponent(runId)}` } };
  const reg = await navigator.serviceWorker?.getRegistration?.("/ui/");
  if (reg) await reg.showNotification(m.title, options);
  else { const n = new Notification(m.title, options); n.onclick = () => { window.focus(); n.close(); }; }
  return true;
}
