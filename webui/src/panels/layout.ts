// ペインの幅（.claude/ui-gateway-settings-plan.md「ペインの幅と狭い画面」）。
// 履歴とチャット、チャットと作業記録の間の境界を、ドラッグか矢印キー（Shift で大きく・Home で既定）で動かす。
// 幅はこのブラウザに覚え、窓を狭めたら今の幅に収める。幅を変えたら onChange（図の並べ直し。見ている対象と拡大は保つ）。
// 3 列が入らない幅では履歴を引き出しにし（style.css）、さらに狭い画面では「チャット」と「作業記録」を切り替えて全面に出す
import { $, clamp } from "../core/util.ts";
import { store } from "../core/api.ts";

const KEY = "paneWidths";
type Key = "side" | "chat";
type Widths = Record<Key, number | null>;
export const LIMITS: Record<Key, [number, number]> = { side: [160, 420], chat: [280, 760] };
const TELE_MIN = 360;                 // 作業記録に残す最小の幅
const app = () => document.querySelector<HTMLElement>(".app")!;
let widths: Widths = { side: null, chat: null }, onChange: (snap?: boolean) => void = () => {};

const stacked = () => window.matchMedia("(max-width: 820px)").matches;     // チャットと作業記録を切り替える幅
const drawerSide = () => window.matchMedia("(max-width: 1180px)").matches; // 履歴が引き出しになる幅

// 今の窓に収まる幅（窓より広い幅は詰める）
export function fitWidths(w: Widths, total: number): Widths {
  const side = w.side == null ? null : clamp(w.side, ...LIMITS.side);
  const sideNow = drawerSide() ? 0 : side ?? 220;
  const maxChat = Math.max(LIMITS.chat[0], Math.min(LIMITS.chat[1], total - sideNow - TELE_MIN));
  const chat = w.chat == null ? null : clamp(w.chat, LIMITS.chat[0], maxChat);
  return { side, chat };
}
function apply() {
  const el = app(), fitted = fitWidths(widths, window.innerWidth);
  if (fitted.side == null) el.style.removeProperty("--side-w"); else el.style.setProperty("--side-w", fitted.side + "px");
  if (fitted.chat == null) el.style.removeProperty("--chat-w"); else el.style.setProperty("--chat-w", fitted.chat + "px");
  for (const [id, key] of [["splitSide", "side"], ["splitChat", "chat"]] as [string, Key][]) {
    const s = $(id), now = key === "side" ? $("runs").closest(".side")!.getBoundingClientRect().width : $("chatpane").getBoundingClientRect().width;
    s.setAttribute("aria-valuenow", String(Math.round(fitted[key] ?? now)));
    s.setAttribute("aria-valuemin", String(LIMITS[key][0])); s.setAttribute("aria-valuemax", String(LIMITS[key][1]));
  }
}
function set(key: Key, value: number | null): void {
  widths = { ...widths, [key]: value == null ? null : Math.round(value) };
  widths = { ...widths, ...fitWidths(widths, window.innerWidth) };
  store.set(KEY, widths); apply(); onChange();
}
export function resetWidths(): void { widths = { side: null, chat: null }; store.set(KEY, widths); apply(); onChange(); }
export function widthsText(): string {
  const f = fitWidths(widths, window.innerWidth);
  return `履歴 ${f.side ?? "既定"}${f.side ? "px" : ""} · チャット ${f.chat ?? "既定"}${f.chat ? "px" : ""}`;
}

function wire(id: string, key: Key, edge: () => number): void {
  const s = $(id);
  s.addEventListener("pointerdown", (e) => {
    e.preventDefault(); s.setPointerCapture?.(e.pointerId); s.classList.add("drag");
    const move = (ev: PointerEvent) => set(key, ev.clientX - edge());
    const up = () => { s.classList.remove("drag"); s.removeEventListener("pointermove", move); s.removeEventListener("pointerup", up); s.removeEventListener("pointercancel", up); };
    s.addEventListener("pointermove", move); s.addEventListener("pointerup", up); s.addEventListener("pointercancel", up);
  });
  s.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 48 : 16, cur = +(s.getAttribute("aria-valuenow") || 0);
    if (e.key === "ArrowLeft") set(key, cur - step);
    else if (e.key === "ArrowRight") set(key, cur + step);
    else if (e.key === "Home") set(key, null);
    else return;
    e.preventDefault();
  });
}

// 狭い画面: チャットと作業記録の切り替え（どちらも消さずに隠すだけなので、入力中の文と購読はそのまま）
export function showPane(which: string): void {
  app().classList.toggle("show-tele", which === "tele");
  document.querySelectorAll<HTMLElement>("#paneTabs [data-pane]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.pane === which)));
  onChange(true);
}

export function initLayout(changed: (snap?: boolean) => void) {
  onChange = changed;
  const w = store.get<Partial<Widths>>(KEY, null);
  if (w && typeof w === "object") widths = { side: w.side ?? null, chat: w.chat ?? null };
  wire("splitSide", "side", () => app().getBoundingClientRect().left);
  wire("splitChat", "chat", () => $("chatpane").getBoundingClientRect().left);
  document.querySelectorAll<HTMLElement>("#paneTabs [data-pane]").forEach((b) => b.addEventListener("click", () => showPane(b.dataset.pane!)));
  window.addEventListener("resize", apply);
  apply();
  return { stacked, drawerSide };
}
