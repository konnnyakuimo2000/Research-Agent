// 流れの図（1 段）。ノードと線は、イベントが起きたときに初めて生まれる（線が伸びてから、ノードが実るように膨らむ）。
// 線のエフェクト: 制御の流れ（作業から作業へ）は尾を引く点、情報の流れは線を流れる粒。
//
// FlowBase は、ノード・線・点・粒の描き方の土台。並べ方（layout）と線の形（geometry）とイベントの読み方（on）を
// 差し替えて使う:
// - GraphFlow（この下）: エージェントの名乗りの graph を、左から右への層に並べる一般的な並べ方。
//   ノードの種類は step・data・terminal だけを知っている。エージェントがノードを足しても、画面を直さずに出る
// - 拡張の並べ方（例: extensions/research-v1/flow.js の、判断を中心に作業が並ぶ図）
import { $, esc, clamp, bezierTo, bezierPoint, easeOut, easeInOut, grownScale, fmtPt, STEM, RIPEN, ARRIVE } from "../core/util.js";
import { ctx, labelOf, manifest } from "../core/ctx.js";
import { nodeKind, registerNodeKind } from "../core/events.js";
import { showDetails } from "../panels/details.js";

const graphEl = $("graph"), gsvg = $("gedges");
const SVGNS = "http://www.w3.org/2000/svg";

export class FlowBase {
  constructor() { this.name = "flow"; this.el = graphEl; this.margin = 56; this.reset(); }
  reset() {
    graphEl.querySelectorAll(".fnode").forEach((e) => e.remove());
    gsvg.innerHTML = "";
    this.fx = document.createElementNS(SVGNS, "g"); gsvg.appendChild(this.fx);   // 点と粒（毎フレーム描き直す）
    this.n = {}; this.e = {}; this.tokens = []; this.particles = []; this.cur = null; this.lastFrom = null;
    this.visits = {}; this.seconds = {};
  }
  // ---- 差し替えるところ ----
  def(id) { return { w: 220, label: id, cls: "" }; }     // ノードの幅・名前・class
  body(id) { return `<ul class="hist"></ul>`; }          // ノードの中身（題・一行の下）
  layout() {}
  geometry(e) {
    const a = this.boxOf(e.a), b = this.boxOf(e.b);
    const anchor = (n) => n.y + Math.min(24, n.h / 2);
    const p0 = { x: a.x + a.w, y: anchor(a) }, p3 = { x: b.x, y: anchor(b) };
    const mx = (p0.x + p3.x) / 2;
    return [p0, { x: mx, y: p0.y }, { x: mx, y: p3.y }, p3];
  }
  labelled(a, b, kind) { return kind !== "back" && kind !== "data"; }   // 通った回数を書く線か
  isHot(e) {
    const target = this.n[e.b];
    return this.cur === e.b && this.lastFrom === e.a && ["running", "llm", "waiting"].some((c) => target.el.classList.contains(c));
  }
  beforeDraw() {}
  extraFx() { return ""; }
  on() {}
  // ---- 土台 ----
  boxOf(id) { const n = this.n[id]; return { x: n.cur.x, y: n.cur.y, w: n.el.offsetWidth, h: n.el.offsetHeight }; }
  // ノードを（なければ）作る。生まれたノードは、その場所で実るように膨らむ
  node(id) {
    if (this.n[id]) return this.n[id];
    const d = this.def(id);
    const el = document.createElement("div");
    el.className = `fnode ${d.cls || ""}`; el.style.setProperty("--w", d.w + "px");
    el.innerHTML = `<div class="k"><span>${esc(d.label)}</span><span class="row"><span class="cnt"></span><span class="st"></span></span></div>
      <div class="t"></div><div class="s"></div>${this.body(id, d)}`;
    el.tabIndex = 0; el.setAttribute("role", "button");
    el.addEventListener("click", (ev) => { if (!ev.target.closest(".pchip")) this.select(id); });
    el.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); this.select(id); } });
    graphEl.appendChild(el);
    const n = this.n[id] = { id, el, ...d, hist: [], cur: { x: 0, y: 0 }, tgt: { x: 0, y: 0 }, born: ctx.instant ? -1e9 : performance.now() };
    this.visits[id] = 0; this.seconds[id] = 0;
    this.layout();
    n.cur = { ...n.tgt };
    if (!ctx.instant) { el.style.opacity = "0"; el.style.transform = `translate(${n.cur.x}px, ${n.cur.y}px) scale(0.55)`; }
    if (ctx.view === "flow") $("flowEmpty").hidden = true;
    return n;
  }
  // 線を（なければ）作る。生まれた線は、元のノードから伸びていく
  edge(a, b, kind = "") {
    const key = `${a}>${b}`;
    if (this.e[key]) return this.e[key];
    this.node(a); this.node(b);
    const path = document.createElementNS(SVGNS, "path");
    gsvg.insertBefore(path, this.fx);
    let label = null;
    if (this.labelled(a, b, kind)) {
      label = document.createElementNS(SVGNS, "text"); label.setAttribute("class", "glabel"); label.setAttribute("text-anchor", "middle");
      gsvg.insertBefore(label, this.fx);
    }
    const target = this.n[b];
    if (performance.now() - target.born < 150) target.delay = ARRIVE;   // 線が届いてから現れる
    return (this.e[key] = { a, b, kind, path, label, count: 0, over: 0, born: ctx.instant ? -1e9 : performance.now() });
  }
  removeEdge(key) { const e = this.e[key]; if (!e) return; e.path.remove(); e.label?.remove(); delete this.e[key]; }
  setText(id, t, s) { const n = this.n[id]; if (!n) return; if (t !== undefined) n.el.querySelector(".t").textContent = t; if (s !== undefined) n.el.querySelector(".s").textContent = s; }
  setState(id, st) {
    const n = this.n[id]; if (!n) return;
    n.el.classList.remove("running", "llm", "waiting", "done", "failed");
    n.el.classList.add(st);
  }
  push(id, line) {   // ノードの中の最近の出来事（新しいものを上に、3 つまで）。すべては詳細に残す
    const n = this.n[id]; if (!n) return;
    n.hist.unshift(line);
    const ul = n.el.querySelector(".hist"); if (ul) ul.innerHTML = n.hist.slice(0, 3).map((x) => `<li>${esc(x)}</li>`).join("");
    this.layout();
  }
  count(id) {
    const n = this.n[id]; if (!n) return;
    n.el.querySelector(".cnt").textContent = `×${this.visits[id]}${this.seconds[id] ? ` · ${Math.round(this.seconds[id])} 秒` : ""}`;
  }
  // 作業が移る: 線を（なければ）作り、尾を引く点を流して回数を数える
  go(to, { back = false, state = "running" } = {}) {
    const from = this.cur;
    this.node(to);
    if (from && from !== to) {
      const e = this.edge(from, to, back ? "back" : "");
      e.count++;
      this.tokens.push({ e, t0: performance.now() + (e.born > performance.now() - 300 ? STEM * 1000 : 0) });
      if (!["failed", "waiting"].some((c) => this.n[from].el.classList.contains(c))) this.setState(from, "done");
    }
    this.lastFrom = from; this.cur = to;
    this.visits[to]++; this.count(to);
    this.setState(to, state);
  }
  // 情報の流れ: 線の上に粒を n 個、少しずつずらして流す。届いたら onArrive を呼ぶ
  flow(a, b, n = 1, onArrive = null, kind = "data") {
    if (!this.n[a] || !this.n[b]) return;
    const e = this.edge(a, b, kind);
    const start = performance.now() + Math.max(0, e.born + STEM * 1000 - performance.now());
    for (let i = 0; i < Math.min(n, 12); i++) {
      if (ctx.instant) { onArrive?.(i); continue; }
      this.particles.push({ e, t0: start + i * 140, dur: 900, done: false, onArrive: i < n ? onArrive : null, i });
    }
    if (n > 12 && onArrive) for (let i = 12; i < n; i++) setTimeout(() => onArrive(i), ctx.instant ? 0 : 900 + 12 * 140);
  }
  snap() { this.layout(); Object.values(this.n).forEach((n) => { n.cur = { ...n.tgt }; }); }
  isEmpty() { return Object.keys(this.n).length === 0; }
  bounds() {
    const ns = Object.values(this.n);
    if (!ns.length) return { x0: 0, y0: 0, x1: 400, y1: 300 };
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of ns) { x0 = Math.min(x0, n.tgt.x); y0 = Math.min(y0, n.tgt.y); x1 = Math.max(x1, n.tgt.x + n.el.offsetWidth); y1 = Math.max(y1, n.tgt.y + n.el.offsetHeight); }
    return { x0, y0, x1, y1 };
  }
  select(id) {
    document.querySelectorAll(".fnode.sel").forEach((x) => x.classList.remove("sel")); this.n[id].el.classList.add("sel");
    showDetails(this.details(id));
  }
  details(id) {
    const n = this.n[id];
    return { title: n.label, kv: [["通った回数", this.visits[id] ? `${this.visits[id]} 回` : ""], ["かかった時間", this.seconds[id] ? `${Math.round(this.seconds[id])} 秒` : ""]],
             list: n.hist.map((x) => esc(x)) };
  }
  // 毎フレーム: ノードを目標の位置へ近づけて膨らませ、線を伸ばし、点と粒を流す
  draw(dt) {
    const t = performance.now();
    const k = ctx.instant ? 1 : 1 - Math.pow(0.0008, dt);
    for (const n of Object.values(this.n)) {
      n.cur.x += (n.tgt.x - n.cur.x) * k; n.cur.y += (n.tgt.y - n.cur.y) * k;
      const age = (t - n.born) / 1000, g = clamp((age - (n.delay || 0)) / RIPEN);
      n.el.style.transform = `translate(${n.cur.x.toFixed(1)}px, ${n.cur.y.toFixed(1)}px) scale(${grownScale(g).toFixed(3)})`;
      n.el.style.opacity = easeOut(g).toFixed(3);
    }
    this.beforeDraw();
    const pens = [];
    for (const e of Object.values(this.e)) {
      const full = e.pts = this.geometry(e);
      const grow = clamp((t - e.born) / 1000 / STEM);
      const [p0, p1, p2, p3] = grow >= 1 ? full : bezierTo(...full, easeInOut(grow));
      e.path.setAttribute("d", `M${fmtPt(p0)} C${fmtPt(p1)} ${fmtPt(p2)} ${fmtPt(p3)}`);
      if (grow < 1) pens.push(p3);   // 描いている途中の線の先に、鉛筆の先を出す
      const busyData = e.kind === "data" && this.particles.some((q) => q.e === e);
      e.path.setAttribute("class", `gedge ${e.kind} ${e.count || busyData ? "used" : ""} ${this.isHot(e) ? "hot" : ""} ${grow < 1 ? "growing" : ""}`);
      e.path.style.strokeWidth = e.count && e.kind !== "data" ? Math.min(4, 1.4 + e.count * 0.4) : "";
      if (e.label) {
        const lp = bezierPoint(...full, 0.8);
        e.label.setAttribute("x", lp.x); e.label.setAttribute("y", lp.y - 7);
        e.label.textContent = e.count ? `×${e.count}${e.over ? ` · 変更 ${e.over}` : ""}` : "";
        e.label.setAttribute("class", `glabel ${e.over ? "over" : ""}`);
      }
    }
    let fx = pens.map((p) => `<circle class="pen" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.6"/>`).join("");
    // 制御の流れ: 尾を引く点（0.75 秒で線をたどる）
    this.tokens = this.tokens.filter((tk) => {
      const u = (t - tk.t0) / 750; if (u >= 1) return false; if (u < 0 || !tk.e.pts) return true;
      for (let j = 4; j >= 0; j--) {
        const p = bezierPoint(...tk.e.pts, easeOut(clamp(u - j * 0.035)));
        fx += `<circle class="token" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${(5 - j * 0.8).toFixed(1)}" opacity="${(1 - j * 0.18).toFixed(2)}"/>`;
      }
      return true;
    });
    // 情報の流れ: 線を流れる粒。届いたら知らせる
    this.particles = this.particles.filter((q) => {
      const u = (t - q.t0) / q.dur; if (u < 0 || !q.e.pts) return true;
      if (u >= 1) { if (!q.done) { q.done = true; q.onArrive?.(q.i); } return false; }
      const p = bezierPoint(...q.e.pts, u * u * (3 - 2 * u));
      fx += `<circle class="particle" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3"/>`;
      return true;
    });
    this.fx.innerHTML = fx + this.extraFx(t);
  }
}

// ---- ノードの種類（共通の画面が知っているのは step・data・terminal だけ。知らない種類は step として描く） ----
registerNodeKind("step", { cls: "", w: 220 });
registerNodeKind("data", { cls: "data", w: 240 });
registerNodeKind("terminal", { cls: "terminal", w: 180 });

const COL_W = 290, ROW_GAP = 30;

// 名乗りの graph から作る一般的な流れの図。層（左から右）は、始まりのノードからの近さで決める
export class GraphFlow extends FlowBase {
  constructor(agentId = ctx.runAgent) {
    super();
    const g = manifest(agentId).graph || { nodes: [], edges: [] };
    this.agentId = agentId;
    this.nodes = Object.fromEntries((g.nodes || []).map((n, i) => [n.id, { ...n, i }]));
    this.edges = (g.edges || []).filter((e) => this.nodes[e[0]] && this.nodes[e[1]]);
    const into = new Set(this.edges.map((e) => e[1])), out = new Set(this.edges.map((e) => e[0]));
    const terminals = Object.values(this.nodes).filter((n) => n.kind === "terminal");
    this.startId = terminals.find((n) => !into.has(n.id))?.id || null;
    this.endId = terminals.find((n) => !out.has(n.id) && n.id !== this.startId)?.id || null;
    // 層: 始まり（なければ、入ってくる線のないノード）からの最短の段数
    this.layer = {};
    const roots = this.startId ? [this.startId] : Object.keys(this.nodes).filter((id) => !into.has(id));
    const queue = roots.map((id) => [id, 0]);
    while (queue.length) {
      const [id, d] = queue.shift();
      if (id in this.layer) continue;
      this.layer[id] = d;
      this.edges.filter((e) => e[0] === id).forEach((e) => queue.push([e[1], d + 1]));
    }
  }
  layerOf(id) {
    if (id in this.layer) return this.layer[id];
    // 名乗りにないノード（新しい版のエージェントなど）: 直前のノードの次の層に置く
    this.layer[id] = this.cur ? this.layerOf(this.cur) + 1 : 1;
    return this.layer[id];
  }
  def(id) {
    const m = this.nodes[id] || { id, label: labelOf(id, this.agentId), kind: "step" };
    const k = nodeKind(m.kind);
    return { w: k.w, label: m.label || id, cls: `${k.cls}${id === this.startId ? " start" : ""}`, kind: m.kind };
  }
  layout() {
    if (graphEl.hidden) return;   // 非表示の間は高さが測れない。表示を切り替えたときに並べ直す
    const cols = {};
    for (const id of Object.keys(this.n)) (cols[this.layerOf(id)] ||= []).push(id);
    const order = (id) => this.nodes[id]?.i ?? 1e6 + Object.keys(this.n).indexOf(id);
    const heights = {};
    let tallest = 0;
    for (const [c, ids] of Object.entries(cols)) {
      ids.sort((a, b) => order(a) - order(b));
      heights[c] = ids.reduce((s, id) => s + (this.n[id].el.offsetHeight || 60) + ROW_GAP, -ROW_GAP);
      tallest = Math.max(tallest, heights[c]);
    }
    for (const [c, ids] of Object.entries(cols)) {
      let y = (tallest - heights[c]) / 2;
      for (const id of ids) { this.n[id].tgt = { x: +c * COL_W, y }; y += (this.n[id].el.offsetHeight || 60) + ROW_GAP; }
    }
    if (ctx.instant) Object.values(this.n).forEach((n) => { n.cur = { ...n.tgt }; });
  }
  // 同じ層のノードどうしは縦にまっすぐ、戻る線（右から左）はノードの下を回る
  geometry(e) {
    const la = this.layerOf(e.a), lb = this.layerOf(e.b);
    if (lb > la) return super.geometry(e);
    const a = this.boxOf(e.a), b = this.boxOf(e.b);
    if (la === lb) {
      const down = a.y < b.y;
      const p0 = { x: a.x + a.w / 2, y: down ? a.y + a.h : a.y }, p3 = { x: b.x + b.w / 2, y: down ? b.y : b.y + b.h };
      const my = (p0.y + p3.y) / 2;
      return [p0, { x: p0.x, y: my }, { x: p3.x, y: my }, p3];
    }
    const p0 = { x: a.x + a.w / 2, y: a.y + a.h }, p3 = { x: b.x + b.w / 2, y: b.y + b.h };
    const dip = Math.max(p0.y, p3.y) + 46;
    return [p0, { x: p0.x, y: dip }, { x: p3.x, y: dip }, p3];
  }
  labelled(a, b, kind) { return kind !== "data"; }
  on(e) {
    switch (e.type) {
      case "run.start":
        if (this.startId) { this.node(this.startId); this.setState(this.startId, "done"); this.setText(this.startId, e.request, ctx.runId); this.cur = this.startId; }
        break;
      case "step.start":
        if (this.cur === e.node && this.n[e.node]?.el.classList.contains("waiting")) { this.setState(e.node, "running"); break; }   // 答えで再開した
        this.go(e.node, { back: this.cur && this.layerOf(e.node) <= this.layerOf(this.cur) });
        this.setText(e.node, "", "");
        break;
      case "step.end":
        if (!this.n[e.node]) break;
        this.seconds[e.node] += e.seconds || 0; this.count(e.node);
        if (e.error) { this.setState(e.node, "failed"); this.setText(e.node, undefined, e.error); }
        break;
      case "status": if (this.cur) { this.setText(this.cur, undefined, e.text); this.push(this.cur, e.text); } break;
      case "question": if (this.cur) { this.setState(this.cur, "waiting"); this.setText(this.cur, e.question.split("\n")[0], "利用者の答えを待っている"); } break;
      case "answer": if (this.cur) this.push(this.cur, `答え: ${e.answer}`); break;
      case "run.end":
        if (this.endId) { this.go(this.endId); this.setState(this.endId, "done"); this.setText(this.endId, `${Math.round(e.t)} 秒`, "終わった"); }
        else if (this.cur) this.setState(this.cur, "done");
        break;
      case "run.error": if (this.cur) { this.setState(this.cur, "failed"); this.setText(this.cur, undefined, e.error); } break;
      case "run.cancelled": if (this.cur) { this.setState(this.cur, "failed"); this.setText(this.cur, undefined, "止めた"); } break;
    }
  }
}
