// カメラ（拡大・縮小・移動）。見せている図（流れの図・木）が bounds() で中身の範囲を返す。
// 図が広がると、全体が収まるよう滑らかに縮小する。ホイール・ピンチで拡大、ドラッグで移動（そのときは自動をやめる）
import { $, clamp } from "./util.ts";
import type { Box } from "./util.ts";
import { ctx, wake } from "./ctx.ts";

/** 見せている図（流れの図・木） */
export interface CameraView {
  el: HTMLElement;
  margin?: number;
  bounds(): { x0: number; y0: number; x1: number; y1: number } | null;
  aimBox?(): Box | null;
}

const MIN_ZOOM = 0.22, MAX_ZOOM = 2.5, FIT_MAX = 1;
const flow = $("flow");
let cam = { x: 0, y: 0, s: 1 }, camT = { ...cam }, auto = true;
let view: CameraView | null = null;

export function useView(v: CameraView): void { view = v; }
export function fitMode(on: boolean): void { auto = on; wake(); $("zoomFit").setAttribute("aria-pressed", String(on)); }
export function resetCamera(): void { fitMode(true); cam = { x: 0, y: 0, s: 1 }; camT = { ...cam }; }
// 全体に合わせる目標を決める。すぐ合わせるなら snap
export function fitTarget(snap = false): void {
  const b = view?.bounds(); if (!b) return;
  const { x0, y0, x1, y1 } = b;
  const W = flow.clientWidth, H = flow.clientHeight, m = view!.margin ?? 36, bottom = 64;
  const bw = x1 - x0, bh = y1 - y0;
  const s = clamp(Math.min((W - 2 * m) / bw, (H - m - bottom) / bh), MIN_ZOOM, FIT_MAX);
  let x = (W - bw * s) / 2 - x0 * s, y = m + Math.max(0, (H - m - bottom - bh * s) / 2) - y0 * s;
  // 縮小しきっても収まらないときは、いちばん新しいノードのあたりを見せる
  const aim = view!.aimBox?.();
  if (aim && bw * s > W - 2 * m) x = clamp(W * 0.62 - (aim.x + aim.w) * s, W - 2 * m - x1 * s, m - x0 * s);
  if (aim && bh * s > H - m - bottom) y = clamp(H * 0.5 - aim.y * s, H - bottom - y1 * s, m - y0 * s);
  camT = { x, y, s };
  if (snap) cam = { ...camT };
}
function zoomAt(mx: number, my: number, factor: number): void {
  fitMode(false); wake(1.5);
  const s = clamp(camT.s * factor, 0.05, MAX_ZOOM);   // 自分で縮小するときは、自動より小さくできる
  camT = { x: mx - (mx - camT.x) * (s / camT.s), y: my - (my - camT.y) * (s / camT.s), s };
}
// そのノード（図の中の箱）を見せる
export function jumpTo(box: Box): void {
  fitMode(false); wake(1.5);
  const s = Math.max(camT.s, 0.95);
  camT = { s, x: flow.clientWidth * 0.4 - (box.x + box.w / 2) * s, y: flow.clientHeight * 0.45 - (box.y + box.h / 2) * s };
}
// 毎フレーム: 目標へ近づける
export function stepCamera(dt: number): void {
  if (!view) return;
  if (auto) fitTarget();
  const kc = ctx.instant ? 1 : 1 - Math.pow(0.02, dt);
  cam.x += (camT.x - cam.x) * kc; cam.y += (camT.y - cam.y) * kc; cam.s += (camT.s - cam.s) * kc;
  view.el.style.transform = `translate(${cam.x.toFixed(1)}px, ${cam.y.toFixed(1)}px) scale(${cam.s.toFixed(4)})`;
}

flow.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = flow.getBoundingClientRect();
  zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0018)));
}, { passive: false });
// 移動: ノードの上からでも、少し動かせばドラッグにする（図が大きいと、窓がノードで埋まってつかむ場所がなくなるため）。
// 動かさずに離したら、これまでどおりノードを押したことになる。
// マウスで押し始めるたびに、取り逃した「離した」の記録を消す（残っていると 2 本指のピンチと見なされ、動かせなくなる）
const pointers = new Map<number, { x: number; y: number }>();
let pinch: { d: number } | null = null, downAt = { x: 0, y: 0 }, dragged = false;
flow.addEventListener("pointerdown", (e) => {
  if (e.button > 0 || (e.target as Element).closest(".hud, .drawer, .toggle, .pchip, a")) return;
  if (e.pointerType === "mouse") pointers.clear();
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pointers.size === 1) { downAt = { x: e.clientX, y: e.clientY }; dragged = false; }
  if (pointers.size === 2) { const [a, b] = [...pointers.values()]; pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) }; dragged = true; }
});
flow.addEventListener("pointermove", (e) => {
  const prev = pointers.get(e.pointerId); if (!prev) return;
  if (e.pointerType === "mouse" && e.buttons === 0) { pointers.clear(); flow.classList.remove("dragging"); return; }   // 離したのに届かなかった
  const now = { x: e.clientX, y: e.clientY }; pointers.set(e.pointerId, now);
  if (pointers.size === 1) {
    if (!dragged) {
      if (Math.hypot(now.x - downAt.x, now.y - downAt.y) < 5) return;
      dragged = true; fitMode(false); flow.classList.add("dragging");
      try { flow.setPointerCapture(e.pointerId); } catch (err) { /* 取れなくても動かせる */ }
    }
    const dx = now.x - prev.x, dy = now.y - prev.y;
    camT.x += dx; camT.y += dy; cam.x += dx; cam.y += dy;   // ドラッグは遅れなく動かす
    wake(0.5);
  } else if (pointers.size === 2 && pinch) {
    const [a, b] = [...pointers.values()], d = Math.hypot(a.x - b.x, a.y - b.y), r = flow.getBoundingClientRect();
    zoomAt((a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top, d / pinch.d); pinch.d = d;
  }
});
const endPointer = (e: PointerEvent) => { pointers.delete(e.pointerId); if (pointers.size < 2) pinch = null; if (!pointers.size) flow.classList.remove("dragging"); };
(["pointerup", "pointercancel", "lostpointercapture"] as const).forEach((t) => flow.addEventListener(t, endPointer));
window.addEventListener("blur", () => { pointers.clear(); flow.classList.remove("dragging"); });
// ドラッグした後の「押した」は、ノードの選択にしない
flow.addEventListener("click", (e) => { if (dragged) { e.stopPropagation(); e.preventDefault(); dragged = false; } }, true);
$("zoomIn").addEventListener("click", () => zoomAt(flow.clientWidth / 2, flow.clientHeight / 2, 1.3));
$("zoomOut").addEventListener("click", () => zoomAt(flow.clientWidth / 2, flow.clientHeight / 2, 1 / 1.3));
$("zoomFit").addEventListener("click", () => fitMode(true));
