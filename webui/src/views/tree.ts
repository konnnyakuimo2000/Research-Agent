// 木（1 段）: 作業の流れを、左から右へ伸びる木として描く。
// - ノードは、親から枝が伸びた先に、実がなるように膨らんで現れる
// - 共通の描き手は、依頼 → 作業（step）を並べる。拡張（research/v1 など）は自分のイベントの描き手で、ノードを足す
import { $, esc, card, clamp, bezierTo, easeOut, easeInOut, grownScale, fmtPt, STEM, RIPEN, ARRIVE } from "../core/util.ts";
import type { Box, Point } from "../core/util.ts";
import { ctx, labelOf } from "../core/ctx.ts";
import { registerRenderer } from "../core/events.ts";
import { showDetails, onDetailsClose } from "../panels/details.ts";
import type { Details } from "../panels/details.ts";
import type { UIEvent } from "../types/api.ts";

const canvas = $("canvas"), svg = $("edges");
const COLW = [170, 250, 236, 236, 236];   // 深さごとの列の幅（依頼・判断・作業・ページ・データベースの記録）
const GAPX = 44, GAPY = 12, PAD = 24;

export interface TreeNode {
  id: string;
  parent: string | null;
  depth: number;
  el: HTMLElement;
  kids: string[];
  cur: Point;            // 今の位置（毎フレーム tgt へ近づく）
  tgt: Point;            // 並べた位置
  collapsed: boolean;
  hidden: boolean;
  extra: string[];       // ノードどうしの別の結び付き（転載など。点線で結ぶ）
  born: number;          // 生まれた時刻（performance.now()。すぐ描くときは過去）
  // 拡張が覚えさせる値（research/v1: 概要の URL・転送先・画像のライセンス）
  url?: string;
  final?: string;
  license?: string;
}
export interface Tree {
  name: "tree";
  el: HTMLElement;
  margin: number;
  N: Record<string, TreeNode>;
  order: string[];
  info: Record<string, Details>;
  selected: string | null;
  aim: string | null;      // いちばん新しいノード（カメラが追う）
  work: string | null;     // 今の作業のノード
  reset(): void;
  add(id: string, parent: string | null, depth: number, html: string, details?: Details, cls?: string, first?: boolean): HTMLElement;
  layout(): void;
  snap(): void;
  setState(id: string, st?: string): void;
  setSub(id: string, text: string): void;
  setTitle(id: string, text: string): void;
  step(id: string, i: number, cls: string): void;
  collapse(id: string, yes: boolean): void;
  reveal(id: string): void;
  select(id: string): void;
  box(id: string): Box | null;
  bounds(): { x0: number; y0: number; x1: number; y1: number } | null;
  aimBox(): Box | null;
  isEmpty(): boolean;
  draw(dt: number): void;
}

// 木の状態。拡張の描き手も、これを通してノードを足す
export const tree: Tree = {
  name: "tree", el: canvas, margin: 36,
  N: {}, order: [], info: {}, selected: null, aim: null, work: null,
  reset() {
    canvas.querySelectorAll(".node, .fly").forEach((e) => e.remove());
    svg.innerHTML = "";
    Object.assign(this, { N: {}, order: [], info: {}, selected: null, aim: null, work: null });
  },
  // ノードを足す（すでにあれば何もしない）。details は詳細の欄に出すもの
  add(id, parent, depth, html, details, cls = "", first = false) {
    const N = this.N;
    if (N[id]) return N[id].el;
    if (ctx.view === "tree") $("flowEmpty").hidden = true;
    const el = document.createElement("div");
    el.className = `node ${cls}`; el.style.setProperty("--w", COLW[Math.min(depth, COLW.length - 1)] + "px"); el.innerHTML = html;
    el.tabIndex = 0; el.setAttribute("role", "button");
    el.addEventListener("click", (e) => { if (!(e.target as Element).closest(".toggle")) this.select(id); });
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.select(id); } });
    const tg = el.querySelector(".toggle"); if (tg) tg.addEventListener("click", () => this.collapse(id, !N[id].collapsed));
    canvas.appendChild(el);
    const p = parent ? N[parent] : undefined;
    N[id] = { id, parent, depth, el, kids: [], cur: { x: 0, y: 0 }, tgt: { x: 0, y: 0 }, collapsed: false, hidden: false, extra: [],
              born: ctx.instant ? -1e9 : performance.now() };
    if (p) {
      if (first) p.kids.unshift(id); else p.kids.push(id);
      const t = p.el.querySelector<HTMLElement>(".toggle"); if (t) t.hidden = false;
    }
    this.order.push(id); this.info[id] = details || { title: id };
    this.layout();
    N[id].cur = { ...N[id].tgt };   // 生まれるノードは、その場所で大きくなる（動いてくるのではない）
    if (!ctx.instant) { el.style.opacity = "0"; el.style.transform = `translate(${N[id].cur.x}px, ${N[id].cur.y}px) scale(0.55)`; }
    this.aim = id;
    return el;
  },
  layout() {
    const N = this.N, root = N.req; if (!root || canvas.hidden) return;   // 非表示の間は高さが測れない
    const colX: number[] = []; let x = PAD; COLW.forEach((w, i) => { colX[i] = x; x += w + GAPX; });
    const col = (d: number) => colX[Math.min(d, colX.length - 1)];
    let y = PAD;
    function place(id: string, hide: boolean): void {
      const n = N[id]; n.hidden = hide; n.el.classList.toggle("gone", hide);
      const h = n.el.offsetHeight || 50;
      if (hide) { const p = N[n.parent!]; n.tgt = { x: p.tgt.x + 30, y: p.tgt.y }; n.kids.forEach((k) => place(k, true)); return; }
      const kids = n.collapsed ? [] : n.kids;
      if (!kids.length) { n.tgt = { x: col(n.depth), y }; y += h + GAPY; n.kids.forEach((k) => place(k, true)); return; }
      const top = y;
      kids.forEach((k) => place(k, false));
      const f = N[kids[0]], l = N[kids[kids.length - 1]];
      const mid = (f.tgt.y + l.tgt.y + (l.el.offsetHeight || 50)) / 2;
      n.tgt = { x: col(n.depth), y: Math.max(top, mid - h / 2) };
      if (n.tgt.y + h + GAPY > y) y = n.tgt.y + h + GAPY;
    }
    root.kids.forEach((k) => place(k, false));
    root.tgt = { x: colX[0], y: PAD };
    if (ctx.instant) this.order.forEach((id) => { N[id].cur = { ...N[id].tgt }; });
  },
  snap() { this.layout(); this.order.forEach((id) => { this.N[id].cur = { ...this.N[id].tgt }; }); },
  setState(id, st) {
    const n = this.N[id]; if (!n) return;
    n.el.classList.remove("planned", "running", "llm", "waiting", "done", "failed");
    if (st) n.el.classList.add(st);
  },
  setSub(id, text) { const s = this.N[id]?.el.querySelector(".s"); if (s) s.textContent = text; },
  setTitle(id, text) { const s = this.N[id]?.el.querySelector(".t"); if (s) s.textContent = text; },
  step(id, i, cls) { const s = this.N[id]?.el.querySelectorAll(".steps span")[i]; if (s) s.className = cls; },
  collapse(id, yes) {
    const n = this.N[id]; if (!n) return; n.collapsed = yes;
    const t = n.el.querySelector(".toggle"); if (t) t.textContent = yes ? `${n.kids.length} 件 ▸` : "畳む ▾";
    this.layout();
  },
  reveal(id) { let p = this.N[id]?.parent; while (p) { if (this.N[p].collapsed) this.collapse(p, false); p = this.N[p].parent; } },
  select(id) {
    if (this.selected && this.N[this.selected]) this.N[this.selected].el.classList.remove("sel");
    this.selected = id; this.N[id].el.classList.add("sel");
    showDetails(this.info[id] || { title: id });
  },
  box(id) { const n = this.N[id]; return n ? { x: n.tgt.x, y: n.tgt.y, w: n.el.offsetWidth, h: n.el.offsetHeight } : null; },
  bounds() {
    const vis = this.order.filter((id) => !this.N[id].hidden);
    if (!vis.length) return null;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const id of vis) {
      const n = this.N[id];
      x0 = Math.min(x0, n.tgt.x); y0 = Math.min(y0, n.tgt.y);
      x1 = Math.max(x1, n.tgt.x + n.el.offsetWidth); y1 = Math.max(y1, n.tgt.y + n.el.offsetHeight);
    }
    return { x0, y0, x1, y1 };
  },
  aimBox() {
    const n = this.aim ? this.N[this.aim] : undefined;
    return n ? { x: n.tgt.x, y: n.tgt.y, w: COLW[Math.min(n.depth, COLW.length - 1)], h: n.el.offsetHeight } : null;
  },
  isEmpty() { return this.order.length === 0; },
  // 毎フレーム: ノードを目標の位置へ近づけ、生まれたばかりのノードは枝を伸ばしてから膨らませ、線を描く
  draw(dt) {
    const N = this.N, t = performance.now();
    const k = ctx.instant ? 1 : 1 - Math.pow(0.0008, dt);
    for (const id of this.order) {
      const n = N[id];
      n.cur.x += (n.tgt.x - n.cur.x) * k; n.cur.y += (n.tgt.y - n.cur.y) * k;
      const age = (t - n.born) / 1000;
      const g = n.parent ? clamp((age - ARRIVE) / RIPEN) : clamp(age / RIPEN);
      n.el.style.transform = `translate(${n.cur.x.toFixed(1)}px, ${n.cur.y.toFixed(1)}px) scale(${grownScale(g).toFixed(3)})`;
      n.el.style.opacity = n.hidden ? "0" : easeOut(g).toFixed(3);
    }
    let d = ""; const live: string[] = [], growing: string[] = [], buds: Point[] = [], rep: string[] = [];
    const anchorY = (n: TreeNode) => n.cur.y + Math.min(22, n.el.offsetHeight / 2);
    for (const id of this.order) {
      const n = N[id]; if (!n.parent || n.hidden) continue;
      const p = N[n.parent];
      const p0 = { x: p.cur.x + p.el.offsetWidth, y: anchorY(p) }, p3 = { x: n.cur.x, y: anchorY(n) };
      const mx = p0.x + (p3.x - p0.x) / 2;
      const age = (t - n.born) / 1000, e = clamp(age / STEM);
      const [a, b, c, q] = e >= 1 ? [p0, { x: mx, y: p0.y }, { x: mx, y: p3.y }, p3] : bezierTo(p0, { x: mx, y: p0.y }, { x: mx, y: p3.y }, p3, easeInOut(e));
      const path = `M${fmtPt(a)} C${fmtPt(b)} ${fmtPt(c)} ${fmtPt(q)}`;
      if (e < 1 || age < ARRIVE + RIPEN * 0.5) { growing.push(path); if (e < 1) buds.push(q); }
      else if (n.el.classList.contains("running") || n.el.classList.contains("llm")) live.push(path);
      else d += path;
      for (const other of n.extra) {   // ノードどうしの別の結び付き（転載など）を点線で結ぶ
        const o = N[other]; if (!o || o.hidden) continue;
        const s0 = { x: n.cur.x + n.el.offsetWidth, y: anchorY(n) + 8 }, s3 = { x: o.cur.x + o.el.offsetWidth, y: anchorY(o) + 8 };
        rep.push(`M${fmtPt(s0)} C${fmtPt({ x: s0.x + 60, y: s0.y })} ${fmtPt({ x: s3.x + 60, y: s3.y })} ${fmtPt(s3)}`);
      }
    }
    svg.innerHTML = `<path d="${d}"/>` + live.map((p) => `<path class="live" d="${p}"/>`).join("")
      + growing.map((p) => `<path class="grow" d="${p}"/>`).join("") + buds.map((q) => `<circle class="pen" cx="${q.x.toFixed(1)}" cy="${q.y.toFixed(1)}" r="2.6"/>`).join("")
      + rep.map((p) => `<path class="rep" d="${p}"/>`).join("");
  },
};
onDetailsClose(() => { if (tree.selected && tree.N[tree.selected]) tree.N[tree.selected].el.classList.remove("sel"); tree.selected = null; });

// ---- 共通の描き手（0・1 段のイベント） ----
const stepId = (e: UIEvent) => "w" + e.step;
/** 今の作業のノード（まだなければ null） */
const work = (): string | null => (tree.work && tree.N[tree.work] ? tree.work : null);
registerRenderer("run.start", (e) => {
  tree.add("req", null, 0, `<div class="k"><span>依頼</span><span class="st"></span></div><div class="t">${esc(e.request)}</div><div class="s">${esc(ctx.runId)}</div>`,
    { title: "依頼", kv: [["依頼文", esc(e.request)], ["実行の ID", `<span class="mono">${esc(ctx.runId)}</span>`]] }, "root");
});
// 作業（step）は依頼の下に並べる。名前はエージェントの名乗り（graph）から
registerRenderer("step.start", (e) => {
  const id = stepId(e), label = labelOf(e.node);
  if (tree.N[id]) { tree.setState(id, "running"); return; }   // 答えで再開した
  tree.work = id;
  tree.add(id, "req", 1, card(label, label, "始めた"), { title: label, kv: [["ノード", `<span class="mono">${esc(e.node)}</span>`]] }, "running");
});
registerRenderer("step.end", (e) => {
  const id = stepId(e);
  if (e.error) { tree.setState(id, "failed"); tree.setSub(id, e.error); }
  else if (!tree.N[id]?.el.classList.contains("failed")) { tree.setState(id, "done"); tree.setSub(id, "終わった"); }
  if (tree.info[id]) tree.info[id].kv = [...(tree.info[id].kv || []), ["所要時間", `${e.seconds} 秒${e.llm_wait ? `（うち LLM サーバーの待ち ${e.llm_wait} 秒）` : ""}`]];
});
registerRenderer("status", (e) => { const w = work(); if (w) { tree.setSub(w, e.text); (tree.info[w].list ||= []).push(esc(e.text)); } });
registerRenderer("question", (e) => {
  const w = work(); if (w) { tree.setState(w, "waiting"); tree.setTitle(w, "利用者の答えを待っている"); tree.setSub(w, e.question.split("\n")[0]); }
});
registerRenderer("run.end", () => tree.setState("req", "done"));
registerRenderer("run.error", (e) => { const w = work(); if (w) { tree.setState(w, "failed"); tree.setSub(w, e.error); } });
registerRenderer("run.cancelled", () => { const w = work(); if (w) { tree.setState(w, "failed"); tree.setSub(w, "止めた"); } });
// 0 段だけのイベント（文の続き・答えなど）は、木には描かない
["message.delta", "answer", "run.waiting", "run.paused", "run.resumed", "report"].forEach((t) => registerRenderer(t, () => {}));
// 描き手のないイベント（知らない拡張・新しい版）: そのときの作業の詳細に中身を残す（画面は止まらない）
registerRenderer("*", (e) => {
  const id = work() || (tree.N.req ? "req" : null);
  if (id) (tree.info[id].events ||= []).push(e);
});
