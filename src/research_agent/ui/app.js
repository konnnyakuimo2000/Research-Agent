// テレメトリー画面（docs/telemetry_design.md、部品の分け方は docs/telemetry_enhancement_plan.md 8.1）。
// サーバーから届くイベント（/api/runs/{id}/events）を受け取り、チャット・流れの図・木に配る。
// 動いている調査はそのまま、終わった調査は記録を再生する（速さを選べる）。
//
// 部品:
//   core/       … API・共有の状態・イベントの配り方と差し込み口・カメラ・JSON Schema の欄
//   views/      … 流れの図（名乗りの graph を一般的な並べ方で）・木（共通。1 段）
//   panels/     … チャット（0 段）・詳細・モデルの選択・設定
//   extensions/ … 2 段の拡張の部品。依頼のエージェントが名乗った拡張だけを読み込む（research-v1 など）
import { $, esc } from "./core/util.js";
import { api, store, withKey } from "./core/api.js";
import { ctx, hasFlow, caps } from "./core/ctx.js";
import { dispatch, layoutFor, loadExtensions, norm, rememberExtensions, resetExtensions, runExtensions } from "./core/events.js";
import { fitMode, fitTarget, resetCamera, stepCamera, useView } from "./core/camera.js";
import { tree } from "./views/tree.js";
import { GraphFlow } from "./views/flow.js";
import { chatAgent, hooks as chatHooks, resetChat, setEmpty, now } from "./panels/chat.js";
import { closeDetails } from "./panels/details.js";
import { chosenModels, fmtUsd, hooks as modelHooks, loadChoice, loadModels, modelMeta, renderModels, runModelTag } from "./panels/models.js";
import { chosenSettings, loadSettings, settingsMeta } from "./panels/settings.js";

// ---- エージェント（ゲートウェイがつないでいるもの） ----
function setLayout(id) { document.querySelector(".app").classList.toggle("chatonly", !hasFlow(id)); }
async function loadAgents() {
  try {
    const { agents, default: def } = await api("/api/agents");
    ctx.AGENTS = Object.fromEntries(agents.map((a) => [a.id, a]));
    rememberExtensions(agents);
    await loadExtensions([...new Set(agents.flatMap((a) => a.extensions || []))]);
    const saved = store.get("agent", "");
    ctx.agentId = ctx.AGENTS[saved] ? saved : def;
    const sel = $("agentSel");
    sel.innerHTML = agents.map((a) => `<option value="${esc(a.id)}"${a.id === ctx.agentId ? " selected" : ""}${a.ok ? "" : " disabled"}>${esc(a.name)}${a.ok ? "" : "（つながらない）"}</option>`).join("");
    sel.hidden = agents.length < 2; sel.title = ctx.AGENTS[ctx.agentId]?.description || "";
    if (!ctx.runId) { ctx.runAgent = ctx.agentId; reset(); }
    setLayout(ctx.runId ? ctx.runAgent : ctx.agentId);   // 名乗りが届く前に開いた依頼も、段に合わせて出し直す
    checkConnection(); loadChoice(); loadSettings(); loadModels();
  } catch (err) { /* 一覧が取れなければ、既定のエージェントに送る */ }
}
$("agentSel").addEventListener("change", (ev) => {
  ctx.agentId = ev.target.value; ev.target.title = ctx.AGENTS[ctx.agentId]?.description || "";
  store.set("agent", ctx.agentId);
  newResearch(); checkConnection(); loadChoice(); loadSettings(); renderModels();
});

// ---- 図（流れの図 / 木）とカメラ ----
// 流れの図は、依頼のエージェントの拡張に並べ方があればそれを、なければ名乗りの graph の一般的な並べ方を使う
const activeView = () => (ctx.view === "flow" ? ctx.flow : tree);
function reset() {
  tree.reset(); resetExtensions();
  const Flow = layoutFor("flow") || GraphFlow;
  ctx.flow = new Flow(ctx.runAgent);
  closeDetails(); $("flowEmpty").hidden = false;
  resetCamera(); useView(activeView());
}
function setView(v) {
  ctx.view = v;
  $("graph").hidden = v !== "flow"; tree.el.hidden = v !== "tree";
  activeView().snap();
  $("flowEmpty").hidden = !activeView().isEmpty();
  document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === v)));
  useView(activeView()); fitMode(true); fitTarget(true);   // 切り替えたら、カメラもすぐ全体に合わせる
}
document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
function handle(e) { dispatch(e); ctx.flow.on(e); }

// ---- 調査の開始・答え・一覧 ----
let source = null, waitingRun = null, liveState = "";   // liveState: 動いている調査の状態（running / paused / ""）
function setComposer(answerMode) {
  waitingRun = answerMode ? ctx.runId : null;
  $("composer").classList.toggle("answer", answerMode);
  $("input").placeholder = answerMode ? "質問への答えを入力（番号でも可）" : "調べたいことを入力";
  updateSend();
}
// 送信ボタンは、調査が動いている間（再生中も）は一時停止 / 再開のボタンになる
function busy() { return !waitingRun && (ctx.replay ? queue.length > 0 || !!source : liveState !== ""); }
function paused() { return ctx.replay ? !playing : liveState === "paused"; }
function updateSend() {
  const b = $("sendBtn"), stop = busy(), p = paused();
  const canPause = ctx.replay || caps(ctx.runAgent).includes("pause");
  b.setAttribute("aria-label", stop ? (!canPause ? "止める" : p ? "再開" : "一時停止") : "送る"); b.title = b.getAttribute("aria-label");
  b.classList.toggle("pause", stop && !p); b.classList.toggle("resume", stop && p);
}
async function togglePause() {
  if (ctx.replay) { playing = !playing; updateSend(); now(playing ? "再生している" : "一時停止している"); return; }
  const action = !caps(ctx.runAgent).includes("pause") ? "cancel" : liveState === "paused" ? "resume" : "pause";
  try { await api(`/api/runs/${ctx.runId}/${action}`, {}); }
  catch (err) { chatAgent(`<p>${action === "resume" ? "再開できませんでした" : "止められませんでした"}: ${esc(err.message)}</p>`); }
}
async function sendAnswer(text) {
  if (!waitingRun || !text) return;
  try { await api(`/api/runs/${waitingRun}/answer`, { answer: text }); setComposer(false); }
  catch (err) { chatAgent(`<p>答えを送れませんでした: ${esc(err.message)}</p>`); }
}
Object.assign(chatHooks, { setComposer, loadRuns, updateSend, sendAnswer, setLive: (v) => { liveState = v; } });
$("composer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (busy()) { togglePause(); return; }
  const text = input.value.trim(); if (!text) return;
  input.value = ""; growInput();
  if (waitingRun) { sendAnswer(text); return; }
  const settings = chosenSettings();
  try {
    const r = await api("/api/runs", { request: text, agent: ctx.agentId, models: chosenModels(), settings });
    open(r.id, true, r.agent, r.models, r.settings || settings); loadRuns();
  } catch (err) { chatAgent(`<p>始められませんでした: ${esc(err.message)}</p>`); }
});
function newResearch() {
  openSeq++; closeSource(); ctx.runId = null; ctx.runAgent = ctx.agentId; reset(); setLayout(ctx.agentId); resetChat(); setEmpty(true);
  ctx.replay = false; liveState = ""; setComposer(false); highlightRun(); $("input").focus();
}
$("newBtn").addEventListener("click", () => { closeSide(); newResearch(); });
$("plusBtn").addEventListener("click", newResearch);
// 履歴: 広い画面では列を出す・隠す。狭い画面（列にしない幅）では、チャットの上に重ねて開く
const narrow = () => window.matchMedia("(max-width: 1180px)").matches;
const closeSide = () => document.querySelector(".app").classList.remove("sideopen");
$("runsBtn").addEventListener("click", () => {
  const app = document.querySelector(".app");
  if (narrow()) app.classList.toggle("sideopen");
  else { app.classList.toggle("noside"); setTimeout(() => fitMode(true), 50); }
});
$("scrim").addEventListener("click", closeSide);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeSide(); });

// 入力欄: Enter で送る（Shift+Enter は改行。日本語の変換を確定する Enter では送らない）。行が増えたら高さを伸ばす
const input = $("input");
function growInput() { input.style.height = "auto"; input.style.height = Math.min(180, input.scrollHeight) + "px"; }
input.addEventListener("input", growInput);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); $("composer").requestSubmit(); }
});

// エージェントと、エージェントが使うモデル・検索につながるか（入力欄の下に出す）
async function checkConnection() {
  try {
    const st = await api(`/api/status?agent=${encodeURIComponent(ctx.agentId)}`);
    const name = ctx.AGENTS[st.agent]?.name || st.agent;
    $("modelName").textContent = st.model || ""; $("modelName").title = `${name}が使っているモデル: ${st.model}`;
    const checks = Object.values(st.checks || {}), bad = checks.find((c) => !c.ok), ok = st.ok && !bad;
    $("conn").className = "conn " + (ok ? "ok" : "ng");
    $("connText").textContent = ok ? "" : !st.ok ? "エージェントにつながらない" : bad.hint || `${bad.title}につながらない`;
    $("conn").title = st.ok ? `${name}: ` + checks.map((c) => `${c.title} ${c.ok ? `応答 ${c.seconds} 秒` : c.error || "応答なし"}`).join(" / ")
      : `${name}: ${st.error || "応答なし"}`;
  } catch (err) { $("conn").className = "conn ng"; $("connText").textContent = "エージェントにつながらない"; }
}
setInterval(checkConnection, 30000);
setInterval(loadModels, 60000);
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

// 依頼で使ったモデル・設定・費用を、チャットの頭に小さく出す
function showRunMeta(id, models, settings) {
  const m = modelMeta(models), s = settingsMeta(ctx.runAgent, settings);
  if (!m && !s) return;
  $("runMeta")?.remove();                     // モデルの一覧が後から届いたら、作り直す
  const d = document.createElement("div"); d.className = "runmeta"; d.id = "runMeta";
  d.innerHTML = `${m ? `<span>${esc(m.text)}</span>` : ""}${s ? `<span>${esc(s)}</span>` : ""}<span class="cost mono" id="runCost"></span>`;
  $("chat").prepend(d);
  if (m?.priced) { d.dataset.priced = "1"; showRunCost(id); }
}
async function showRunCost(id) {
  try {
    const u = await api(`/api/runs/${encodeURIComponent(id)}/usage`);
    const el = $("runCost"); if (el && id === ctx.runId) el.textContent = `費用 ${fmtUsd(u.usd || 0)}${u.limit_usd ? ` / 上限 ${fmtUsd(u.limit_usd)}` : ""}`;
  } catch (err) { /* 費用を読めなくても、調査の表示は続ける */ }
}
modelHooks.afterLoad = async () => {
  await loadRuns();
  if (ctx.runId) showRunMeta(ctx.runId, RUNS[ctx.runId]?.models, RUNS[ctx.runId]?.settings);   // 一覧が届く前に開いた調査にも出す
};

// 再生: 終わった調査は、記録の時刻どおりに（速さを掛けて）流す。動いている調査は届いたらすぐ描く
let RUNS = {};   // 一覧の依頼（id → 要約。使ったモデル・設定を出すため）
let queue = [], clock = 0, speed = 8, playing = true, lastTs = null, stopAt = Infinity, openSeq = 0;
function closeSource() { if (source) { source.close(); source = null; } queue = []; }
async function open(id, live, agent, models, settings) {
  const seq = ++openSeq;
  closeSource();
  const agentId = agent || RUNS[id]?.agent || "research";
  await loadExtensions(runExtensions(agentId));   // その依頼のエージェントが名乗った拡張の部品
  if (seq !== openSeq) return;                    // 待っている間に別の依頼を開いた
  ctx.runId = id; ctx.runAgent = agentId; reset(); setLayout(agentId); resetChat();
  showRunMeta(id, models || RUNS[id]?.models, settings || RUNS[id]?.settings);
  ctx.replay = !live; clock = 0; playing = true; liveState = live ? "running" : "";
  setComposer(false); highlightRun();
  source = new EventSource(withKey(`/api/runs/${encodeURIComponent(id)}/events`));
  source.onmessage = (m) => { const e = norm(JSON.parse(m.data)); if (ctx.replay) queue.push(e); else handle(e); };
  source.addEventListener("end", () => { if (source) { source.close(); source = null; } updateSend(); if ($("runMeta")?.dataset.priced) showRunCost(id); });
  source.addEventListener("missing", () => { if (source) { source.close(); source = null; } chatAgent("<p>この調査の記録が見つかりません。</p>"); });
}
function frame(ts) {
  if (lastTs === null) lastTs = ts;
  const dt = Math.min(0.1, (ts - lastTs) / 1000); lastTs = ts;
  if (ctx.replay && playing && queue.length) {
    if (speed === 0) { ctx.instant = true; while (queue.length && queue[0].t <= stopAt) handle(queue.shift()); tree.layout(); ctx.flow.layout(); ctx.instant = false; }
    else {
      clock += dt * speed;
      if (queue[0].t - clock > 20) clock = queue[0].t;   // 利用者の答えを待った時間などの長い空白は飛ばす
      while (queue.length && queue[0].t <= clock) handle(queue.shift());
    }
    if (!queue.length && !source) updateSend();   // 再生し終えたら、送信ボタンに戻す
  }
  activeView().draw(dt);
  stepCamera(dt);
  requestAnimationFrame(frame);
}

async function loadRuns() {
  try {
    const { runs } = await api(withKey("/api/runs"));
    RUNS = Object.fromEntries(runs.map((r) => [r.id, r]));
    const label = { running: "調べている", paused: "一時停止中", waiting: "答え待ち", finishing: "報告を保存中", done: "完了",
      error: "エラー", stopped: "途中で止まった", cancelled: "止めた" };
    const defAgent = Object.values(ctx.AGENTS).find((a) => a.default)?.id;
    $("runs").innerHTML = runs.map((r) => `<button type="button" class="run" data-id="${esc(r.id)}" data-agent="${esc(r.agent || "")}" data-live="${["running", "waiting", "paused"].includes(r.status)}">
      <span class="r1">${esc(r.request || "(依頼なし)")}</span><span class="r2">${esc(r.id.slice(0, 13).replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2}).*/, "$2/$3 $4:$5"))} · ${label[r.status] || r.status}${ctx.AGENTS[r.agent] && r.agent !== defAgent ? " · " + esc(ctx.AGENTS[r.agent].name) : ""}${runModelTag(r.models) ? " · " + esc(runModelTag(r.models)) : ""}</span></button>`).join("")
      || `<p class="r2" style="padding:8px;color:var(--muted)">まだ調査はありません</p>`;
    $("runs").querySelectorAll(".run").forEach((b) => b.addEventListener("click", () => { closeSide(); open(b.dataset.id, b.dataset.live === "true", b.dataset.agent); }));
    showRecent(runs);
    highlightRun();
  } catch (err) { $("runs").innerHTML = `<p style="padding:8px;color:var(--muted)">一覧を読めません: ${esc(err.message)}</p>`; }
}
function highlightRun() { $("runs").querySelectorAll(".run").forEach((b) => b.classList.toggle("on", b.dataset.id === ctx.runId)); }

window.addEventListener("resize", () => { tree.layout(); ctx.flow?.layout(); });
reset(); requestAnimationFrame(frame);
const params = new URLSearchParams(location.search);
if (params.has("speed")) speed = +params.get("speed") || 0;   // 再生の速さ（0 = すぐ全部を出す。確認用）
if (params.has("at")) stopAt = +params.get("at");   // この時刻（秒）の状態で止める
const ready = Promise.all([loadAgents(), loadRuns()]);
const first = params.get("run");
if (first) ready.then(() => {
  const r = RUNS[first];
  open(first, !!r && ["running", "waiting", "paused"].includes(r.status), r?.agent, r?.models, r?.settings);
});
