// 画面のどこでも使う小さな道具（文字の安全な埋め込み・URL・ノードの見た目の部品・線の曲線と動き）
import { withKey } from "./api.js";
import { ctx } from "./ctx.js";

export const $ = (id) => document.getElementById(id);
export const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
export const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return u; } };
export const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
export const tier = (t) => t ? `<span class="tier${t === 1 ? " t1" : ""}">T${t}</span>` : "";
export const link = (u) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer" class="mono">${esc(u)}</a>`;
export function sameUrl(a, b) { const n = (u) => u.replace(/[?#].*$/, "").replace(/\/$/, ""); return n(a) === n(b); }
// サイトのアイコン（取れなければ頭文字）
export function icon(h) {
  const letter = esc((h || "?").replace(/^www\./, "")[0]?.toUpperCase() || "?");
  return `<img class="ico" src="/api/favicon?host=${encodeURIComponent(h)}" alt="" onerror="this.outerHTML='<span class=&quot;ico letter&quot;>${letter}</span>'">`;
}
// ゲートウェイが配る、その依頼の画像
export const imageUrl = (src) => withKey(`/api/image?run=${encodeURIComponent(ctx.runId)}&src=${encodeURIComponent(src)}`);
// 木のノードの中身（種類・題・一行）
export const card = (kind, title, sub, right = "", lead = "") =>
  `<div class="k"><span>${kind}</span><span class="row">${right}<span class="st"></span></span></div><div class="row">${lead}<span class="t">${esc(title)}</span></div><div class="s">${esc(sub)}</div>`;
export const toggleBtn = `<button type="button" class="toggle" hidden>畳む ▾</button>`;

// 動き: 線を鉛筆で描く秒数、ノードが大きくなる秒数。線がノードの位置に届くころに、ノードが現れ始める
export const STEM = 1.1, RIPEN = 0.9, ARRIVE = STEM * 0.92;
// 3 次ベジェ曲線を途中（t）までで切る（枝が伸びていく途中を描く）
export function bezierTo(p0, p1, p2, p3, t) {
  const L = (a, b) => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
  const a = L(p0, p1), b = L(p1, p2), c = L(p2, p3), d = L(a, b), e = L(b, c), f = L(d, e);
  return [p0, a, d, f];
}
export function bezierPoint(p0, p1, p2, p3, t) {
  const u = 1 - t;
  return { x: u * u * u * p0.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t * t * t * p3.x,
           y: u * u * u * p0.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t * t * t * p3.y };
}
export const easeOut = (t) => 1 - Math.pow(1 - t, 3);
export const easeInOut = (t) => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;   // ゆっくり始まり、ゆっくり終わる（鉛筆の線）
// ノードは小さめの大きさから、行き過ぎずに滑らかに大きくなる。現れ方も同じ速さで
export const grownScale = (g) => g >= 1 ? 1 : 0.55 + 0.45 * easeOut(g);
export const fmtPt = (p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
