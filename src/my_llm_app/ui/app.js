// テレメトリー画面（docs/telemetry_design.md）。
// サーバーから届くイベント（/api/runs/{id}/events）を受け取り、作業の流れを左から右へ伸びる木として描く。
// 動いている調査はそのまま、終わった調査は記録を再生する（速さを選べる）。
(() => {
"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("canvas"), svg = $("edges"), flow = $("flow");
const emptyChat = $("emptyChat");   // チャットを空にしても、最初の案内は取っておいて使い直す
const COLW = [170, 250, 236, 236, 236];   // 深さごとの列の幅（依頼・判断・作業・ページ・データベースの記録）
const GAPX = 44, GAPY = 12, PAD = 24, LIMIT = 900;
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
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
let N, order, info, cov, pagesOk, follow, aim, jump, selected, round, work, dbNode, failGroup, spans, maxT, runId, statusEl, questions,
    sources, instant, rounds;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return u; } };
const tier = (t) => t ? `<span class="tier${t === 1 ? " t1" : ""}">T${t}</span>` : "";
function icon(h) {
  const letter = esc((h || "?").replace(/^www\./, "")[0]?.toUpperCase() || "?");
  return `<img class="ico" src="/api/favicon?host=${encodeURIComponent(h)}" alt="" onerror="this.outerHTML='<span class=&quot;ico letter&quot;>${letter}</span>'">`;
}
const card = (kind, title, sub, right = "", lead = "") =>
  `<div class="k"><span>${kind}</span><span class="row">${right}<span class="st"></span></span></div><div class="row">${lead}<span class="t">${esc(title)}</span></div><div class="s">${esc(sub)}</div>`;

function reset() {
  canvas.querySelectorAll(".node").forEach((e) => e.remove());
  document.querySelectorAll(".fly").forEach((e) => e.remove());
  $("gantt").querySelectorAll(".span").forEach((e) => e.remove());
  svg.innerHTML = ""; $("cols").innerHTML = ""; $("boardSum").textContent = "";
  N = {}; order = []; info = {}; cov = {}; pagesOk = 0; follow = true; aim = null; jump = null; selected = null; round = null; work = null;
  dbNode = {}; failGroup = {}; spans = []; maxT = 0; statusEl = null; questions = new Set(); sources = []; instant = false; rounds = 0;
  $("drawer").classList.remove("open"); $("backTo").hidden = true; $("flowEmpty").hidden = false;
  $("pageTxt").textContent = "0"; $("pageBar").style.width = "0%"; $("timeBar").style.width = "0%"; $("timeTxt").textContent = "0";
  $("ganttTotal").textContent = ""; now("待機中", false); flow.scrollLeft = 0; flow.scrollTop = 0;
}

// ---- 木 ----
function add(id, parent, depth, html, details, cls = "") {
  if (N[id]) return N[id].el;
  $("flowEmpty").hidden = true;
  const el = document.createElement("div");
  el.className = `node born ${cls}`; el.style.setProperty("--w", COLW[depth] + "px"); el.innerHTML = html;
  el.tabIndex = 0; el.setAttribute("role", "button");
  el.addEventListener("click", (e) => { if (!e.target.closest(".toggle")) select(id); });
  el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(id); } });
  canvas.appendChild(el);
  const p = N[parent];
  const start = p ? { x: p.cur.x + COLW[p.depth] - 20, y: p.cur.y + 4 } : { x: PAD, y: PAD };
  N[id] = { id, parent, depth, el, kids: [], cur: { ...start }, tgt: { ...start }, collapsed: false, hidden: false, extra: [] };
  if (p) p.kids.push(id);
  order.push(id); info[id] = details || { title: id };
  layout();
  if (instant || reduce) el.classList.remove("born"); else requestAnimationFrame(() => el.classList.remove("born"));
  if (follow) aim = id;
  slots();
  return el;
}
function layout() {
  const root = N.req; if (!root) return;
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
    const first = N[kids[0]], last = N[kids[kids.length - 1]];
    const mid = (first.tgt.y + last.tgt.y + (last.el.offsetHeight || 50)) / 2;
    n.tgt = { x: colX[n.depth], y: Math.max(top, mid - h / 2) };
    if (n.tgt.y + h + GAPY > y) y = n.tgt.y + h + GAPY;
  }
  root.kids.forEach((k) => place(k, false));
  root.tgt = { x: colX[0], y: PAD };
  const vis = order.filter((id) => !N[id].hidden);
  const maxY = Math.max(...vis.map((id) => N[id].tgt.y + N[id].el.offsetHeight));
  const maxX = Math.max(...vis.map((id) => N[id].tgt.x + COLW[N[id].depth]));
  canvas.style.width = maxX + PAD * 4 + "px"; canvas.style.height = maxY + PAD * 3 + "px";
  svg.setAttribute("width", maxX + PAD * 4); svg.setAttribute("height", maxY + PAD * 3);
  if (instant) order.forEach((id) => { N[id].cur = { ...N[id].tgt }; });
}
function setState(id, st) {
  const n = N[id]; if (!n) return;
  n.el.classList.remove("planned", "running", "llm", "waiting", "done", "failed");
  if (st) n.el.classList.add(st);
  slots();
}
function setSub(id, text) { const s = N[id]?.el.querySelector(".s"); if (s) s.textContent = text; }
function setTitle(id, text) { const s = N[id]?.el.querySelector(".t"); if (s) s.textContent = text; }
function step(id, i, cls) { const s = N[id]?.el.querySelectorAll(".steps span")[i]; if (s) s.className = cls; }
function slots() {
  const busy = order.filter((id) => N[id].el.classList.contains("llm")).length;
  $("slot0").classList.toggle("on", busy >= 1); $("slot1").classList.toggle("on", busy >= 2);
}
function collapse(id, yes) {
  const n = N[id]; if (!n) return; n.collapsed = yes;
  const t = n.el.querySelector(".toggle"); if (t) { t.hidden = !n.kids.length; t.textContent = yes ? `${n.kids.length} 件 ▸` : "畳む ▾"; }
  layout();
}
function reveal(id) { let p = N[id]?.parent; while (p) { if (N[p].collapsed) collapse(p, false); p = N[p].parent; } }

// 毎フレーム: 目標の位置へ滑らかに近づけ、線を引き直す
function animate(dt) {
  if (!N) return;
  const k = instant || reduce ? 1 : 1 - Math.pow(0.0008, dt);
  for (const id of order) {
    const n = N[id];
    n.cur.x += (n.tgt.x - n.cur.x) * k; n.cur.y += (n.tgt.y - n.cur.y) * k;
    n.el.style.transform = `translate(${n.cur.x.toFixed(1)}px, ${n.cur.y.toFixed(1)}px)`;
  }
  let d = ""; const live = [], rep = [];
  const anchorY = (n) => n.cur.y + Math.min(22, n.el.offsetHeight / 2);
  const curve = (x1, y1, x2, y2) => { const mx = x1 + (x2 - x1) / 2; return `M${x1.toFixed(1)},${y1.toFixed(1)} C${mx.toFixed(1)},${y1.toFixed(1)} ${mx.toFixed(1)},${y2.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`; };
  for (const id of order) {
    const n = N[id]; if (!n.parent || n.hidden) continue;
    const p = N[n.parent];
    const path = curve(p.cur.x + p.el.offsetWidth, anchorY(p), n.cur.x, anchorY(n));
    if (n.el.classList.contains("running") || n.el.classList.contains("llm")) live.push(path); else d += path;
    for (const other of n.extra) {   // 転載: ほぼ同じ文章のページどうしを点線で結ぶ
      const o = N[other]; if (!o || o.hidden) continue;
      rep.push(curve(n.cur.x + n.el.offsetWidth, anchorY(n) + 8, o.cur.x + o.el.offsetWidth, anchorY(o) + 8));
    }
  }
  svg.innerHTML = `<path d="${d}"/>` + live.map((p) => `<path class="live" d="${p}"/>`).join("") + rep.map((p) => `<path class="rep" d="${p}"/>`).join("");
  const target = follow ? aim : jump;
  if (target && N[target]) {
    const n = N[target];
    const tx = Math.max(0, n.cur.x - flow.clientWidth * 0.42), ty = Math.max(0, n.cur.y - flow.clientHeight * 0.4);
    const f = instant || reduce ? 1 : Math.min(1, dt * 4);
    flow.scrollLeft += (tx - flow.scrollLeft) * f; flow.scrollTop += (ty - flow.scrollTop) * f;
    if (!follow && Math.abs(tx - flow.scrollLeft) < 2 && Math.abs(ty - flow.scrollTop) < 2) jump = null;
  }
}
["wheel", "touchstart", "pointerdown"].forEach((ev) => flow.addEventListener(ev, (e) => {
  if (e.target.closest(".node, .backto")) return;
  follow = false; $("backTo").hidden = false;
}, { passive: true }));
$("backTo").addEventListener("click", () => { follow = true; $("backTo").hidden = true; aim = order[order.length - 1]; });

// ---- 上の帯・チャット・網羅表・時間の帯 ----
function now(text, busy = true) {
  $("nowText").textContent = text; $("pulse").classList.toggle("idle", !busy);
  const txt = statusEl && statusEl.querySelector(".txt"); if (txt) txt.textContent = text;
}
function chatUser(text) { emptyChat.hidden = true; const d = document.createElement("div"); d.className = "u"; d.textContent = text; $("chat").appendChild(d); scrollChat(); }
function chatAgent(html) {
  emptyChat.hidden = true;
  const d = document.createElement("div"); d.className = "a"; d.innerHTML = `<div class="avatar">R</div><div class="body">${html}</div>`;
  $("chat").appendChild(d); scrollChat(); return d;
}
function scrollChat() { $("chat").scrollTop = $("chat").scrollHeight; }
function statusBubble() {
  if (!statusEl) statusEl = chatAgent(`<div class="status"><span class="dots"><i></i><i></i><i></i></span><span class="txt">調べています</span></div>`);
}
function buildBoard(aspects) {
  $("cols").innerHTML = aspects.map((a, i) => `<div class="col" id="col${i}"><div class="h"><span>${esc(a)}</span><span class="mono" id="cn${i}">0</span></div>
    <div class="s2" id="cs${i}">未発見</div><div class="chips" id="cc${i}"></div></div>`).join("");
  cov = {}; aspects.forEach((a, i) => (cov[a] = i));
}
function flyFact(nodeId, f, t, delay) {
  const i = cov[f.aspect]; if (i === undefined) return;
  const drop = () => {
    const sq = document.createElement("i"); if (t !== 1) sq.className = "t2"; sq.title = f.text; $("cc" + i)?.appendChild(sq);
    const n = $("cn" + i); if (n) n.textContent = $("cc" + i).children.length;   // 状態（確認済みなど）は coverage のイベントで決まる
  };
  if (instant || reduce || !N[nodeId] || N[nodeId].hidden) { drop(); return; }
  setTimeout(() => {
    const from = N[nodeId].el.getBoundingClientRect(), to = $("col" + i).getBoundingClientRect();
    const chip = document.createElement("div"); chip.className = "fly"; chip.textContent = f.text.length > 13 ? f.text.slice(0, 13) + "…" : f.text;
    chip.style.left = from.left + 12 + "px"; chip.style.top = from.top + 10 + "px"; document.body.appendChild(chip);
    requestAnimationFrame(() => { chip.style.transform = `translate(${to.left - from.left}px, ${to.top - from.top + 26}px) scale(.6)`; chip.style.opacity = "0"; });
    setTimeout(() => { chip.remove(); drop(); }, 900);
  }, delay);
}
function coverage(rows) {
  rows.forEach((r) => {
    const i = cov[r.aspect]; if (i === undefined) return;
    $("cn" + i).textContent = r.facts;
    $("col" + i).className = "col " + (r.status === "確認済み" ? "ok" : r.status === "出典不足" ? "weak" : "");
    $("cs" + i).textContent = r.status === "確認済み" ? "✓ 確認済み" : r.status + (r.independent > 1 ? `（${r.independent} サイト）` : "");
  });
  $("boardSum").textContent = `${rows.filter((r) => r.status === "確認済み").length} / ${rows.length} 観点を確認済み`;
}
function span(name, a, row) { const s = document.createElement("div"); s.className = "span" + (row ? " row2" : ""); s.textContent = name; $("gantt").appendChild(s); const o = { s, a, b: null, name }; spans.push(o); return o; }
function drawGantt(t) {
  maxT = Math.max(maxT, t);
  const total = Math.max(maxT, 1);
  for (const o of spans) {
    const b = o.b ?? maxT;
    o.s.style.left = (o.a / total) * 100 + "%"; o.s.style.width = Math.max(0.3, ((b - o.a) / total) * 100) + "%";
    o.s.textContent = (b - o.a) / total > 0.05 ? o.name : ""; o.s.title = `${o.name}: ${(b - o.a).toFixed(1)} 秒`;
  }
  $("cursor").style.left = "100%";
  $("timeTxt").textContent = Math.round(maxT); $("timeBar").style.width = Math.min(100, (maxT / LIMIT) * 100) + "%";
  $("ganttTotal").textContent = `${Math.round(maxT)} 秒`;
}

// ---- 詳細 ----
function select(id) {
  if (selected && N[selected]) N[selected].el.classList.remove("sel");
  selected = id; N[id].el.classList.add("sel");
  const d = info[id] || {};
  $("dTitle").textContent = d.title || id;
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
const stepStart = {};
function handle(e) {
  drawGantt(e.t || 0);
  const H = HANDLERS[e.type]; if (H) H(e);
}
const HANDLERS = {
  "run.start": (e) => {
    chatUser(e.request); statusBubble();
    add("req", null, 0, `<div class="k"><span>依頼</span><span class="st"></span></div><div class="t">${esc(e.request)}</div><div class="s">${esc(runId)}</div>`,
      { title: "依頼", kv: [["依頼文", esc(e.request)], ["実行の ID", `<span class="mono">${esc(runId)}</span>`]] }, "root");
    now("調査を始めた");
  },
  "step.start": (e) => {
    stepStart[e.step] = e.t;
    if (e.node === "decide") {
      rounds++; const id = "d" + e.step; round = id;
      add(id, "req", 1, `<div class="row"><span class="num">判断 ${rounds}</span><span class="choice">LLM が考えている</span><span class="st"></span></div>`,
        { title: `判断 ${rounds}` }, "round llm");
      setState(id, "llm"); span("判断", e.t, 0); now(`判断 ${rounds} · 次に何をするかを LLM が考えている`);
      return;
    }
    if (e.node === "check_feasibility") {
      add("w" + e.step, "req", 1, `<div class="row"><span class="num">判定</span><span class="choice">調べられるか</span><span class="st"></span></div>`,
        { title: "公開情報で調べられるか" }, "round llm");
      span("判定", e.t, 0); now("依頼が公開情報で調べられるかを、LLM が判定している");
      return;
    }
    const id = "w" + e.step;
    if (N[id]) { setState(id, "running"); return; }   // 聞き返しの答えで再開した
    work = id;
    add(id, round || "req", 2, card(WORK[e.node] || e.node, WORK[e.node] || e.node, "始めた") + `<button type="button" class="toggle" hidden>畳む ▾</button>`,
      { title: WORK[e.node] || e.node }, e.node === "respond" || e.node === "check_conflicts" ? "llm" : "running");
    N[id].el.querySelector(".toggle").addEventListener("click", () => collapse(id, !N[id].collapsed));
    order.filter((x) => N[x].depth === 2 && x !== id && N[x].kids.length).forEach((x) => collapse(x, true));   // 前の巡回の作業は畳む
    N[id].span = span(WORK[e.node] || e.node, e.t, 1);
    if (e.node === "respond") now("確かめた事実と出典番号から、LLM が報告を書いている");
    if (e.node === "check_conflicts") now("観点ごとに、別々のサイトの事実が食い違っていないか確かめている");
  },
  "step.end": (e) => {
    const id = e.node === "decide" ? "d" + e.step : "w" + e.step;
    if (e.error) { setState(id, "failed"); setSub(id, e.error); }
    else if (!N[id]?.el.classList.contains("failed")) setState(id, "done");
    const sp = spans.filter((o) => o.b === null && Math.abs(o.a - (stepStart[e.step] ?? o.a)) < 0.01).pop();
    if (sp) sp.b = e.t;
    if (info[id]) info[id].kv = [...(info[id].kv || []), ["所要時間", `${e.seconds} 秒${e.llm_wait ? `（うち LLM サーバーの待ち ${e.llm_wait} 秒）` : ""}`]];
    const tg = N[id]?.el.querySelector(".toggle"); if (tg && N[id].kids.length) tg.hidden = false;
  },
  "feasibility": (e) => {
    const id = Object.keys(N).find((k) => N[k].el.querySelector(".num")?.textContent === "判定");
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
    if (e.summary_url && !N["sum"]) {
      add("sum", w, 3, card("概要", host(e.summary_url), "対象の概要（報告の冒頭に使う）", tier(2), icon(host(e.summary_url))),
        { title: "概要", kv: [["URL", link(e.summary_url)]] }, "done");
      N.sum.url = e.summary_url;
    }
  },
  "aspects": (e) => {
    buildBoard(e.aspects);
    if (N[work]) { setSub(work, `${e.subject_type} · 観点 ${e.aspects.length} つ（${e.origin}が作成）`); info[work].kv = [["対象の種類", esc(e.subject_type)], ["観点", esc(e.aspects.join("、"))], ["観点を作ったもの", esc(e.origin)]]; }
    now("対象の種類を判定し、網羅表の観点を作った");
  },
  "databases": (e) => {
    if (!N[work]) return;
    const id = work + "-db"; dbNode[work] = id;
    add(id, work, 3, card("分野別データベース · L3", e.used.join(" → ") || "この種類に使えるものがない", `記録 ${e.records.length} 件（${e.seconds} 秒）${e.failures.length ? " · 失敗あり" : ""}`,
      "", e.records[0] ? icon(host(e.records[0].url)) : ""),
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
    pagesOk++; $("pageTxt").textContent = pagesOk; $("pageBar").style.width = Math.min(100, (pagesOk / 12) * 100) + "%";
    Object.assign(info[id], { facts: e.facts, dropped: e.dropped });
    info[id].kv.push(["Tier の調整", e.tier_reason ? esc(`T${e.base_tier}→T${e.tier}（${e.tier_reason}）`) : ""],
      ["見分ける特徴", esc((e.features || []).join("、"))], ["別の呼び方", esc((e.new_terms || []).join("、"))],
      ["同名の別の対象", esc(e.other_entity)]);
    e.facts.forEach((f, i) => flyFact(id, f, e.tier, i * 160));
  },
  "page.reprint": (e) => {
    const id = Object.keys(N).find((k) => k.startsWith("p:") && sameUrl(k.slice(2), e.url));
    const orig = Object.keys(N).find((k) => k.startsWith("p:") && sameUrl(k.slice(2), e.original));
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
    } else th.innerHTML = ng ? "—" : "許可が<br>必要";
    info[id].kv.push(["判定", esc(e.level)], ["理由", esc(e.reason)], ["写っているもの", esc(e.description)],
      ["文脈 V-1 / 見た目 V-3", e.context ? esc(`${e.context} / ${e.visual}`) : ""], ["画像の中の文字", esc(e.visible_text)],
      ["画像で確かめた特徴", esc((e.features_seen || []).join("、"))], ["ライセンス", esc(license)]);
  },
  "image.final": (e) => {
    (e.withheld || []).forEach((w) => { const id = "i:" + w.src; if (!N[id]) return; N[id].el.classList.add("dropped"); setSub(id, "確認済み · 許可が必要なので載せない"); info[id].kv.push(["報告", esc(w.why)]); });
    if (N[work]) setSub(work, `確かめた ${order.filter((k) => k.startsWith("i:")).length} · 載せる ${e.shown.length}`);
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
    now("利用者の答えを待っている", false);
  },
  "answer": (e) => { if (!questions.has("a:" + e.answer + e.question)) { questions.add("a:" + e.answer + e.question); chatUser(e.answer); } setComposer(false); statusBubble(); },
  "run.waiting": (e) => { if (!questions.has(e.question)) { questions.add(e.question); showQuestion(e.question); } setComposer(true); now("利用者の答えを待っている", false); },
  "report": (e) => {
    sources = e.sources || [];
    if (N[work]) { setSub(work, `出典 ${sources.length} 件`); info[work].list = sources.map((s, i) => `[${i + 1}] ${esc(s.title)} — ${link(s.url)} · T${s.tier}`); }
  },
  "run.end": (e) => {
    setState("req", "done"); now(`完了 · ${Math.round(e.t)} 秒`, false);
    const body = renderReport(e.answer || "");
    if (statusEl) { statusEl.querySelector(".body").innerHTML = body; } else chatAgent(body);
    wireCites(statusEl || $("chat").lastElementChild); statusEl = null; setComposer(false);
    loadRuns();
  },
  "run.error": (e) => { now("エラーで止まった", false); chatAgent(`<p>エラーで調査を続けられませんでした: ${esc(e.error)}</p>`); statusEl = null; },
};
function sameUrl(a, b) { const n = (u) => u.replace(/[?#].*$/, "").replace(/\/$/, ""); return n(a) === n(b); }
function pageNode(e, state) {
  const id = "p:" + e.url;
  if (N[id]) { if (state !== "planned") setState(id, state); return; }
  const parent = e.db && dbNode[work] ? dbNode[work] : work;
  if (!N[parent]) return;
  add(id, parent, N[parent].depth + 1, card(e.db ? "記録 · " + e.db : "ページ", e.host, state === "planned" ? "読む順番を待っている" : "取得している", tier(e.tier), icon(e.host)) +
    `<div class="steps"><span></span><span></span><span></span></div>`,
    { title: e.host, kv: [["URL", link(e.url)], ["Tier", `${e.tier}`]],
      note: "本文に一字一句ある引用の事実だけを残す（引用の照合）。" }, state);
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
    const id = Object.keys(N).find((k) => k.startsWith("p:") && sameUrl(k.slice(2), s.url)) || (N.sum && sameUrl(N.sum.url, s.url) ? "sum" : null);
    if (!id) return;
    reveal(id); follow = false; $("backTo").hidden = false; jump = id; select(id);
  }));
}

// ---- 調査の開始・答え・一覧 ----
let source = null, waitingRun = null;
function setComposer(answerMode) {
  waitingRun = answerMode ? runId : null;
  $("composer").classList.toggle("answer", answerMode);
  $("input").placeholder = answerMode ? "質問への答えを入力（番号でも可）" : "調べたいことを入力";
}
async function sendAnswer(text) {
  if (!waitingRun || !text) return;
  try { await api(`/api/runs/${waitingRun}/answer`, { answer: text }); setComposer(false); now("答えを受け取った。調査を続けている"); }
  catch (err) { chatAgent(`<p>答えを送れませんでした: ${esc(err.message)}</p>`); }
}
$("composer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = $("input").value.trim(); if (!text) return;
  $("input").value = "";
  if (waitingRun) { sendAnswer(text); return; }
  try { const r = await api("/api/runs", { request: text }); open(r.id, true); loadRuns(); }
  catch (err) { chatAgent(`<p>調査を始められませんでした: ${esc(err.message)}</p>`); }
});
$("newBtn").addEventListener("click", () => { closeSource(); reset(); runId = null; $("chat").innerHTML = ""; $("chat").appendChild(emptyChat); emptyChat.hidden = false;
  $("replayCtl").hidden = true; setComposer(false); highlightRun(); $("input").focus(); });

function closeSource() { if (source) { source.close(); source = null; } queue = []; }

// 再生: 終わった調査は、記録の時刻どおりに（速さを掛けて）流す。動いている調査は届いたらすぐ描く
let queue = [], clock = 0, speed = 8, playing = true, replay = false, lastTs = null, stopAt = Infinity;
function open(id, live) {
  closeSource(); reset(); runId = id; $("chat").innerHTML = "";
  replay = !live; clock = 0; playing = true; $("playBtn").textContent = "一時停止";
  $("replayCtl").hidden = live; setComposer(false); highlightRun();
  source = new EventSource(withKey(`/api/runs/${encodeURIComponent(id)}/events`));
  source.onmessage = (m) => { const e = JSON.parse(m.data); if (replay) queue.push(e); else handle(e); };
  source.addEventListener("end", () => closeSource0());
  source.addEventListener("missing", () => { closeSource0(); chatAgent("<p>この調査の記録が見つかりません。</p>"); });
  source.onerror = () => { /* EventSource が自動で受け直す（Last-Event-ID） */ };
}
function closeSource0() { if (source) { source.close(); source = null; } }
$("playBtn").addEventListener("click", () => { playing = !playing; $("playBtn").textContent = playing ? "一時停止" : "再生"; });
document.querySelectorAll("[data-speed]").forEach((b) => b.addEventListener("click", () => {
  speed = +b.dataset.speed; document.querySelectorAll("[data-speed]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
}));
function frame(ts) {
  if (lastTs === null) lastTs = ts;
  const dt = Math.min(0.1, (ts - lastTs) / 1000); lastTs = ts;
  if (replay && playing && queue.length) {
    if (speed === 0) { instant = true; while (queue.length && queue[0].t <= stopAt) handle(queue.shift()); layout(); instant = false; }
    else {
      clock += dt * speed;
      // 利用者の答えを待った時間などの長い空白は飛ばす
      if (queue[0].t - clock > 20) clock = queue[0].t;
      while (queue.length && queue[0].t <= clock) handle(queue.shift());
    }
  }
  animate(dt);
  requestAnimationFrame(frame);
}

async function loadRuns() {
  try {
    const { runs } = await api(withKey("/api/runs"));
    const label = { running: "調べている", waiting: "答え待ち", done: "完了", error: "エラー", stopped: "途中で止まった" };
    $("runs").innerHTML = runs.map((r) => `<button type="button" class="run" data-id="${esc(r.id)}" data-live="${r.status === "running" || r.status === "waiting"}">
      <span class="r1">${esc(r.request || "(依頼なし)")}</span><span class="r2">${esc(r.id.slice(0, 13).replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2}).*/, "$2/$3 $4:$5"))} · ${label[r.status] || r.status}</span></button>`).join("")
      || `<p class="r2" style="padding:8px;color:var(--muted)">まだ調査はありません</p>`;
    $("runs").querySelectorAll(".run").forEach((b) => b.addEventListener("click", () => open(b.dataset.id, b.dataset.live === "true")));
    highlightRun();
  } catch (err) { $("runs").innerHTML = `<p style="padding:8px;color:var(--muted)">一覧を読めません: ${esc(err.message)}</p>`; }
}
function highlightRun() { $("runs").querySelectorAll(".run").forEach((b) => b.classList.toggle("on", b.dataset.id === runId)); }

window.addEventListener("resize", () => N && layout());
reset(); loadRuns(); requestAnimationFrame(frame);
const params = new URLSearchParams(location.search);
if (params.has("speed")) {   // 再生の速さ（0 = すぐ全部を出す）。共有するリンクで使う
  speed = +params.get("speed") || 0;
  document.querySelectorAll("[data-speed]").forEach((x) => x.setAttribute("aria-pressed", String(+x.dataset.speed === speed)));
}
if (params.has("at")) stopAt = +params.get("at");   // この時刻（秒）の状態で止める
const first = params.get("run");
if (first) api(withKey("/api/runs")).then(({ runs }) => {
  const r = runs.find((x) => x.id === first);
  open(first, !!r && (r.status === "running" || r.status === "waiting"));
}).catch(() => open(first, false));
})();
