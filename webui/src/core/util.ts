// 画面のどこでも使う小さな道具（文字の安全な埋め込み・URL・ノードの見た目の部品・線の曲線と動き）
import { apiUrl, withKey } from "./api.ts";
import { ctx } from "./ctx.ts";

export interface Point { x: number; y: number }
export interface Box { x: number; y: number; w: number; h: number }
/** 3 次ベジェ曲線の 4 点（始点・制御点 2 つ・終点） */
export type Curve = [Point, Point, Point, Point];

/** id で要素を取る。画面の骨組み（index.html）にある要素だけに使う */
export const $ = <T extends HTMLElement = HTMLElement>(id: string): T => document.getElementById(id) as T;
export const esc = (s: unknown): string => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" } as Record<string, string>)[c]);
export const host = (u: string): string => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return u; } };
export const clamp = (v: number, a = 0, b = 1): number => Math.min(b, Math.max(a, v));
export const tier = (t?: number | null): string => t ? `<span class="tier${t === 1 ? " t1" : ""}">T${t}</span>` : "";
export const link = (u: string): string => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer" class="mono">${esc(u)}</a>`;
// 同じ資料を指す URL の表記ゆれをそろえた形（サーバーの tiers.canonical と同じ決まり）。
// #以降・;jsessionid=…・追跡用のパラメータ（utm_* など）だけを外し、ほかのクエリは残す。
// 前はクエリを丸ごと捨てていたので、?id=1 と ?id=2 を同じ資料とみなし、2 番の出典で 1 番の資料を開いていた
const TRACKING = /^(srsltid|utm_\w+|gclid|fbclid|yclid|msclkid|mc_cid|mc_eid)$/i;
export function canonicalUrl(u: string): string {
  try {
    const x = new URL(u);
    let path = x.pathname.replace(/;jsessionid=[^/?]*/i, "");
    try { path = decodeURI(path); } catch (e) { /* 壊れた符号はそのまま */ }   // %C3%BC と ü をそろえる（%2F は残る）
    const q = new URLSearchParams([...x.searchParams].filter(([k]) => !TRACKING.test(k))).toString();
    return `${x.protocol}//${x.host}${path}${q ? "?" + q : ""}`;
  } catch (e) { return String(u || ""); }
}
export const sameUrl = (a: string, b: string): boolean => canonicalUrl(a) === canonicalUrl(b);
// サイトのアイコン（取れなければ頭文字）
export function icon(h: string): string {
  const letter = esc((h || "?").replace(/^www\./, "")[0]?.toUpperCase() || "?");
  return `<img class="ico" src="${apiUrl(`/api/favicon?host=${encodeURIComponent(h)}`)}" alt="" onerror="this.outerHTML='<span class=&quot;ico letter&quot;>${letter}</span>'">`;
}
// ゲートウェイが配る、その依頼の画像
export const imageUrl = (src: string): string => withKey(`/api/image?run=${encodeURIComponent(ctx.runId ?? "")}&src=${encodeURIComponent(src)}`);
// 木のノードの中身（種類・題・一行）
export const card = (kind: string, title: unknown, sub: unknown, right = "", lead = ""): string =>
  `<div class="k"><span>${kind}</span><span class="row">${right}<span class="st"></span></span></div><div class="row">${lead}<span class="t">${esc(title)}</span></div><div class="s">${esc(sub)}</div>`;
export const toggleBtn = `<button type="button" class="toggle" hidden>畳む ▾</button>`;

// 動き: 線を鉛筆で描く秒数、ノードが大きくなる秒数。線がノードの位置に届くころに、ノードが現れ始める
export const STEM = 1.1, RIPEN = 0.9, ARRIVE = STEM * 0.92;
// 3 次ベジェ曲線を途中（t）までで切る（枝が伸びていく途中を描く）
export function bezierTo(p0: Point, p1: Point, p2: Point, p3: Point, t: number): Curve {
  const L = (a: Point, b: Point): Point => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
  const a = L(p0, p1), b = L(p1, p2), c = L(p2, p3), d = L(a, b), e = L(b, c), f = L(d, e);
  return [p0, a, d, f];
}
export function bezierPoint(p0: Point, p1: Point, p2: Point, p3: Point, t: number): Point {
  const u = 1 - t;
  return { x: u * u * u * p0.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t * t * t * p3.x,
           y: u * u * u * p0.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t * t * t * p3.y };
}
export const easeOut = (t: number): number => 1 - Math.pow(1 - t, 3);
export const easeInOut = (t: number): number => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;   // ゆっくり始まり、ゆっくり終わる（鉛筆の線）
// ノードは小さめの大きさから、行き過ぎずに滑らかに大きくなる。現れ方も同じ速さで
export const grownScale = (g: number): number => g >= 1 ? 1 : 0.55 + 0.45 * easeOut(g);
export const fmtPt = (p: Point): string => `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
