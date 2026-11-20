// テレメトリー画面（docs/telemetry_design.md）。
// サーバーから届くイベント（/api/runs/{id}/events）を受け取り、作業の流れを左から右へ伸びる木として描く。
// - ノードは、親から枝が伸びた先に、実がなるように膨らんで現れる
// - 網羅表もノード（依頼 → 網羅表 → 観点）。ページで確かめた事実は、観点のノードへ飛んで溜まる
// - 木が広がると、全体が収まるよう滑らかに縮小する。ホイール・ピンチで拡大、ドラッグで移動（そのときは自動をやめる）
// 動いている調査はそのまま、終わった調査は記録を再生する（速さを選べる）。
(() => {
"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("canvas"), svg = $("edges"), flow = $("flow");
// チャットが空のときは、見出しと入力欄を真ん中に置く（会話が始まったら入力欄を下に移す）
const setEmpty = (on) => $("chatpane").classList.toggle("empty", on);
const COLW = [170, 250, 236, 236, 236];   // 深さごとの列の幅（依頼・判断・作業・ページ・データベースの記録）
const GAPX = 44, GAPY = 12, PAD = 24;
const MIN_ZOOM = 0.22, MAX_ZOOM = 2.5, FIT_MAX = 1;
const STEM = 1.1, RIPEN = 0.9;      // 線を鉛筆で描く秒数、ノードが大きくなる秒数
const ARRIVE = STEM * 0.92;          // 線がノードの位置に届くころに、ノードが現れ始める
// 動きは画面の中身（何が起きているか）そのものなので、OS の「動きを減らす」設定でも止めない。
// Windows でアニメーション効果をオフにしていると prefers-reduced-motion が有効になり、ノードも線も一瞬で現れていた
const reduce = false;
const ACTION = { search: "検索する", read_pages: "ページを読む", find_images: "画像を探す", check_conflicts: "食い違いを調べる",
  respond: "回答する", ask_user: "聞き返す" };
const WORK = { search: "検索", read_pages: "ページを読む", find_images: "画像", check_conflicts: "食い違い", respond: "報告",
  ask_user: "聞き返し", report_infeasible: "調べられないことの報告" };
const SEARCH_KIND = { free: "LLM が選んだ言葉", aspect: "観点を狙った検索", site: "サイトを狙った検索", conflict: "食い違いの確かめ直し" };
const NEEDS_PERMISSION = /all rights reserved|著作権あり|無断転載/i;

// ---- API ----
let apiKey = "";
try { apiKey = new URLSearchParams(location.search).get("key") || localStorage.getItem("agentKey") || ""; if (apiKey) localStorage.setItem("agentKey", apiKey); } catch (e) { /* 保存できなくても動く */ }
const withKey = (url) => apiKey ? url + (url.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(apiKey) : url;
async function api(path, body) {
  const r = await fetch(path, { method: body ? "POST" : "GET", headers: { "Content-Type": "application/json", ...(apiKey ? { Authorization: "Bearer " + apiKey } : {}) },
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

// ---- 状態 ----
let N, order, info, selected, round, work, dbNode, runId, statusEl, questions, sources, instant, rounds, aim;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return u; } };
const tier = (t) => t ? `<span class="tier${t === 1 ? " t1" : ""}">T${t}</span>` : "";
const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
function icon(h) {
  const letter = esc((h || "?").replace(/^www\./, "")[0]?.toUpperCase() || "?");
  return `<img class="ico" src="/api/favicon?host=${encodeURIComponent(h)}" alt="" onerror="this.outerHTML='<span class=&quot;ico letter&quot;>${letter}</span>'">`;
}
const card = (kind, title, sub, right = "", lead = "") =>
  `<div class="k"><span>${kind}</span><span class="row">${right}<span class="st"></span></span></div><div class="row">${lead}<span class="t">${esc(title)}</span></div><div class="s">${esc(sub)}</div>`;
const toggleBtn = `<button type="button" class="toggle" hidden>畳む ▾</button>`;

function reset() {
  canvas.querySelectorAll(".node, .fly").forEach((e) => e.remove());
  svg.innerHTML = "";
  N = {}; order = []; info = {}; selected = null; round = null; work = null; dbNode = {}; statusEl = null;
  questions = new Set(); sources = []; instant = false; rounds = 0; aim = null;
  $("drawer").classList.remove("open"); $("flowEmpty").hidden = false;
  FLOW.reset();
  fitMode(true); cam = { x: 0, y: 0, s: 1 }; camT = { ...cam };
}

// ---- 木 ----
function add(id, parent, depth, html, details, cls = "", first = false) {
  if (N[id]) return N[id].el;
  $("flowEmpty").hidden = true;
  const el = document.createElement("div");
  el.className = `node ${cls}`; el.style.setProperty("--w", COLW[depth] + "px"); el.innerHTML = html;
  el.tabIndex = 0; el.setAttribute("role", "button");
  el.addEventListener("click", (e) => { if (!e.target.closest(".toggle")) select(id); });
  el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(id); } });
  const tg = el.querySelector(".toggle"); if (tg) tg.addEventListener("click", () => collapse(id, !N[id].collapsed));
  canvas.appendChild(el);
  const p = N[parent];
  N[id] = { id, parent, depth, el, kids: [], cur: { x: 0, y: 0 }, tgt: { x: 0, y: 0 }, collapsed: false, hidden: false, extra: [],
            born: instant || reduce ? -1e9 : performance.now() };
  if (p) { first ? p.kids.unshift(id) : p.kids.push(id); const t = p.el.querySelector(".toggle"); if (t) t.hidden = false; }
  order.push(id); info[id] = details || { title: id };
  layout();
  N[id].cur = { ...N[id].tgt };   // 生まれるノードは、その場所で大きくなる（動いてくるのではない）
  if (!instant && !reduce) { el.style.opacity = "0"; el.style.transform = `translate(${N[id].cur.x}px, ${N[id].cur.y}px) scale(0.55)`; }
  aim = id;
  return el;
}
function layout() {
  const root = N.req; if (!root || canvas.hidden) return;   // 非表示の間は高さが測れない
  const colX = []; let x = PAD; COLW.forEach((w, i) => { colX[i] = x; x += w + GAPX; });
  let y = PAD;
  function place(id, hide) {
    const n = N[id]; n.hidden = hide; n.el.classList.toggle("gone", hide);
    const h = n.el.offsetHeight || 50;
    if (hide) { const p = N[n.parent]; n.tgt = { x: p.tgt.x + 30, y: p.tgt.y }; n.kids.forEach((k) => place(k, true)); return; }
    const kids = n.collapsed ? [] : n.kids;
    if (!kids.length) { n.tgt = { x: colX[n.depth], y }; y += h + GAPY; n.kids.forEach((k) => place(k, true)); return; }
    const top = y;
    kids.forEach((k) => place(k, false));
    const f = N[kids[0]], l = N[kids[kids.length - 1]];
    const mid = (f.tgt.y + l.tgt.y + (l.el.offsetHeight || 50)) / 2;
    n.tgt = { x: colX[n.depth], y: Math.max(top, mid - h / 2) };
    if (n.tgt.y + h + GAPY > y) y = n.tgt.y + h + GAPY;
  }
  root.kids.forEach((k) => place(k, false));
  root.tgt = { x: colX[0], y: PAD };
  if (instant) order.forEach((id) => { N[id].cur = { ...N[id].tgt }; });
}
function setState(id, st) {
  const n = N[id]; if (!n) return;
  n.el.classList.remove("planned", "running", "llm", "waiting", "done", "failed");
  if (st) n.el.classList.add(st);
}
function setSub(id, text) { const s = N[id]?.el.querySelector(".s"); if (s) s.textContent = text; }
function setTitle(id, text) { const s = N[id]?.el.querySelector(".t"); if (s) s.textContent = text; }
function step(id, i, cls) { const s = N[id]?.el.querySelectorAll(".steps span")[i]; if (s) s.className = cls; }
function collapse(id, yes) {
  const n = N[id]; if (!n) return; n.collapsed = yes;
  const t = n.el.querySelector(".toggle"); if (t) t.textContent = yes ? `${n.kids.length} 件 ▸` : "畳む ▾";
  layout();
}
function reveal(id) { let p = N[id]?.parent; while (p) { if (N[p].collapsed) collapse(p, false); p = N[p].parent; } }

// 3 次ベジェ曲線を途中（t）までで切る（枝が伸びていく途中を描く）
function bezierTo(p0, p1, p2, p3, t) {
  const L = (a, b) => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
  const a = L(p0, p1), b = L(p1, p2), c = L(p2, p3), d = L(a, b), e = L(b, c), f = L(d, e);
  return [p0, a, d, f];
}
const easeOut = (t) => 1 - Math.pow(1 - t, 3);
const easeInOut = (t) => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;   // ゆっくり始まり、ゆっくり終わる（鉛筆の線）
// ノードは小さめの大きさから、行き過ぎずに滑らかに大きくなる。現れ方も同じ速さで
const grownScale = (g) => g >= 1 ? 1 : 0.55 + 0.45 * easeOut(g);

// ---- 表示の切り替え（流れの図 / 木） ----
let view = "flow";
function setView(v) {
  view = v;
  $("graph").hidden = v !== "flow"; canvas.hidden = v !== "tree";
  if (v === "flow") { FLOW.layout(); Object.values(FLOW.n).forEach((n) => { n.cur = { ...n.tgt }; }); }
  else { layout(); order.forEach((id) => { N[id].cur = { ...N[id].tgt }; }); }
  $("flowEmpty").hidden = v === "flow" ? Object.keys(FLOW.n).length > 0 : order.length > 0;
  document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === v)));
  fitMode(true); fitTarget(); cam = { ...camT };   // 切り替えたら、カメラもすぐ全体に合わせる
}
document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));

// ---- カメラ（拡大・縮小・移動） ----
let cam = { x: 0, y: 0, s: 1 }, camT = { ...cam }, auto = true;
function fitMode(on) { auto = on; $("zoomFit").setAttribute("aria-pressed", String(on)); }
function fitTarget() {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  if (view === "flow") ({ x0, y0, x1, y1 } = FLOW.bounds());
  else {
    const vis = order.filter((id) => !N[id].hidden);
    if (!vis.length) return;
    for (const id of vis) {
      const n = N[id];
      x0 = Math.min(x0, n.tgt.x); y0 = Math.min(y0, n.tgt.y);
      x1 = Math.max(x1, n.tgt.x + n.el.offsetWidth); y1 = Math.max(y1, n.tgt.y + n.el.offsetHeight);
    }
  }
  const W = flow.clientWidth, H = flow.clientHeight, m = view === "flow" ? 56 : 36, bottom = 64;
  const bw = x1 - x0, bh = y1 - y0;
  const s = clamp(Math.min((W - 2 * m) / bw, (H - m - bottom) / bh), MIN_ZOOM, FIT_MAX);
  let x = (W - bw * s) / 2 - x0 * s, y = m + Math.max(0, (H - m - bottom - bh * s) / 2) - y0 * s;
  // 縮小しきっても収まらないときは、いちばん新しいノードのあたりを見せる
  if (view === "tree" && bw * s > W - 2 * m && aim && N[aim]) x = clamp(W * 0.62 - (N[aim].tgt.x + COLW[N[aim].depth]) * s, W - 2 * m - x1 * s, m - x0 * s);
  if (view === "tree" && bh * s > H - m - bottom && aim && N[aim]) y = clamp(H * 0.5 - N[aim].tgt.y * s, H - bottom - y1 * s, m - y0 * s);
  camT = { x, y, s };
}
function zoomAt(mx, my, factor) {
  fitMode(false);
  const s = clamp(camT.s * factor, 0.05, MAX_ZOOM);   // 自分で縮小するときは、自動より小さくできる
  camT = { x: mx - (mx - camT.x) * (s / camT.s), y: my - (my - camT.y) * (s / camT.s), s };
}
function jumpTo(id) {
  const n = N[id]; if (!n) return;
  fitMode(false);
  const s = Math.max(camT.s, 0.95);
  camT = { s, x: flow.clientWidth * 0.4 - (n.tgt.x + n.el.offsetWidth / 2) * s, y: flow.clientHeight * 0.45 - (n.tgt.y + n.el.offsetHeight / 2) * s };
}
flow.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = flow.getBoundingClientRect();
  zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0018)));
}, { passive: false });
// 移動: ノードの上からでも、少し動かせばドラッグにする（木が大きいと、窓がノードで埋まってつかむ場所がなくなるため）。
// 動かさずに離したら、これまでどおりノードを押したことになる。
// マウスで押し始めるたびに、取り逃した「離した」の記録を消す（残っていると 2 本指のピンチと見なされ、動かせなくなる）
const pointers = new Map(); let pinch = null, downAt = null, dragged = false;
flow.addEventListener("pointerdown", (e) => {
  if (e.button > 0 || e.target.closest(".hud, .drawer, .toggle, .pchip, a")) return;
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
  } else if (pointers.size === 2 && pinch) {
    const [a, b] = [...pointers.values()], d = Math.hypot(a.x - b.x, a.y - b.y), r = flow.getBoundingClientRect();
    zoomAt((a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top, d / pinch.d); pinch.d = d;
  }
});
const endPointer = (e) => { pointers.delete(e.pointerId); if (pointers.size < 2) pinch = null; if (!pointers.size) flow.classList.remove("dragging"); };
["pointerup", "pointercancel", "lostpointercapture"].forEach((t) => flow.addEventListener(t, endPointer));
window.addEventListener("blur", () => { pointers.clear(); flow.classList.remove("dragging"); });
// ドラッグした後の「押した」は、ノードの選択にしない
flow.addEventListener("click", (e) => { if (dragged) { e.stopPropagation(); e.preventDefault(); dragged = false; } }, true);
$("zoomIn").addEventListener("click", () => zoomAt(flow.clientWidth / 2, flow.clientHeight / 2, 1.3));
$("zoomOut").addEventListener("click", () => zoomAt(flow.clientWidth / 2, flow.clientHeight / 2, 1 / 1.3));
$("zoomFit").addEventListener("click", () => fitMode(true));

// 毎フレーム: ノードを目標の位置へ近づけ、生まれたばかりのノードは枝を伸ばしてから膨らませ、線とカメラを動かす
function animate(dt) {
  if (!N || !FLOW.n) return;
  const t = performance.now();
  const k = instant || reduce ? 1 : 1 - Math.pow(0.0008, dt);
  for (const id of order) {
    const n = N[id];
    n.cur.x += (n.tgt.x - n.cur.x) * k; n.cur.y += (n.tgt.y - n.cur.y) * k;
    const age = (t - n.born) / 1000;
    const g = n.parent ? clamp((age - ARRIVE) / RIPEN) : clamp(age / RIPEN);
    n.el.style.transform = `translate(${n.cur.x.toFixed(1)}px, ${n.cur.y.toFixed(1)}px) scale(${grownScale(g).toFixed(3)})`;
    n.el.style.opacity = n.hidden ? 0 : easeOut(g).toFixed(3);
  }
  let d = ""; const live = [], growing = [], buds = [], rep = [];
  const anchorY = (n) => n.cur.y + Math.min(22, n.el.offsetHeight / 2);
  const fmt = (p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
  for (const id of order) {
    const n = N[id]; if (!n.parent || n.hidden) continue;
    const p = N[n.parent];
    const p0 = { x: p.cur.x + p.el.offsetWidth, y: anchorY(p) }, p3 = { x: n.cur.x, y: anchorY(n) };
    const mx = p0.x + (p3.x - p0.x) / 2;
    const age = (t - n.born) / 1000, e = clamp(age / STEM);
    const [a, b, c, q] = e >= 1 ? [p0, { x: mx, y: p0.y }, { x: mx, y: p3.y }, p3] : bezierTo(p0, { x: mx, y: p0.y }, { x: mx, y: p3.y }, p3, easeInOut(e));
    const path = `M${fmt(a)} C${fmt(b)} ${fmt(c)} ${fmt(q)}`;
    if (e < 1 || age < ARRIVE + RIPEN * 0.5) { growing.push(path); if (e < 1) buds.push(q); }
    else if (n.el.classList.contains("running") || n.el.classList.contains("llm")) live.push(path);
    else d += path;
    for (const other of n.extra) {   // 転載: ほぼ同じ文章のページどうしを点線で結ぶ
      const o = N[other]; if (!o || o.hidden) continue;
      const s0 = { x: n.cur.x + n.el.offsetWidth, y: anchorY(n) + 8 }, s3 = { x: o.cur.x + o.el.offsetWidth, y: anchorY(o) + 8 };
      rep.push(`M${fmt(s0)} C${fmt({ x: s0.x + 60, y: s0.y })} ${fmt({ x: s3.x + 60, y: s3.y })} ${fmt(s3)}`);
    }
  }
  svg.innerHTML = `<path d="${d}"/>` + live.map((p) => `<path class="live" d="${p}"/>`).join("")
    + growing.map((p) => `<path class="grow" d="${p}"/>`).join("") + buds.map((q) => `<circle class="pen" cx="${q.x.toFixed(1)}" cy="${q.y.toFixed(1)}" r="2.6"/>`).join("")
    + rep.map((p) => `<path class="rep" d="${p}"/>`).join("");
  if (view === "flow") FLOW.draw(dt);
  if (auto) fitTarget();
  const kc = instant || reduce ? 1 : 1 - Math.pow(0.02, dt);
  cam.x += (camT.x - cam.x) * kc; cam.y += (camT.y - cam.y) * kc; cam.s += (camT.s - cam.s) * kc;
  (view === "flow" ? $("graph") : canvas).style.transform = `translate(${cam.x.toFixed(1)}px, ${cam.y.toFixed(1)}px) scale(${cam.s.toFixed(4)})`;
}

// ---- チャット ----
function now(text) { const txt = statusEl && statusEl.querySelector(".txt"); if (txt) txt.textContent = text; }
function chatUser(text) { setEmpty(false); const d = document.createElement("div"); d.className = "u"; d.textContent = text; $("chat").appendChild(d); scrollChat(); }
function chatAgent(html) {
  setEmpty(false);
  const d = document.createElement("div"); d.className = "a"; d.innerHTML = `<div class="avatar">R</div><div class="body">${html}</div>`;
  $("chat").appendChild(d); scrollChat(); return d;
}
function scrollChat() { $("chat").scrollTop = $("chat").scrollHeight; }
function statusBubble() {
  if (!statusEl) statusEl = chatAgent(`<div class="status"><span class="dots"><i></i><i></i><i></i></span><span class="txt">調べています</span></div>`);
}

// ---- 網羅表のノード ----
const aspectId = (name) => "a:" + name;
function buildCoverage(e) {
  add("cov", "req", 1, card("網羅表", `観点 ${e.aspects.length} つ`, `${e.subject_type} · ${e.origin}が作成`) + toggleBtn,
    { title: "網羅表", kv: [["対象の種類", esc(e.subject_type)], ["観点を作ったもの", esc(e.origin)]],
      note: "観点ごとに、確かめた事実の数と状態（未発見 · 出典不足 · 確認済み）をプログラムが集計する。" }, "done", true);
  e.aspects.forEach((a) => add(aspectId(a), "cov", 2,
    `<div class="k"><span>観点</span><span class="mono cnt">0</span></div><div class="t">${esc(a)}</div><div class="s">未発見</div><div class="facts"></div>`,
    { title: `観点: ${a}`, kv: [["状態", "未発見"]], facts: [] }, "aspect"));
}
function flyFact(pageId, f, t, delay) {
  const target = N[aspectId(f.aspect)];
  const land = () => {
    if (!target) return;
    const sq = document.createElement("i"); if (t !== 1) sq.className = "t2"; sq.title = f.text;
    target.el.querySelector(".facts").appendChild(sq);
    target.el.querySelector(".cnt").textContent = target.el.querySelectorAll(".facts i").length;
    // 状態（確認済みなど）は、ページを読み終えたときにプログラムが集計する（coverage のイベント）
    if (!target.el.classList.contains("ok") && !target.el.classList.contains("weak")) setSub(target.id, "集計を待っている");
    info[target.id].facts.push(f);
    layout();
  };
  const from = N[pageId];
  if (instant || reduce || !from || !target || from.hidden || target.hidden) { land(); return; }
  setTimeout(() => {
    const chip = document.createElement("div"); chip.className = "fly"; chip.textContent = f.text.length > 13 ? f.text.slice(0, 13) + "…" : f.text;
    chip.style.transform = `translate(${from.cur.x + 14}px, ${from.cur.y + 10}px)`; canvas.appendChild(chip);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      chip.style.transform = `translate(${target.cur.x + 14}px, ${target.cur.y + 30}px) scale(.55)`; chip.style.opacity = "0";
    }));
    setTimeout(() => { chip.remove(); land(); }, 900);
  }, delay);
}
function coverage(rows) {
  rows.forEach((r) => {
    const id = aspectId(r.aspect), n = N[id]; if (!n) return;
    n.el.classList.toggle("ok", r.status === "確認済み"); n.el.classList.toggle("weak", r.status === "出典不足");
    setSub(id, r.status === "確認済み" ? "✓ 確認済み" : r.status + (r.independent > 1 ? `（${r.independent} サイト）` : ""));
    n.el.querySelector(".cnt").textContent = r.facts;
    info[id].kv = [["状態", esc(r.status)], ["事実", `${r.facts} 件`], ["独立した出典のサイト", `${r.independent}`],
      ["最も高い Tier", r.best_tier ? `T${r.best_tier}` : ""]];
  });
  if (N.cov) setSub("cov", `${rows.filter((r) => r.status === "確認済み").length} / ${rows.length} 観点を確認済み`);
}

// ---- 詳細 ----
function select(id) {
  if (selected && N[selected]) N[selected].el.classList.remove("sel");
  selected = id; N[id].el.classList.add("sel");
  showDetails(info[id] || { title: id });
}
function showDetails(d) {
  $("dTitle").textContent = d.title || "";
  let h = "";
  if (d.photo) h += `<img class="big" src="${d.photo}" alt="${esc(d.alt || "")}">`;
  if (d.kv) h += `<dl class="kv">${d.kv.filter(([, v]) => v !== "" && v != null).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
  if (d.facts?.length) h += `<div><strong>照合できた事実 ${d.facts.length} 件</strong><ul>${d.facts.map((f) =>
    `<li>${esc(f.text)} <span style="color:var(--muted)">· ${esc(f.aspect)}</span><div class="q">「${esc(f.quote)}」</div></li>`).join("")}</ul></div>`;
  if (d.dropped?.length) h += `<div><strong>捨てた事実 ${d.dropped.length} 件</strong><ul>${d.dropped.map((f) =>
    `<li>${esc(f.text)}<div class="q">${esc(f.why)}${f.quote ? `：「${esc(f.quote)}」` : ""}</div></li>`).join("")}</ul></div>`;
  if (d.list?.length) h += `<ul>${d.list.map((x) => `<li>${x}</li>`).join("")}</ul>`;
  if (d.note) h += `<p class="q">${d.note}</p>`;
  $("dBody").innerHTML = h; $("drawer").classList.add("open");
}
$("dClose").addEventListener("click", () => { $("drawer").classList.remove("open"); if (selected && N[selected]) N[selected].el.classList.remove("sel"); selected = null; });
const link = (u) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer" class="mono">${esc(u)}</a>`;

// ---- イベント → 画面 ----
function handle(e) { const H = HANDLERS[e.type]; if (H) H(e); FLOW.on(e); }
const HANDLERS = {
  "run.start": (e) => {
    chatUser(e.request); statusBubble();
    add("req", null, 0, `<div class="k"><span>依頼</span><span class="st"></span></div><div class="t">${esc(e.request)}</div><div class="s">${esc(runId)}</div>`,
      { title: "依頼", kv: [["依頼文", esc(e.request)], ["実行の ID", `<span class="mono">${esc(runId)}</span>`]] }, "root");
    now("調査を始めた");
  },
  "step.start": (e) => {
    if (e.node === "decide") {
      rounds++; const id = "d" + e.step; round = id;
      add(id, "req", 1, `<div class="row"><span class="num">判断 ${rounds}</span><span class="choice">LLM が考えている</span><span class="st"></span></div>`,
        { title: `判断 ${rounds}` }, "round llm");
      now(`判断 ${rounds} · 次に何をするかを LLM が考えている`);
      return;
    }
    if (e.node === "check_feasibility") {
      add("w" + e.step, "req", 1, `<div class="row"><span class="num">判定</span><span class="choice">調べられるか</span><span class="st"></span></div>`,
        { title: "公開情報で調べられるか" }, "round llm feas");
      now("依頼が公開情報で調べられるかを、LLM が判定している");
      return;
    }
    const id = "w" + e.step;
    if (N[id]) { setState(id, "running"); return; }   // 聞き返しの答えで再開した
    work = id;
    add(id, round || "req", 2, card(WORK[e.node] || e.node, WORK[e.node] || e.node, "始めた") + toggleBtn,
      { title: WORK[e.node] || e.node }, e.node === "respond" || e.node === "check_conflicts" ? "llm" : "running");
    if (e.node === "respond") now("確かめた事実と出典番号から、LLM が報告を書いている");
    if (e.node === "check_conflicts") now("観点ごとに、別々のサイトの事実が食い違っていないか確かめている");
  },
  "step.end": (e) => {
    const id = e.node === "decide" ? "d" + e.step : "w" + e.step;
    if (e.error) { setState(id, "failed"); setSub(id, e.error); }
    else if (!N[id]?.el.classList.contains("failed")) setState(id, "done");
    if (info[id]) info[id].kv = [...(info[id].kv || []), ["所要時間", `${e.seconds} 秒${e.llm_wait ? `（うち LLM サーバーの待ち ${e.llm_wait} 秒）` : ""}`]];
  },
  "feasibility": (e) => {
    const id = order.find((k) => N[k].el.classList.contains("feas"));
    if (!id) return;
    N[id].el.querySelector(".choice").textContent = { yes: "調べられる", partial: "一部は調べられない", no: "調べられない", unsure: "調べてみる" }[e.verdict] || e.verdict;
    info[id].kv = [["判定", esc(e.verdict)], ["理由", esc(e.reason)], ["調べられない部分", esc((e.infeasible_parts || []).join("、"))]];
    layout();
  },
  "decision": (e) => {
    if (!round || !N[round]) return;
    const llm = ACTION[e.llm?.action] || e.llm?.action || "", act = ACTION[e.action] || e.action;
    const q = e.action === "search" && e.query ? `「${e.query}」` : "";
    const changed = e.guard && e.llm?.action !== e.action || (e.guard && e.llm?.query !== e.query);
    N[round].el.querySelector(".choice").innerHTML = changed ? `<span class="was">${esc(llm)}</span> → ${esc(act + q)}` : esc(act + q);
    if (changed) N[round].el.querySelector(".row").insertAdjacentHTML("beforeend", `<span class="tag">変更</span>`);
    const plan = e.plan ? `点数 ${e.plan.score}（${Object.entries(e.plan.parts || {}).map(([k, v]) => `${esc(k)} ${(+v).toFixed(2)}`).join(" × ")}）` : "";
    info[round].kv = [["LLM の選択", esc(llm + (e.llm?.query ? `「${e.llm.query}」` : ""))], ["LLM の理由", esc(e.llm?.reason)],
      ["実行した行動", esc(act + q)], ["プログラムの変更", esc(e.guard)], ["検索の点数", plan],
      ["他の候補", (e.plan?.alternatives || []).map((a) => `${esc(a.query)}（${a.score}）`).join("<br>")]];
    info[round].note = changed ? "LLM の選択を、網羅表と止める条件を見たプログラムが変えた（guard）。" : "";
    layout();
  },
  "search.start": (e) => {
    if (!N[work]) return;
    N[work].el.querySelector(".k span").textContent = "検索 · " + (SEARCH_KIND[e.kind] || e.kind);
    setTitle(work, e.query); setSub(work, "SearXNG と Wikidata に問い合わせている");
    info[work] = { title: "検索", kv: [["検索の言葉", esc(e.query)], ["種類", esc(SEARCH_KIND[e.kind] || e.kind)], ["狙った観点", esc(e.aspect)], ["狙ったサイト", esc(e.site)]] };
    now(`検索している: ${e.query}`);
  },
  "search.done": (e) => {
    if (!N[work]) return;
    const w = work, rel = e.results.filter((r) => r.relevant).length;
    setSub(w, e.failed ? e.failed : `結果 ${e.results.length} 件 · 対象を含む ${rel} 件${e.target ? " · 対象: " + e.target.label : ""}`);
    if (e.failed) setState(w, "failed");
    info[w].kv.push(["結果", `${e.results.length} 件（対象の名前を含む ${rel} 件）`], ["応答しなかったエンジン", esc((e.unresponsive || []).join("、"))],
      ["やり直し", e.retried ? "0 件だったので時間をおいて 1 回やり直した" : ""], ["公式サイト", esc((e.official || []).join("、"))],
      ["拾った表記ゆれ", esc((e.variants || []).join("、"))]);
    if (e.candidates?.length) {
      add(w + "-cand", w, 3, card("同名の候補 · Wikidata", e.target ? e.target.label : `${e.candidates.length} 件`, e.target ? "対象が決まった" : "決まっていない", "", icon("wikidata.org")) +
        `<div class="chips2">${e.candidates.slice(0, 4).map((c) => `<span>${esc(c.label)}</span>`).join("")}</div>`,
        { title: "同名の候補", list: e.candidates.map((c) => `${esc(c.id)} ${esc(c.label)}（${esc(c.description)}）`) }, "done");
    }
    if (e.results.length) {
      const hosts = [...new Set(e.results.map((r) => r.host))].slice(0, 10);
      add(w + "-res", w, 3, card("検索結果", `${e.results.length} 件 · 対象を含む ${rel} 件`, "Tier の高い順に読む") + `<div class="icons">${hosts.map(icon).join("")}</div>`,
        { title: `検索結果（${e.results.length} 件）`, list: e.results.map((r) => `${r.relevant ? "" : "（対象の名前なし）"}${esc(r.title)} — ${link(r.url)} · T${r.tier}`) }, "done");
    }
    if (e.summary_url && !N.sum) {
      add("sum", w, 3, card("概要", host(e.summary_url), "対象の概要（報告の冒頭に使う）", tier(2), icon(host(e.summary_url))),
        { title: "概要", kv: [["URL", link(e.summary_url)]] }, "done");
      N.sum.url = e.summary_url;
    }
  },
  "aspects": (e) => {
    buildCoverage(e);
    if (N[work]) { setSub(work, `${e.subject_type} · 観点 ${e.aspects.length} つ`); info[work].kv = [["対象の種類", esc(e.subject_type)], ["観点", esc(e.aspects.join("、"))]]; }
    now("対象の種類を判定し、網羅表の観点を作った");
  },
  "databases": (e) => {
    if (!N[work]) return;
    const id = work + "-db"; dbNode[work] = id;
    add(id, work, 3, card("分野別データベース · L3", e.used.join(" → ") || "この種類に使えるものがない", `記録 ${e.records.length} 件（${e.seconds} 秒）${e.failures.length ? " · 失敗あり" : ""}`,
      "", e.records[0] ? icon(host(e.records[0].url)) : "") + toggleBtn,
      { title: "分野別データベース（L3）", kv: [["引いたもの", esc(e.used.join("、"))], ["使えなかったもの", esc(e.failures.join("、"))]],
        list: e.records.map((r) => `${esc(r.database)}: ${link(r.url)}`) }, e.failures.length && !e.records.length ? "failed" : "done");
  },
  "page.planned": (e) => pageNode(e, "planned"),
  "page.start": (e) => { pageNode(e, "running"); step("p:" + e.url, 0, "work"); now(`${e.host} を${e.db ? "（データベースの記録として）" : ""}読んでいる`); },
  "page.fetched": (e) => {
    const id = "p:" + e.url; if (!N[id]) return;
    if (!e.ok) { setState(id, "failed"); step(id, 0, "ng"); setSub(id, e.reason); info[id].kv.push(["結果", esc(e.reason)]); return; }
    step(id, 0, "on"); if (e.title) setTitle(id, e.title); setSub(id, `取得した · ${e.chars.toLocaleString()} 文字`);
    info[id].title = e.title || info[id].title;
  },
  "page.llm": (e) => {
    const id = "p:" + e.url; if (!N[id]) return;
    setState(id, "llm"); step(id, 1, "work"); setSub(id, "LLM が引用付きで事実を抜き出している");
    now(`${host(e.url)} の本文から、LLM が引用付きで事実を抜き出している`);
  },
  "page.done": (e) => {
    const id = "p:" + e.url; if (!N[id]) return;
    setState(id, "done"); step(id, 1, "on"); step(id, 2, "on");
    const moved = e.tier !== e.base_tier ? ` · T${e.base_tier}→T${e.tier}` : "";
    setSub(id, `事実 ${e.facts.length} 件 · 捨てた ${e.dropped.length} 件${moved}`);
    if (moved) N[id].el.querySelector(".tier").outerHTML = tier(e.tier);
    Object.assign(info[id], { facts: e.facts, dropped: e.dropped });
    info[id].kv.push(["Tier の調整", e.tier_reason ? esc(`T${e.base_tier}→T${e.tier}（${e.tier_reason}）`) : ""],
      ["見分ける特徴", esc((e.features || []).join("、"))], ["別の呼び方", esc((e.new_terms || []).join("、"))],
      ["同名の別の対象", esc(e.other_entity)]);
    e.facts.forEach((f, i) => flyFact(id, f, e.tier, i * 160));
  },
  "page.reprint": (e) => {
    const id = order.find((k) => k.startsWith("p:") && sameUrl(k.slice(2), e.url));
    const orig = order.find((k) => k.startsWith("p:") && sameUrl(k.slice(2), e.original));
    if (!id) return;
    setSub(id, `ほぼ同じ文章: ${host(e.original)}（独立した出典に数えない）`);
    if (orig) N[id].extra.push(orig);
    info[id].kv.push(["転載", `${link(e.original)} とほぼ同じ文章`]);
  },
  "coverage": (e) => coverage(e.rows),
  "image.candidates": (e) => {
    if (!N[work]) return;
    setSub(work, `${e.ranked} 候補 · ${e.fetching} 件を取得`);
    info[work] = { title: "画像の探索と確認", kv: [["候補", esc(Object.entries(e.counts).map(([k, v]) => `${k} ${v}`).join(" · "))]] };
    now("画像の候補を集め、1 枚ずつ確かめている");
  },
  "image.fetched": (e) => {
    if (!N[work]) return;
    if (!e.ok) {
      const g = work + "-imgfail";
      if (!N[g]) add(g, work, 3, card("取れなかった画像", "0 件", "取得できない・小さすぎる・画像でない"), { title: "取れなかった画像", list: [] }, "failed");
      info[g].list.push(`${esc(e.stage)} · ${esc(e.reason)} · ${link(e.src)}`); setTitle(g, `${info[g].list.length} 件`);
      return;
    }
    const id = "i:" + e.src;
    add(id, work, 3, `<div class="k"><span class="row">${icon(e.host)}<span>${esc(e.stage)}</span></span><span class="row">${tier(e.tier)}<span class="st"></span></span></div>
      <div class="row" style="margin-top:4px"><div class="thumb">${e.related ? "参考" : "確認前"}</div><div style="min-width:0"><div class="t">${esc(e.title || host(e.src))}</div><div class="s">取得した · ${esc(e.size)}</div></div></div>
      <div class="steps"><span class="on"></span><span></span><span></span></div>`,
      { title: e.stage, kv: [["画像", link(e.src)], ["掲載ページ", `${link(e.page_url)}（T${e.tier}）`], ["ライセンス", esc(e.license)],
        ["関係", e.related ? esc(`「${e.related}」（${e.relation}）の画像。対象そのものではない`) : ""]] }, "running");
    N[id].license = e.license;
  },
  "image.llm": (e) => { const id = "i:" + e.src; if (!N[id]) return; setState(id, "llm"); step(id, 1, "work"); setSub(id, "VLM が対象かどうか確かめている"); },
  "image.done": (e) => {
    const id = "i:" + e.src; if (!N[id]) return;
    const ng = e.level === "不採用";
    setState(id, ng ? "failed" : "done"); step(id, 1, ng ? "ng" : "on"); step(id, 2, ng ? "ng" : "on");
    if (ng) N[id].el.classList.add("dropped");
    setSub(id, `${e.level} · ${e.reason || ""}`);
    const license = e.license || N[id].license || "";
    const th = N[id].el.querySelector(".thumb");
    if (!ng && !NEEDS_PERMISSION.test(license)) {
      const src = withKey(`/api/image?run=${encodeURIComponent(runId)}&src=${encodeURIComponent(e.src)}`);
      th.innerHTML = `<img src="${src}" alt="${esc(e.description)}">`; info[id].photo = src; info[id].alt = e.description;
    } else if (ng && !NEEDS_PERMISSION.test(license)) {
      th.innerHTML = `<img src="${withKey(`/api/image?run=${encodeURIComponent(runId)}&src=${encodeURIComponent(e.src)}`)}" alt="" onerror="this.remove()"><span class="un">不採用</span>`;
    } else th.innerHTML = "許可が<br>必要";
    info[id].kv.push(["判定", esc(e.level)], ["理由", esc(e.reason)], ["写っているもの", esc(e.description)],
      ["文脈 V-1 / 見た目 V-3", e.context ? esc(`${e.context} / ${e.visual}`) : ""], ["画像の中の文字", esc(e.visible_text)],
      ["画像で確かめた特徴", esc((e.features_seen || []).join("、"))], ["ライセンス", esc(license)]);
  },
  "image.final": (e) => {
    order.filter((k) => k.startsWith("i:") && N[k].parent === work && N[k].el.classList.contains("running")).forEach((k) => {
      setState(k, "planned"); setSub(k, "未確認（確認済みがそろった、または確かめる数の上限）"); step(k, 1, "ng"); step(k, 2, "ng");
      const lic = N[k].license || "", src = k.slice(2);
      N[k].el.querySelector(".thumb").innerHTML = NEEDS_PERMISSION.test(lic) ? "未確認"
        : `<img src="${withKey(`/api/image?run=${encodeURIComponent(runId)}&src=${encodeURIComponent(src)}`)}" alt="" onerror="this.remove()"><span class="un">未確認</span>`;
    });
    (e.withheld || []).forEach((w) => { const id = "i:" + w.src; if (!N[id]) return; N[id].el.classList.add("dropped"); setSub(id, "確認済み · 許可が必要なので載せない"); info[id].kv.push(["報告", esc(w.why)]); });
    if (N[work]) setSub(work, `確かめた ${order.filter((k) => k.startsWith("i:") && N[k].parent === work).length} · 載せる ${e.shown.length}`);
  },
  "conflicts": (e) => {
    if (!N[work]) return;
    setSub(work, `比べた観点 ${e.aspects.length} · 食い違い ${e.conflicts.length} 件`);
    e.conflicts.forEach((c, i) => add(`${work}-c${i}`, work, 3, card("食い違い · " + c.aspect, c.topic, c.facts.map((f) => f.text).join(" / ")),
      { title: `食い違い: ${c.topic}`, list: c.facts.map((f) => `${esc(f.text)} — ${link(f.source)}`) }, "done"));
  },
  "question": (e) => {
    if (N[work]) { setState(work, "waiting"); setTitle(work, "利用者の答えを待っている"); setSub(work, e.question.split("\n")[0]); }
    if (questions.has(e.question)) return;
    questions.add(e.question); showQuestion(e.question);
  },
  "answer": (e) => { const key = "a:" + e.answer + e.question; if (!questions.has(key)) { questions.add(key); chatUser(e.answer); } setComposer(false); statusBubble(); },
  "run.waiting": (e) => { if (!questions.has(e.question)) { questions.add(e.question); showQuestion(e.question); } setComposer(true); },
  "report": (e) => {
    sources = e.sources || [];
    if (N[work]) { setSub(work, `出典 ${sources.length} 件`); info[work].list = sources.map((s, i) => `[${i + 1}] ${esc(s.title)} — ${link(s.url)} · T${s.tier}`); }
  },
  "run.end": (e) => {
    setState("req", "done");
    const body = renderReport(e.answer || "");
    const el = statusEl || chatAgent("");
    el.querySelector(".body").innerHTML = body; wireCites(el); statusEl = null; liveState = ""; setComposer(false);
    loadRuns();
  },
  "run.error": (e) => { chatAgent(`<p>エラーで調査を続けられませんでした: ${esc(e.error)}</p>`); statusEl = null; liveState = ""; updateSend(); },
  "run.paused": () => { if (!replay) liveState = "paused"; now("一時停止している（今の作業が終わったところで止まる）"); updateSend(); },
  "run.resumed": () => { if (!replay) liveState = "running"; now("再開した"); updateSend(); },
};
function sameUrl(a, b) { const n = (u) => u.replace(/[?#].*$/, "").replace(/\/$/, ""); return n(a) === n(b); }
function pageNode(e, state) {
  const id = "p:" + e.url;
  if (N[id]) { if (state !== "planned") setState(id, state); return; }
  const parent = e.db && dbNode[work] ? dbNode[work] : work;
  if (!N[parent]) return;
  add(id, parent, N[parent].depth + 1, card(e.db ? "記録 · " + e.db : "ページ", e.host, state === "planned" ? "読む順番を待っている" : "取得している", tier(e.tier), icon(e.host)) +
    `<div class="steps"><span></span><span></span><span></span></div>`,
    { title: e.host, kv: [["URL", link(e.url)], ["Tier", `${e.tier}`]], note: "本文に一字一句ある引用の事実だけを残す（引用の照合）。" }, state);
}
function showQuestion(q) {
  const lines = q.split("\n"), opts = lines.filter((l) => /^\d+\.\s/.test(l));
  const d = chatAgent(`<div class="question">${esc(q)}${opts.length ? `<div class="opts">${opts.map((o) => `<button type="button" data-a="${esc(o.match(/^(\d+)/)[1])}">${esc(o)}</button>`).join("")}</div>` : ""}</div>`);
  d.querySelectorAll(".opts button").forEach((b) => b.addEventListener("click", () => sendAnswer(b.dataset.a)));
  statusEl = null;
}

// 報告の Markdown（見出し・箇条書き・太字・画像・URL・出典番号）を安全に描く
function renderReport(md) {
  const inline = (s) => esc(s)
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, src) => `<img src="${withKey(`/api/image?run=${encodeURIComponent(runId)}&src=${encodeURIComponent(src.replace(/&amp;/g, "&"))}`)}" alt="${alt}">`)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^"=])(https?:\/\/[^\s<）)]+)/g, (m, pre, u) => `${pre}<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`)
    .replace(/\[(\d{1,3})\]/g, '<button type="button" class="cite" data-src="$1">$1</button>');
  const out = []; let list = false;
  for (const line of md.split("\n")) {
    const l = line.trimEnd();
    if (/^\s*[-*]\s+/.test(l)) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(l.replace(/^\s*[-*]\s+/, ""))}</li>`); continue; }
    if (list) { out.push("</ul>"); list = false; }
    if (/^#{1,4}\s/.test(l)) out.push(`<h4>${inline(l.replace(/^#+\s*/, ""))}</h4>`);
    else if (l.trim()) out.push(`<p>${inline(l)}</p>`);
  }
  if (list) out.push("</ul>");
  return out.join("");
}
function wireCites(el) {
  el?.querySelectorAll(".cite").forEach((b) => b.addEventListener("click", () => {
    const s = sources[b.dataset.src - 1]; if (!s) return;
    const id = order.find((k) => k.startsWith("p:") && sameUrl(k.slice(2), s.url)) || (N.sum && sameUrl(N.sum.url, s.url) ? "sum" : null);
    if (!id) return;
    reveal(id); select(id);
    if (view === "flow") FLOW.pageDetails(s.url); else jumpTo(id);
  }));
}

// ==== 流れの図（エージェントのフロー）====
// ノードと線は、イベントが起きたときに初めて生まれる（線が伸びてから、ノードが実るように膨らむ）。
// 配置: 左から「依頼 → 判定 → 判断 → 作業 → 集まったもの」の 5 列。作業は決まった順のうち出てきたものだけを縦に詰め、
//       判断・判定・依頼は作業の並びの真ん中に揃える。判断と作業の間の行きと帰りの線は、判断の右端に扇形に分けた平行な 2 本。
// 線のエフェクト: 制御の流れ（判断 → 作業）は尾を引く点、情報の流れ（検索結果・ページ・事実・画像・出典）は線を流れる粒。
const graph = $("graph"), gsvg = $("gedges");
const SVGNS = "http://www.w3.org/2000/svg";
const STAGE = { check_feasibility: "feas", decide: "hub", search: "search", read_pages: "read", find_images: "images",
  check_conflicts: "conflicts", ask_user: "ask", respond: "respond", report_infeasible: "infeasible" };
const LLM_STAGES = new Set(["feas", "hub", "conflicts", "respond", "infeasible"]);
const ACTION_STAGE = { search: "search", read_pages: "read", find_images: "images", check_conflicts: "conflicts", ask_user: "ask", respond: "respond" };
const STAGE_ORDER = ["ask", "search", "read", "images", "conflicts", "respond"];
const ROW_DATA = { read: ["pages", "cov"], images: ["imgs"], respond: ["end"] };   // 作業の行に並べる、集まったもののノード
const COL = { start: 0, feas: 210, infeasible: 210, hub: 420, stage: 740, data1: 1050, data2: 1350 };
const FLOW_DEF = {
  start:      { col: "start", w: 180, label: "依頼", cls: "start terminal" },
  feas:       { col: "feas", w: 170, label: "判定" },
  hub:        { col: "hub", w: 230, label: "判断", cls: "hub" },
  infeasible: { col: "infeasible", w: 190, label: "調べられないことの報告" },
  ask:        { col: "stage", w: 240, label: "聞き返し" },
  search:     { col: "stage", w: 240, label: "検索" },
  read:       { col: "stage", w: 240, label: "ページを読む" },
  images:     { col: "stage", w: 240, label: "画像" },
  conflicts:  { col: "stage", w: 240, label: "食い違い" },
  respond:    { col: "stage", w: 240, label: "報告" },
  end:        { col: "data1", w: 180, label: "終わり", cls: "terminal" },
  pages:      { col: "data1", w: 250, label: "読んだページ", cls: "data" },
  cov:        { col: "data2", w: 250, label: "網羅表", cls: "data" },
  imgs:       { col: "data1", w: 250, label: "画像の候補", cls: "data" },
};
const FLOW = {
  reset() {
    graph.querySelectorAll(".fnode").forEach((e) => e.remove());
    gsvg.innerHTML = "";
    this.fx = document.createElementNS(SVGNS, "g"); gsvg.appendChild(this.fx);   // 点と粒（毎フレーム描き直す）
    this.n = {}; this.e = {}; this.tokens = []; this.particles = []; this.ghosts = []; this.cur = null; this.lastFrom = null;
    this.pageChip = {}; this.covRow = {}; this.thumb = {}; this.visits = {}; this.seconds = {};
  },
  // ノードを（なければ）作る。生まれたノードは、その場所で実るように膨らむ
  node(id) {
    if (this.n[id]) return this.n[id];
    const d = FLOW_DEF[id];
    const el = document.createElement("div");
    el.className = `fnode ${d.cls || ""}`; el.style.setProperty("--w", d.w + "px");
    el.innerHTML = `<div class="k"><span>${d.label}</span><span class="row"><span class="cnt"></span><span class="st"></span></span></div>
      <div class="t"></div><div class="s"></div>${d.cls?.includes("data") ? this.dataBody(id) : `<ul class="hist"></ul>`}`;
    el.tabIndex = 0; el.setAttribute("role", "button");
    el.addEventListener("click", (ev) => { if (!ev.target.closest(".pchip")) this.details(id); });
    graph.appendChild(el);
    const n = this.n[id] = { id, el, ...d, hist: [], cur: { x: 0, y: 0 }, tgt: { x: 0, y: 0 }, born: instant || reduce ? -1e9 : performance.now() };
    this.visits[id] = 0; this.seconds[id] = 0;
    this.layout();
    n.cur = { ...n.tgt };
    if (!instant && !reduce) { el.style.opacity = "0"; el.style.transform = `translate(${n.cur.x}px, ${n.cur.y}px) scale(0.55)`; }
    if (view === "flow") $("flowEmpty").hidden = true;
    return n;
  },
  // 線を（なければ）作る。生まれた線は、元のノードから伸びていく
  edge(a, b, kind = "") {
    const key = `${a}>${b}`;
    if (this.e[key]) return this.e[key];
    this.node(a); this.node(b);
    const path = document.createElementNS(SVGNS, "path");
    gsvg.insertBefore(path, this.fx);
    let label = null;
    if (a === "hub" && kind !== "back") {
      label = document.createElementNS(SVGNS, "text"); label.setAttribute("class", "glabel"); label.setAttribute("text-anchor", "middle");
      gsvg.insertBefore(label, this.fx);
    }
    const target = this.n[b];
    if (performance.now() - target.born < 150) target.delay = ARRIVE;   // 線が届いてから現れる
    return (this.e[key] = { a, b, kind, path, label, count: 0, over: 0, born: instant || reduce ? -1e9 : performance.now() });
  },
  dataBody(id) {
    if (id === "pages") return `<div class="chipgrid"></div>`;
    if (id === "cov") return `<div class="covrows"></div>`;
    return `<div class="thumbs"></div>`;
  },
  layout() {
    if ($("graph").hidden) return;   // 非表示の間は高さが測れない。表示を切り替えたときに並べ直す
    const h = (id) => this.n[id]?.el.offsetHeight || 0;
    const rows = STAGE_ORDER.filter((s) => this.n[s]);
    let y = 0;
    for (const s of rows) {
      const members = (ROW_DATA[s] || []).filter((m) => this.n[m]);
      const rh = Math.max(h(s), ...members.map(h));
      this.n[s].tgt = { x: COL.stage, y };
      members.forEach((m) => { this.n[m].tgt = { x: COL[FLOW_DEF[m].col], y }; });
      y += rh + 30;
    }
    const mid = rows.length ? (y - 30) / 2 : 40;
    for (const id of ["start", "feas", "hub"]) if (this.n[id]) this.n[id].tgt = { x: COL[FLOW_DEF[id].col], y: mid - h(id) / 2 };
    if (this.n.infeasible) {
      const f = this.n.feas;
      this.n.infeasible.tgt = { x: COL.infeasible, y: (f ? f.tgt.y + h("feas") : mid) + 70 };
      if (this.n.end && !this.n.respond) this.n.end.tgt = { x: COL.data1, y: this.n.infeasible.tgt.y };
    }
    if (instant) Object.values(this.n).forEach((n) => { n.cur = { ...n.tgt }; });
  },
  setText(id, t, s) { const n = this.n[id]; if (!n) return; if (t !== undefined) n.el.querySelector(".t").textContent = t; if (s !== undefined) n.el.querySelector(".s").textContent = s; },
  setState(id, st) {
    const n = this.n[id]; if (!n) return;
    n.el.classList.remove("running", "llm", "waiting", "done", "failed");
    n.el.classList.add(st);
  },
  push(id, line) {   // ノードの中の最近の出来事（新しいものを上に、3 つまで）。すべては詳細に残す
    const n = this.n[id]; if (!n) return;
    n.hist.unshift(line);
    const ul = n.el.querySelector(".hist"); if (ul) ul.innerHTML = n.hist.slice(0, 3).map((x) => `<li>${esc(x)}</li>`).join("");
    this.layout();
  },
  count(id) {
    const n = this.n[id]; if (!n) return;
    n.el.querySelector(".cnt").textContent = `×${this.visits[id]}${this.seconds[id] ? ` · ${Math.round(this.seconds[id])} 秒` : ""}`;
  },
  // 作業が移る: 線を（なければ）作り、尾を引く点を流して回数を数える
  go(to) {
    const from = this.cur;
    this.node(to);
    if (from && from !== to) {
      const back = to === "hub" && STAGE_ORDER.includes(from) && from !== "respond";
      const e = this.edge(from, to, back ? "back" : "");
      e.count++;
      this.tokens.push({ e, t0: performance.now() + (e.born > performance.now() - 300 ? STEM * 1000 : 0) });
      if (!["failed", "waiting"].some((c) => this.n[from].el.classList.contains(c))) this.setState(from, "done");
    }
    this.lastFrom = from; this.cur = to;
    this.visits[to]++; this.count(to);
    this.setState(to, to === "ask" ? "waiting" : LLM_STAGES.has(to) ? "llm" : "running");
  },
  // 情報の流れ: 線の上に粒を n 個、少しずつずらして流す。届いたら onArrive を呼ぶ
  flow(a, b, n = 1, onArrive = null, kind = "data") {
    if (!this.n[a] || !this.n[b]) return;
    const e = this.edge(a, b, kind);
    const start = performance.now() + Math.max(0, e.born + STEM * 1000 - performance.now());
    for (let i = 0; i < Math.min(n, 12); i++) {
      const p = { e, t0: start + i * 140, dur: 900, done: false, onArrive: i < n ? onArrive : null };
      if (instant || reduce) { onArrive?.(i); continue; }
      this.particles.push({ ...p, i });
    }
    if (n > 12 && onArrive) for (let i = 12; i < n; i++) setTimeout(() => onArrive(i), instant || reduce ? 0 : 900 + 12 * 140);
  },
  ghost(choice) {   // LLM が選んだが、プログラムが変えた行き先（その線を点線で一瞬出して消す。まだない作業なら文字で出す）
    const to = ACTION_STAGE[choice];
    this.ghosts.push({ to, label: ACTION[choice] || choice, t0: performance.now() });
  },
  on(e) {
    const f = this;
    switch (e.type) {
      case "run.start": f.node("start"); f.setState("start", "done"); f.setText("start", e.request, runId); f.cur = "start"; break;
      case "step.start": {
        const id = STAGE[e.node]; if (!id) break;
        if (id === "ask" && f.cur === "ask") { f.setState("ask", "running"); break; }   // 答えで再開した
        f.go(id);
        if (id === "read") f.setText("read", "読む順番を決めている", "");
        if (id === "respond") f.setText("respond", "確かめた事実だけで報告を書いている", "");
        if (id === "conflicts") f.setText("conflicts", "別々のサイトの事実を比べている", "");
        break;
      }
      case "step.end": {
        const id = e.node === "decide" ? "hub" : STAGE[e.node]; if (!id || !f.n[id]) break;
        f.seconds[id] += e.seconds || 0; f.count(id);
        if (e.error) { f.setState(id, "failed"); f.setText(id, undefined, e.error); }
        break;
      }
      case "feasibility": {
        const v = { yes: "調べられる", partial: "一部は調べられない", no: "調べられない", unsure: "調べてみる" }[e.verdict] || e.verdict;
        f.setText("feas", v, e.reason); f.push("feas", v); break;
      }
      case "decision": {
        const llm = ACTION[e.llm?.action] || e.llm?.action || "", act = ACTION[e.action] || e.action;
        const changed = e.guard && e.llm?.action !== e.action;
        f.setText("hub", act + (e.action === "search" && e.query ? `「${e.query}」` : ""), changed ? `LLM は「${llm}」→ プログラムが変更` : "LLM の選択どおり");
        f.push("hub", changed ? `${llm} → ${act}（変更）` : act);
        if (changed) {
          f.ghost(e.llm?.action);
          const to = ACTION_STAGE[e.action];
          if (to) f.edge("hub", to).over++;
        }
        break;
      }
      case "search.start": f.setText("search", e.query, SEARCH_KIND[e.kind] || e.kind); f.push("search", e.query); break;
      case "search.done": {
        const rel = e.results.filter((r) => r.relevant).length;
        f.setText("search", undefined, e.failed || `結果 ${e.results.length} 件 · 対象を含む ${rel} 件`);
        f.flow("search", "hub", Math.max(1, rel), null, "back");   // 検索結果が判断へ戻る
        break;
      }
      case "aspects": {
        const cov = f.node("cov");
        cov.el.querySelector(".covrows").innerHTML = e.aspects.map((a) =>
          `<div class="covrow" data-a="${esc(a)}"><span class="m"></span><span class="nm">${esc(a)}</span><span class="sq"></span></div>`).join("");
        e.aspects.forEach((a) => { f.covRow[a] = cov.el.querySelector(`.covrow[data-a="${CSS.escape(a)}"]`); });
        f.setState("cov", "done"); f.setText("cov", `観点 ${e.aspects.length} つ`, `${e.subject_type} · ${e.origin}が作成`);
        f.setText("read", `対象の種類: ${e.subject_type}`, "観点を作った");
        f.edge(f.n.pages ? "pages" : "read", "cov", "data");
        f.layout();
        break;
      }
      case "databases": f.push("read", `データベース: ${e.used.join(" → ") || "なし"}（記録 ${e.records.length} 件）`); break;
      case "page.planned": case "page.start": {
        let chip = f.pageChip[e.url];
        if (!chip) {
          const pages = f.node("pages"); f.setState("pages", "done"); f.edge("read", "pages", "data");
          if (f.e["read>cov"] && !f.e["pages>cov"]) { const old = f.e["read>cov"]; old.path.remove(); delete f.e["read>cov"]; f.edge("pages", "cov", "data"); }
          chip = document.createElement("div"); chip.className = "pchip planned"; chip.title = e.host;
          chip.innerHTML = icon(e.host); chip.addEventListener("click", () => f.pageDetails(e.url));
          pages.el.querySelector(".chipgrid").appendChild(chip); f.pageChip[e.url] = chip;
          f.updatePages(); f.layout();
        }
        if (e.type === "page.start") { chip.className = "pchip running"; f.setText("read", e.host, e.db ? "データベースの記録を読んでいる" : "ページを取得している"); }
        break;
      }
      case "page.fetched": { const c = f.pageChip[e.url]; if (c && !e.ok) { c.className = "pchip failed"; f.push("read", `${host(e.url)}: ${e.reason}`); f.updatePages(); } break; }
      case "page.llm": { const c = f.pageChip[e.url]; if (c) c.className = "pchip llm"; f.setText("read", host(e.url), "LLM が引用付きで事実を抜き出している"); break; }
      case "page.done": {
        const c = f.pageChip[e.url];
        f.flow("read", "pages", 1, () => {   // 読み終えたページが「読んだページ」へ
          if (c) { c.className = "pchip done"; if (e.facts.length) c.insertAdjacentHTML("beforeend", `<span class="n">${e.facts.length}</span>`); }
          f.updatePages();
          // 照合できた事実が、1 件ずつ網羅表の観点へ流れ、届いたら観点の四角になる
          if (f.n.cov) f.flow("pages", "cov", e.facts.length, (i) => {
            const x = e.facts[i], row = x && f.covRow[x.aspect]; if (!row) return;
            const sq = document.createElement("i"); if (e.tier !== 1) sq.className = "t2"; sq.title = x.text; row.querySelector(".sq").appendChild(sq);
          });
        });
        f.push("read", `${host(e.url)}: 事実 ${e.facts.length} 件`);
        break;
      }
      case "coverage":
        e.rows.forEach((r) => { const row = f.covRow[r.aspect]; if (row) row.className = "covrow " + (r.status === "確認済み" ? "ok" : r.status === "出典不足" ? "weak" : ""); });
        f.setText("cov", undefined, `${e.rows.filter((r) => r.status === "確認済み").length} / ${e.rows.length} 観点を確認済み`);
        break;
      case "image.candidates": f.setText("images", `${e.ranked} 候補`, `${e.fetching} 件を取得して確かめる`); break;
      case "image.fetched": {
        if (!e.ok) break;
        f.node("imgs"); f.setState("imgs", "done"); f.edge("images", "imgs", "data");
        const th = document.createElement("div"); th.className = "th"; th.title = e.title || e.src;
        f.thumb[e.src] = th;
        // 取ってきた画像が粒になって「画像の候補」へ流れ、届いたところでサムネイルが現れる
        f.flow("images", "imgs", 1, () => {
          th.innerHTML = NEEDS_PERMISSION.test(e.license || "") ? "許可が<br>必要"
            : `<img src="${withKey(`/api/image?run=${encodeURIComponent(runId)}&src=${encodeURIComponent(e.src)}`)}" alt="" onerror="this.remove()">`;
          if (NEEDS_PERMISSION.test(e.license || "")) th.classList.add("lock");
          f.n.imgs.el.querySelector(".thumbs").appendChild(th); f.layout();
        });
        break;
      }
      case "image.llm": { const th = f.thumb[e.src]; if (th) th.classList.add("llm"); f.setText("images", undefined, "VLM が対象かどうか確かめている"); break; }
      case "image.done": {
        const th = f.thumb[e.src]; if (!th) break;
        th.classList.remove("llm"); th.dataset.done = e.level;
        const ok = e.level !== "不採用";
        // 判定が出たサムネイルは、一瞬縁取りが光る。不採用は点線の枠と「不採用」のラベル（画像は色付きのまま）
        th.classList.add(ok ? "ok" : "ng", "flash"); setTimeout(() => th.classList.remove("flash"), 900);
        if (NEEDS_PERMISSION.test(e.license || "")) { th.classList.add("lock"); th.innerHTML = "許可が<br>必要"; }
        else if (!ok) th.insertAdjacentHTML("beforeend", `<span class="un">不採用</span>`);
        th.title = `${e.level}: ${e.description || e.reason || ""}`;
        f.push("images", `${e.level}: ${e.description || e.reason || ""}`);
        break;
      }
      case "image.final": {
        // 取得はできたが、VLM で確かめずに打ち切った候補（確認済みが 2 枚そろった、または確かめる数の上限に達した）
        const ths = Object.entries(f.thumb), checked = ths.filter(([, th]) => th.dataset.done);
        const confirmed = checked.filter(([, th]) => th.dataset.done === "確認済み").length;
        const why = confirmed >= 2 ? "確認済みの画像が 2 枚そろったので、残りは確かめていない" : "確かめる数の上限（6 枚）に達したので、残りは確かめていない";
        ths.filter(([, th]) => !th.dataset.done).forEach(([src, th]) => {
          th.classList.add("unchecked"); th.title = `未確認: ${why}`;
          if (!th.classList.contains("lock")) th.insertAdjacentHTML("beforeend", `<span class="un">未確認</span>`);
        });
        f.unchecked = ths.length - checked.length; f.uncheckedWhy = why;
        f.setText("images", `載せる ${e.shown.length} 枚`, `確かめた ${checked.length} 枚`);
        f.setText("imgs", `採用 ${e.shown.length} 枚`, `確かめた ${checked.length} · 未確認 ${f.unchecked}`);
        break;
      }
      case "conflicts":
        f.setText("conflicts", `食い違い ${e.conflicts.length} 件`, `比べた観点 ${e.aspects.length}`);
        e.conflicts.forEach((c) => f.push("conflicts", c.topic));
        if (e.conflicts.length) f.flow("conflicts", "hub", e.conflicts.length, null, "back");
        break;
      case "question": f.setState("ask", "waiting"); f.setText("ask", e.question.split("\n")[0], "利用者の答えを待っている"); break;
      case "answer": f.setState("ask", "running"); f.push("ask", `答え: ${e.answer}`); f.setText("ask", undefined, `答え: ${e.answer}`); break;
      case "report": f.setText("respond", `出典 ${(e.sources || []).length} 件`, "報告を書いた"); f.reportSources = (e.sources || []).length; break;
      case "run.end":
        f.go("end"); f.setState("end", "done"); f.setText("end", `${Math.round(e.t)} 秒`, "報告をチャットに出した");
        if (f.n.respond) f.flow("respond", "end", Math.max(1, f.reportSources || 1), null, "");
        break;
      case "run.error": if (f.cur) { f.setState(f.cur, "failed"); f.setText(f.cur, undefined, e.error); } break;
    }
  },
  updatePages() {
    const chips = Object.values(this.pageChip);
    const ok = chips.filter((c) => c.classList.contains("done")).length, ng = chips.filter((c) => c.classList.contains("failed")).length;
    this.setText("pages", `${chips.length} ページ`, `読めた ${ok} · 読めなかった ${ng}`);
  },
  // 線の形。判断と作業の間は、判断の右端に作業ごとの出口を扇形に分け、行きと帰りを平行な 2 本にする
  geometry(e) {
    const A = this.n[e.a], B = this.n[e.b];
    const box = (n) => ({ x: n.cur.x, y: n.cur.y, w: n.el.offsetWidth, h: n.el.offsetHeight });
    const a = box(A), b = box(B);
    const rows = STAGE_ORDER.filter((s) => this.n[s]);
    const hubY = (stage) => {
      const hub = box(this.n.hub), i = rows.indexOf(stage), k = rows.length;
      return hub.y + 16 + (k <= 1 ? 0.5 : i / (k - 1)) * (hub.h - 32);
    };
    const anchor = (n) => n.y + Math.min(24, n.h / 2);
    let p0, p3;
    if (e.a === "hub" && STAGE_ORDER.includes(e.b)) { p0 = { x: a.x + a.w, y: hubY(e.b) - 5 }; p3 = { x: b.x, y: anchor(b) - 5 }; }
    else if (e.b === "hub" && STAGE_ORDER.includes(e.a)) { p0 = { x: a.x, y: anchor(a) + 5 }; p3 = { x: b.x + b.w, y: hubY(e.a) + 5 }; }
    else if (e.a === "feas" && e.b === "infeasible") {
      p0 = { x: a.x + a.w / 2, y: a.y + a.h }; p3 = { x: b.x + b.w / 2, y: b.y };
      return [p0, { x: p0.x, y: p0.y + 30 }, { x: p3.x, y: p3.y - 30 }, p3];
    } else { p0 = { x: a.x + a.w, y: anchor(a) }; p3 = { x: b.x, y: anchor(b) }; }
    const mx = (p0.x + p3.x) / 2;
    return [p0, { x: mx, y: p0.y }, { x: mx, y: p3.y }, p3];
  },
  // 毎フレーム: ノードを目標の位置へ近づけて膨らませ、線を伸ばし、点と粒を流す
  draw(dt) {
    const t = performance.now();
    const k = instant || reduce ? 1 : 1 - Math.pow(0.0008, dt);
    for (const n of Object.values(this.n)) {
      n.cur.x += (n.tgt.x - n.cur.x) * k; n.cur.y += (n.tgt.y - n.cur.y) * k;
      const age = (t - n.born) / 1000, g = clamp((age - (n.delay || 0)) / RIPEN);
      n.el.style.transform = `translate(${n.cur.x.toFixed(1)}px, ${n.cur.y.toFixed(1)}px) scale(${grownScale(g).toFixed(3)})`;
      n.el.style.opacity = easeOut(g).toFixed(3);
    }
    const fmt = (p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
    const pens = [];
    // 集まったもののノードが忙しい間（ページを読んでいる、画像を確かめている）は、そこへの線とノードを光らせる
    const imgsBusy = Object.values(this.thumb).some((th) => th.classList.contains("llm")) || this.particles.some((q) => q.e.b === "imgs");
    const pagesBusy = Object.values(this.pageChip).some((c) => c.classList.contains("running") || c.classList.contains("llm"));
    this.n.imgs?.el.classList.toggle("busy", imgsBusy); this.n.pages?.el.classList.toggle("busy", pagesBusy);
    for (const e of Object.values(this.e)) {
      const full = e.pts = this.geometry(e);
      const grow = clamp((t - e.born) / 1000 / STEM);
      const [p0, p1, p2, p3] = grow >= 1 ? full : bezierTo(...full, easeInOut(grow));
      e.path.setAttribute("d", `M${fmt(p0)} C${fmt(p1)} ${fmt(p2)} ${fmt(p3)}`);
      if (grow < 1) pens.push(p3);   // 描いている途中の線の先に、鉛筆の先を出す
      const target = this.n[e.b];
      const hot = (this.cur === e.b && this.lastFrom === e.a && ["running", "llm", "waiting"].some((c) => target.el.classList.contains(c)))
        || (e.b === "imgs" && imgsBusy) || (e.b === "pages" && pagesBusy);
      const busyData = e.kind === "data" && this.particles.some((q) => q.e === e);
      e.path.setAttribute("class", `gedge ${e.kind} ${e.count || busyData ? "used" : ""} ${hot ? "hot" : ""} ${grow < 1 ? "growing" : ""}`);
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
    // LLM が選んだが、プログラムが変えた行き先: 点線（その作業がまだなければ文字）を 2.5 秒かけて消す
    this.ghosts = this.ghosts.filter((g) => {
      const u = (t - g.t0) / 2500; if (u >= 1 || !this.n.hub) return false;
      const op = (1 - u).toFixed(2), hub = this.n.hub;
      const e = g.to && this.e[`hub>${g.to}`];
      if (e?.pts) {
        const [p0, p1, p2, p3] = e.pts.map((p) => ({ x: p.x, y: p.y - 9 }));
        fx += `<path class="gedge ghost" opacity="${op}" d="M${fmt(p0)} C${fmt(p1)} ${fmt(p2)} ${fmt(p3)}"/>`;
      } else {
        fx += `<text class="glabel over" opacity="${op}" x="${(hub.cur.x + hub.el.offsetWidth + 16).toFixed(1)}" y="${(hub.cur.y - 10 - u * 14).toFixed(1)}">LLM の選択: ${esc(g.label)} ✕</text>`;
      }
      return true;
    });
    this.fx.innerHTML = fx;
  },
  bounds() {
    const ns = Object.values(this.n);
    if (!ns.length) return { x0: 0, y0: 0, x1: 400, y1: 300 };
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of ns) { x0 = Math.min(x0, n.tgt.x); y0 = Math.min(y0, n.tgt.y); x1 = Math.max(x1, n.tgt.x + n.el.offsetWidth); y1 = Math.max(y1, n.tgt.y + n.el.offsetHeight); }
    return { x0, y0, x1, y1 };
  },
  details(id) {
    const n = this.n[id];
    document.querySelectorAll(".fnode.sel").forEach((x) => x.classList.remove("sel")); n.el.classList.add("sel");
    const kv = [["通った回数", this.visits[id] ? `${this.visits[id]} 回` : ""], ["かかった時間", this.seconds[id] ? `${Math.round(this.seconds[id])} 秒` : ""]];
    let list = n.hist.map((x) => esc(x));
    if (id === "pages") list = Object.keys(this.pageChip).map((u) => `${esc(host(u))} — ${link(u)}`);
    if (id === "imgs") {
      list = Object.entries(this.thumb).map(([u, th]) => `${esc(th.dataset.done || "未確認")} — ${link(u)}`);
      if (this.unchecked) kv.push(["未確認", `${this.unchecked} 枚。${esc(this.uncheckedWhy)}`]);
    }
    showDetails({ title: n.label, kv, list, note: id === "pages" ? "アイコンを押すと、そのページで確かめた事実と引用が出ます。" : "" });
  },
  pageDetails(url) { const d = info["p:" + url]; if (d) showDetails(d); },
};
function bezierPoint(p0, p1, p2, p3, t) {
  const u = 1 - t;
  return { x: u * u * u * p0.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t * t * t * p3.x,
           y: u * u * u * p0.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t * t * t * p3.y };
}

// ---- 調査の開始・答え・一覧 ----
let source = null, waitingRun = null, liveState = "";   // liveState: 動いている調査の状態（running / paused / ""）
function setComposer(answerMode) {
  waitingRun = answerMode ? runId : null;
  $("composer").classList.toggle("answer", answerMode);
  $("input").placeholder = answerMode ? "質問への答えを入力（番号でも可）" : "調べたいことを入力";
  updateSend();
}
// 送信ボタンは、調査が動いている間（再生中も）は一時停止 / 再開のボタンになる
function busy() { return !waitingRun && (replay ? queue.length > 0 || !!source : liveState !== ""); }
function paused() { return replay ? !playing : liveState === "paused"; }
function updateSend() {
  const b = $("sendBtn"), stop = busy(), p = paused();
  b.setAttribute("aria-label", stop ? (p ? "再開" : "一時停止") : "送る"); b.title = b.getAttribute("aria-label");
  b.classList.toggle("pause", stop && !p); b.classList.toggle("resume", stop && p);
}
async function togglePause() {
  if (replay) { playing = !playing; updateSend(); now(playing ? "再生している" : "一時停止している"); return; }
  const action = liveState === "paused" ? "resume" : "pause";
  try { await api(`/api/runs/${runId}/${action}`, {}); }
  catch (err) { chatAgent(`<p>${action === "pause" ? "止められませんでした" : "再開できませんでした"}: ${esc(err.message)}</p>`); }
}
async function sendAnswer(text) {
  if (!waitingRun || !text) return;
  try { await api(`/api/runs/${waitingRun}/answer`, { answer: text }); setComposer(false); }
  catch (err) { chatAgent(`<p>答えを送れませんでした: ${esc(err.message)}</p>`); }
}
$("composer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (busy()) { togglePause(); return; }
  const text = input.value.trim(); if (!text) return;
  input.value = ""; growInput();
  if (waitingRun) { sendAnswer(text); return; }
  try { const r = await api("/api/runs", { request: text }); open(r.id, true); loadRuns(); }
  catch (err) { chatAgent(`<p>調査を始められませんでした: ${esc(err.message)}</p>`); }
});
function newResearch() {
  closeSource(); reset(); runId = null; $("chat").innerHTML = ""; setEmpty(true);
  replay = false; liveState = ""; setComposer(false); highlightRun(); $("input").focus();
}
$("newBtn").addEventListener("click", newResearch);
$("plusBtn").addEventListener("click", newResearch);
$("runsBtn").addEventListener("click", () => { document.querySelector(".app").classList.toggle("noside"); setTimeout(() => fitMode(true), 50); });

// 入力欄: Enter で送る（Shift+Enter は改行。日本語の変換を確定する Enter では送らない）。行が増えたら高さを伸ばす
const input = $("input");
function growInput() { input.style.height = "auto"; input.style.height = Math.min(180, input.scrollHeight) + "px"; }
input.addEventListener("input", growInput);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); $("composer").requestSubmit(); }
});

// 研究室の LLM サーバーと検索につながるか（入力欄の下に出す）
async function checkConnection() {
  try {
    const st = await api("/api/status");
    $("modelName").textContent = st.model; $("modelName").title = `調査エージェント（research-agent）が使っているモデル: ${st.model}`;
    const ok = st.llm.ok && st.search.ok;
    $("conn").className = "conn " + (ok ? "ok" : "ng");
    $("connText").textContent = ok ? "" : !st.llm.ok ? "VPN を確認" : "検索につながらない";
    $("conn").title = `LLM: ${st.llm.ok ? `応答 ${st.llm.seconds} 秒` : st.llm.error || "応答なし"} / 検索: ${st.search.ok ? "応答あり" : st.search.error || "応答なし"}`;
  } catch (err) { $("conn").className = "conn ng"; $("connText").textContent = "エージェントにつながらない"; }
}
checkConnection(); setInterval(checkConnection, 30000);
// 最近の依頼（調査の一覧の新しい順。同じ依頼は 1 つにまとめ、3 つまで）。押すと入力欄に入る（送るのは利用者）
function showRecent(runs) {
  const seen = new Set(), recent = [];
  for (const r of runs) {
    const q = (r.request || "").trim();
    if (q && !seen.has(q)) { seen.add(q); recent.push(q); }
    if (recent.length >= 3) break;
  }
  const box = $("examples");
  box.innerHTML = recent.length ? `<span class="lbl">最近の依頼</span>` + recent.map((q) => `<button type="button" class="ex" title="${esc(q)}">${esc(q)}</button>`).join("") : "";
  box.querySelectorAll(".ex").forEach((b) => b.addEventListener("click", () => { input.value = b.title; growInput(); input.focus(); }));
}

// 再生: 終わった調査は、記録の時刻どおりに（速さを掛けて）流す。動いている調査は届いたらすぐ描く
let queue = [], clock = 0, speed = 8, playing = true, replay = false, lastTs = null, stopAt = Infinity;
function closeSource() { if (source) { source.close(); source = null; } queue = []; }
function open(id, live) {
  closeSource(); reset(); runId = id; $("chat").innerHTML = "";
  replay = !live; clock = 0; playing = true; liveState = live ? "running" : "";
  setComposer(false); highlightRun();
  source = new EventSource(withKey(`/api/runs/${encodeURIComponent(id)}/events`));
  source.onmessage = (m) => { const e = JSON.parse(m.data); if (replay) queue.push(e); else handle(e); };
  source.addEventListener("end", () => { if (source) { source.close(); source = null; } updateSend(); });
  source.addEventListener("missing", () => { if (source) { source.close(); source = null; } chatAgent("<p>この調査の記録が見つかりません。</p>"); });
}
function frame(ts) {
  if (lastTs === null) lastTs = ts;
  const dt = Math.min(0.1, (ts - lastTs) / 1000); lastTs = ts;
  if (replay && playing && queue.length) {
    if (speed === 0) { instant = true; while (queue.length && queue[0].t <= stopAt) handle(queue.shift()); layout(); instant = false; }
    else {
      clock += dt * speed;
      if (queue[0].t - clock > 20) clock = queue[0].t;   // 利用者の答えを待った時間などの長い空白は飛ばす
      while (queue.length && queue[0].t <= clock) handle(queue.shift());
    }
    if (!queue.length && !source) updateSend();   // 再生し終えたら、送信ボタンに戻す
  }
  animate(dt);
  requestAnimationFrame(frame);
}

async function loadRuns() {
  try {
    const { runs } = await api(withKey("/api/runs"));
    const label = { running: "調べている", paused: "一時停止中", waiting: "答え待ち", done: "完了", error: "エラー", stopped: "途中で止まった" };
    $("runs").innerHTML = runs.map((r) => `<button type="button" class="run" data-id="${esc(r.id)}" data-live="${["running", "waiting", "paused"].includes(r.status)}">
      <span class="r1">${esc(r.request || "(依頼なし)")}</span><span class="r2">${esc(r.id.slice(0, 13).replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2}).*/, "$2/$3 $4:$5"))} · ${label[r.status] || r.status}</span></button>`).join("")
      || `<p class="r2" style="padding:8px;color:var(--muted)">まだ調査はありません</p>`;
    $("runs").querySelectorAll(".run").forEach((b) => b.addEventListener("click", () => open(b.dataset.id, b.dataset.live === "true")));
    showRecent(runs);
    highlightRun();
  } catch (err) { $("runs").innerHTML = `<p style="padding:8px;color:var(--muted)">一覧を読めません: ${esc(err.message)}</p>`; }
}
function highlightRun() { $("runs").querySelectorAll(".run").forEach((b) => b.classList.toggle("on", b.dataset.id === runId)); }

window.addEventListener("resize", () => N && layout());
reset(); loadRuns(); requestAnimationFrame(frame);
const params = new URLSearchParams(location.search);
if (params.has("speed")) speed = +params.get("speed") || 0;   // 再生の速さ（0 = すぐ全部を出す。確認用）
if (params.has("at")) stopAt = +params.get("at");   // この時刻（秒）の状態で止める
const first = params.get("run");
if (first) api(withKey("/api/runs")).then(({ runs }) => {
  const r = runs.find((x) => x.id === first);
  open(first, !!r && ["running", "waiting", "paused"].includes(r.status));
}).catch(() => open(first, false));
})();
