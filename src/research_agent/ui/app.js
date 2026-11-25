// テレメトリー画面（docs/telemetry_design.md、部品の分け方は docs/telemetry_enhancement_plan.md 8.1）。
// サーバーから届くイベント（/api/runs/{id}/events）を受け取り、チャット・流れの図・木に配る。
// 動いている調査はそのまま、終わった調査は記録を再生する（速さを選べる）。
//
// 部品:
//   core/       … API・共有の状態・イベントの配り方と差し込み口・カメラ・JSON Schema の欄
//   views/      … 流れの図（名乗りの graph を一般的な並べ方で）・木（共通。1 段）
//   panels/     … チャット（0 段）・詳細・設定のパネル（調査・モデル・接続と使用量・表示・履歴の詳細）・ペインの幅
//   extensions/ … 2 段の拡張の部品。依頼のエージェントが名乗った拡張だけを読み込む（research-v1 など）
//
// 画面の形（.claude/ui-gateway-settings-plan.md）: チャットの下は入力欄と送信ボタンだけ。次の依頼に効く設定の要約は
// チャットの上に 1 行で出し、設定は左の一覧の下の「設定」から開く。一時停止・再開は作業記録の上に置く
import { $, esc } from "./core/util.js";
import { api, withKey } from "./core/api.js";
import { ctx, hasFlow, caps, wake } from "./core/ctx.js";
import { dispatch, layoutFor, loadExtensions, norm, rememberExtensions, resetExtensions, runExtensions } from "./core/events.js";
import { fitMode, fitTarget, resetCamera, stepCamera, useView } from "./core/camera.js";
import { tree } from "./views/tree.js";
import { GraphFlow } from "./views/flow.js";
import { chatAgent, hooks as chatHooks, resetChat, setEmpty, now } from "./panels/chat.js";
import { closeDetails } from "./panels/details.js";
import { answeredMeta, fmtUsd, hooks as modelHooks, loadModels, modelMeta, runModelTag } from "./panels/models.js";
import { closePanel, connectionProblem, consumeOneTime, hooks as setHooks, initSettings, loadAgentStatus, openPanel,
  panelOpen, requestBody, settingsMeta, statusLabel, summary } from "./panels/settings.js";
import { initLayout, resetWidths, showPane, widthsText } from "./panels/layout.js";

// ---- エージェント（ゲートウェイがつないでいるもの） ----
function setLayout(id) {
  const app = document.querySelector(".app"), only = !hasFlow(id);
  app.classList.toggle("chatonly", only);
  if (only) showPane("chat");
  // 一時停止・再開のボタンは作業記録の上に置く。作業記録のないエージェントでは、チャットの上に置く
  (only ? $("headCtl") : document.querySelector(".hud-top")).appendChild($("runCtl"));
}
let defaultAgent = "research";
async function loadAgents() {
  try {
    const { agents, default: def } = await api("/api/agents");
    ctx.AGENTS = Object.fromEntries(agents.map((a) => [a.id, a]));
    defaultAgent = def;
    rememberExtensions(agents);
    await loadExtensions([...new Set(agents.flatMap((a) => a.extensions || []))]);
    initSettings(def);                                   // 覚えているエージェントと設定（ctx.agentId を決める）
    if (!ctx.runId) { ctx.runAgent = ctx.agentId; reset(); }
    setLayout(ctx.runId ? ctx.runAgent : ctx.agentId);   // 名乗りが届く前に開いた依頼も、段に合わせて出し直す
    loadModels();
  } catch (err) { /* 一覧が取れなければ、既定のエージェントに送る */ }
}
// 次の依頼のエージェントが変わったとき。開いている履歴と、ライブの購読はそのまま
setHooks.agentChanged = () => {
  if (!ctx.runId) { ctx.runAgent = ctx.agentId; reset(); setLayout(ctx.agentId); }
  loadAgentStatus();
};
setHooks.summaryChanged = renderHead;
setHooks.startNew = () => newResearch();
setHooks.resetWidths = resetWidths;
setHooks.widthsText = widthsText;

// チャットの上: 次の依頼に効くエージェント・プリセット・モデルと、送信に響く異常（外部モデル・接続・使えなくなったモデル）
function renderHead() {
  const s = summary(), btn = $("headSummary");
  if (!s) { btn.textContent = ""; $("headWarn").hidden = true; return; }
  const parts = [s.agent, s.preset, s.models].filter(Boolean);
  btn.innerHTML = `<span class="lbl">次の依頼</span><span class="txt">${esc(parts.join(" · "))}</span>`
    + (s.oneTime ? `<span class="badge">${esc(s.oneTime)}</span>` : "") + (s.external ? `<span class="ext">外部</span>` : "");
  btn.title = `次の依頼: ${parts.join(" · ")}${s.oneTime ? `（${s.oneTime}）` : ""}\n押すと設定を開く`;   // 幅が狭くて切れても読めるように
  btn.setAttribute("aria-label", `次の依頼:${parts.join("、")}${s.oneTime ? `（${s.oneTime}）` : ""}${s.external ? "、外部のモデルを使う" : ""}。押すと設定を開く`);
  const warn = [connectionProblem(), ...s.warnings, s.external ? "外部のモデルに、依頼文と読んだページの本文が送られます" : ""].filter(Boolean);
  $("headWarn").hidden = !warn.length;
  $("headWarn").innerHTML = warn.map(esc).join("<br>");
}
$("headSummary").addEventListener("click", (ev) => openPanel("research", ev.currentTarget));
$("settingsBtn").addEventListener("click", (ev) => { closeSide(); openPanel("research", narrow() ? $("runsBtn") : ev.currentTarget); });

// ---- 図（流れの図 / 木）とカメラ ----
// 流れの図は、依頼のエージェントの拡張に並べ方があればそれを、なければ名乗りの graph の一般的な並べ方を使う
const activeView = () => (ctx.view === "flow" ? ctx.flow : tree);
function reset() {
  tree.reset(); resetExtensions();
  const Flow = layoutFor("flow") || GraphFlow;
  ctx.flow = new Flow(ctx.runAgent);
  closeDetails(); $("flowEmpty").hidden = false;
  resetCamera(); useView(activeView()); wake(1);
}
function setView(v) {
  ctx.view = v;
  $("graph").hidden = v !== "flow"; tree.el.hidden = v !== "tree";
  activeView().snap();
  $("flowEmpty").hidden = !activeView().isEmpty();
  document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === v)));
  useView(activeView()); fitMode(true); fitTarget(true);   // 切り替えたら、カメラもすぐ全体に合わせる
  wake();
}
document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
function handle(e) { dispatch(e); ctx.flow.on(e); wake(); }

// ---- 調査の開始・答え・一覧 ----
let source = null, waitingRun = null, liveState = "";   // liveState: 動いている調査の状態（running / paused / ""）
function setComposer(answerMode) {
  waitingRun = answerMode ? ctx.runId : null;
  $("composer").classList.toggle("answer", answerMode);
  $("input").placeholder = answerMode ? "質問への答えを入力（番号でも可）" : "調べたいことを入力";
  updateSend();
}
// 調査が動いている間（再生中も）は、作業記録の上のボタンで一時停止・再開（止めるだけのエージェントは「止める」）
function busy() { return !waitingRun && (ctx.replay ? queue.length > 0 || !!source : liveState !== ""); }
function paused() { return ctx.replay ? !playing : liveState === "paused"; }
function updateSend() {
  const stop = busy(), p = paused(), canPause = ctx.replay || caps(ctx.runAgent).includes("pause");
  const ctl = $("runCtl");
  ctl.hidden = !stop;
  ctl.textContent = !canPause ? "止める" : p ? (ctx.replay ? "再生を続ける" : "再開") : (ctx.replay ? "再生を止める" : "一時停止");
  ctl.classList.toggle("on", stop && p);
  // ライブの調査が動いている間は、新しい依頼を送らない（答え待ちのときは答えを送る）
  const b = $("sendBtn"), liveBusy = stop && !ctx.replay;
  b.disabled = liveBusy;
  b.title = liveBusy ? "調査中です（一時停止・止めるは作業記録の上のボタン）" : waitingRun ? "答えを送る" : "送る";
  b.setAttribute("aria-label", b.title);
}
async function togglePause() {
  if (ctx.replay) { playing = !playing; updateSend(); now(playing ? "再生している" : "一時停止している"); return; }
  const action = !caps(ctx.runAgent).includes("pause") ? "cancel" : liveState === "paused" ? "resume" : "pause";
  try { await api(`/api/runs/${ctx.runId}/${action}`, {}); }
  catch (err) { chatAgent(`<p>${action === "resume" ? "再開できませんでした" : "止められませんでした"}: ${esc(err.message)}</p>`); }
}
$("runCtl").addEventListener("click", togglePause);
async function sendAnswer(text) {
  if (!waitingRun || !text) return;
  try { await api(`/api/runs/${waitingRun}/answer`, { answer: text }); setComposer(false); }
  catch (err) { chatAgent(`<p>答えを送れませんでした: ${esc(err.message)}</p>`); }
}
Object.assign(chatHooks, { setComposer, loadRuns, updateSend, sendAnswer, setLive: (v) => { liveState = v; } });
$("composer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (busy() && !ctx.replay) return;
  const text = input.value.trim(); if (!text) return;
  if (waitingRun) { input.value = ""; growInput(); sendAnswer(text); return; }
  const body = requestBody();
  try {
    const r = await api("/api/runs", { request: text, ...body });
    input.value = ""; growInput();
    consumeOneTime();                                  // 「この依頼だけ」は次の依頼に持ち越さない
    open(r.id, true, r.agent, r.models, r.settings || body.settings); loadRuns();
  } catch (err) { chatAgent(`<p>始められませんでした: ${esc(err.message)}</p>`); }   // 送れなければ、入力した文は残す
});
function newResearch() {
  openSeq++; closeSource(); ctx.runId = null; ctx.runAgent = ctx.agentId; reset(); setLayout(ctx.agentId); resetChat(); setEmpty(true);
  ctx.replay = false; liveState = ""; showMode(); setComposer(false); highlightRun(); $("input").focus();
}
$("newBtn").addEventListener("click", () => { closeSide(); newResearch(); });
// 履歴: 広い画面では列を出す・隠す。狭い画面（列にしない幅）では、チャットの上に重ねて開く
const narrow = () => window.matchMedia("(max-width: 1180px)").matches;
const closeSide = () => document.querySelector(".app").classList.remove("sideopen");
$("runsBtn").addEventListener("click", () => {
  const app = document.querySelector(".app");
  if (narrow()) app.classList.toggle("sideopen");
  else { app.classList.toggle("noside"); setTimeout(() => { fitMode(true); relayout(); }, 50); }
});
$("scrim").addEventListener("click", closeSide);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !panelOpen()) { closeSide(); closeRunMenu(); } });

// 入力欄: Enter で送る（Shift+Enter は改行。日本語の変換を確定する Enter では送らない）。行が増えたら高さを伸ばす
const input = $("input");
function growInput() { input.style.height = "auto"; input.style.height = Math.min(180, input.scrollHeight) + "px"; }
input.addEventListener("input", growInput);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); $("composer").requestSubmit(); }
});

setInterval(() => loadAgentStatus(), 30000);
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

// 依頼で使ったモデル・設定・費用を、チャットの頭に小さく出す（押すと履歴の詳細）
function showRunMeta(id, models, settings) {
  const m = modelMeta(models), s = settingsMeta(ctx.runAgent, settings);
  $("runMeta")?.remove();                     // モデルの一覧が後から届いたら、作り直す
  const d = document.createElement("button"); d.type = "button"; d.className = "runmeta"; d.id = "runMeta";
  d.title = "この調査の開始時の設定とモデル（履歴の詳細）を開く";
  d.innerHTML = `${m ? `<span id="runModels">${esc(m.text)}</span>` : ""}${s ? `<span>${esc(s)}</span>` : ""}<span class="cost mono" id="runCost"></span><span class="more">詳細</span>`;
  d.addEventListener("click", () => { const r = RUNS[id]; if (r) openPanel("run", d, r); });
  $("chat").prepend(d);
  if (m?.priced) d.dataset.priced = "1";
  if (m) showRunCost(id);
}
// 窓口が数えた、この調査の費用と応答したモデル。応答したモデルが分かれば、割り当ての表示をそれに置き換える
// （割り当ては title に残す）。分からない（窓口を通していない古い記録など）ときは、割り当てのまま
async function showRunCost(id) {
  try {
    const u = await api(`/api/runs/${encodeURIComponent(id)}/usage`);
    if (id !== ctx.runId) return;
    const a = answeredMeta(u.answered), span = $("runModels");
    if (a && span) { if (!span.dataset.assigned) span.dataset.assigned = span.textContent;
      span.textContent = a.text; span.title = `${a.title}\n${span.dataset.assigned}`; }
    const el = $("runCost");
    if (el && $("runMeta")?.dataset.priced) el.textContent = `費用 ${fmtUsd(u.usd || 0)}${u.limit_usd ? ` / 上限 ${fmtUsd(u.limit_usd)}` : ""}`;
  } catch (err) { /* 費用を読めなくても、調査の表示は続ける */ }
}
modelHooks.afterLoad = async () => {
  renderHead();
  await loadRuns();
  if (ctx.runId && RUNS[ctx.runId]) showRunMeta(ctx.runId, RUNS[ctx.runId].models, RUNS[ctx.runId].settings);   // 一覧が届く前に開いた調査にも出す
};

// 再生: 終わった調査は、記録の時刻どおりに（速さを掛けて）流す。動いている調査は届いたらすぐ描く
let RUNS = {};   // 一覧の依頼（id → 要約。使ったモデル・設定を出すため）
let queue = [], clock = 0, speed = 8, playing = true, lastTs = null, stopAt = Infinity, openSeq = 0, skipped = 0, lastSeen = 0;
// ライブか履歴の再生か。再生は実際の速さと、飛ばした無イベントの時間も出す
function showMode(text) { $("modeBadge").textContent = text ?? (ctx.replay
  ? (speed === 0 ? "履歴（すぐに表示）" : `履歴を${speed}倍で再生`) + (skipped ? ` · 無イベントの ${Math.round(skipped)} 秒を省略` : "")
  : ctx.runId ? "ライブ" : ""); }
function closeSource() { if (source) { source.close(); source = null; } queue = []; }
async function open(id, live, agent, models, settings) {
  const seq = ++openSeq;
  closeSource();
  const agentId = agent || RUNS[id]?.agent || defaultAgent;
  await loadExtensions(runExtensions(agentId));   // その依頼のエージェントが名乗った拡張の部品
  if (seq !== openSeq) return;                    // 待っている間に別の依頼を開いた
  ctx.runId = id; ctx.runAgent = agentId; reset(); setLayout(agentId); resetChat();
  showRunMeta(id, models || RUNS[id]?.models, settings || RUNS[id]?.settings);
  ctx.replay = !live; clock = 0; playing = true; liveState = live ? "running" : ""; skipped = 0; lastSeen = Date.now();
  showMode();
  setComposer(false); highlightRun();
  source = new EventSource(withKey(`/api/runs/${encodeURIComponent(id)}/events`));
  source.onmessage = (m) => { lastSeen = Date.now(); const e = norm(JSON.parse(m.data)); if (ctx.replay) queue.push(e); else handle(e); };
  // 通信が切れて実行の状況を確かめられないときは、最後に受け取った時刻を出す（EventSource はつなぎ直す）
  source.onerror = () => { if (!ctx.replay && source) showMode(`接続が切れた（最後に受け取った ${new Date(lastSeen).toLocaleTimeString("ja-JP")}）`); };
  source.onopen = () => showMode();
  source.addEventListener("end", () => { if (source) { source.close(); source = null; } updateSend(); if ($("runMeta")) showRunCost(id); });   // 終わったら、応答したモデルと費用を読み直す
  source.addEventListener("missing", () => { if (source) { source.close(); source = null; } chatAgent("<p>この調査の記録が見つかりません。</p>"); });
}
function frame(ts) {
  if (lastTs === null) lastTs = ts;
  const dt = Math.min(0.1, (ts - lastTs) / 1000); lastTs = ts;
  if (ctx.replay && playing && queue.length) {
    if (speed === 0) { ctx.instant = true; while (queue.length && queue[0].t <= stopAt) handle(queue.shift()); tree.layout(); ctx.flow.layout(); ctx.instant = false; }
    else {
      clock += dt * speed;
      if (queue[0].t - clock > 20) { skipped += queue[0].t - clock; clock = queue[0].t; showMode(); }   // 利用者の答えを待った時間などの長い空白は飛ばす
      while (queue.length && queue[0].t <= clock) handle(queue.shift());
    }
    if (!queue.length && !source) updateSend();   // 再生し終えたら、送信ボタンに戻す
  }
  // 何も起きていない画面では描き直しを休む（前は再生が終わっても毎フレーム、線の計算と SVG の書き直しをしていた）
  if (performance.now() < ctx.awakeUntil || ctx.flow?.busy?.() || (ctx.replay && playing && queue.length)) {
    activeView().draw(dt);
    stepCamera(dt);
    ctx.frames.drawn++;
  } else ctx.frames.idle++;
  requestAnimationFrame(frame);
}

// ---- 履歴の一覧（各行の「…」: 詳細・削除） ----
const LIVE = ["running", "waiting", "paused"], UNFINISHED = [...LIVE, "finishing"];
async function loadRuns() {
  try {
    const { runs } = await api(withKey("/api/runs"));
    RUNS = Object.fromEntries(runs.map((r) => [r.id, r]));
    const defAgent = Object.values(ctx.AGENTS).find((a) => a.default)?.id;
    $("runs").innerHTML = runs.map((r) => `<div class="runrow" data-id="${esc(r.id)}"><button type="button" class="run" data-id="${esc(r.id)}" data-agent="${esc(r.agent || "")}" data-live="${LIVE.includes(r.status)}">
      <span class="r1">${esc(r.request || "(依頼なし)")}</span><span class="r2">${esc(r.id.slice(0, 13).replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2}).*/, "$2/$3 $4:$5"))} · ${esc(statusLabel(r.status))}${ctx.AGENTS[r.agent] && r.agent !== defAgent ? " · " + esc(ctx.AGENTS[r.agent].name) : ""}${runModelTag(r.models) ? " · " + esc(runModelTag(r.models)) : ""}</span></button>
      <button type="button" class="more" data-id="${esc(r.id)}" aria-haspopup="menu" aria-expanded="false" aria-label="「${esc(r.request || "依頼")}」の操作" title="操作">…</button></div>`).join("")
      || `<p class="r2" style="padding:8px;color:var(--muted)">まだ調査はありません</p>`;
    $("runs").querySelectorAll(".run").forEach((b) => b.addEventListener("click", () => { closeSide(); open(b.dataset.id, b.dataset.live === "true", b.dataset.agent); }));
    $("runs").querySelectorAll(".more").forEach((b) => b.addEventListener("click", (ev) => { ev.stopPropagation(); openRunMenu(b); }));
    showRecent(runs);
    highlightRun();
  } catch (err) { $("runs").innerHTML = `<p style="padding:8px;color:var(--muted)">一覧を読めません: ${esc(err.message)}</p>`; }
}
function highlightRun() { $("runs").querySelectorAll(".run").forEach((b) => b.classList.toggle("on", b.dataset.id === ctx.runId)); }

let menuFor = null;
function openRunMenu(btn) {
  const r = RUNS[btn.dataset.id]; if (!r) return;
  if (menuFor === btn) { closeRunMenu(); return; }
  closeRunMenu(); menuFor = btn; btn.setAttribute("aria-expanded", "true");
  const busyRun = UNFINISHED.includes(r.status), m = $("runMenu");
  m.innerHTML = `<button type="button" role="menuitem" data-act="detail">詳細を見る</button>
    <button type="button" role="menuitem" data-act="delete"${busyRun ? ` aria-disabled="true"` : ""}>削除</button>
    ${busyRun ? `<p class="mnote">調査が終わってから削除できます（${esc(statusLabel(r.status))}）</p>` : ""}`;
  const rect = btn.getBoundingClientRect();
  m.style.top = Math.min(rect.bottom + 4, window.innerHeight - 140) + "px"; m.style.left = Math.max(8, rect.right - 190) + "px";
  m.hidden = false;
  m.querySelector("[data-act=detail]").addEventListener("click", () => { closeRunMenu(); closeSide(); openPanel("run", btn, r); });
  m.querySelector("[data-act=delete]").addEventListener("click", () => { if (busyRun) return; closeRunMenu(); deleteRun(r, btn); });
  m.querySelector("button").focus();
}
function closeRunMenu() { if (menuFor) menuFor.setAttribute("aria-expanded", "false"); menuFor = null; $("runMenu").hidden = true; }
document.addEventListener("click", (ev) => { if (menuFor && !$("runMenu").contains(ev.target) && ev.target !== menuFor) closeRunMenu(); });
async function deleteRun(r, btn) {
  const ok = window.confirm(`「${r.request || r.id}」を削除しますか？\n\n`
    + "消すのは、このアプリの履歴・報告・作業記録です。Open WebUI の会話と、調査エージェントの内部の診断ファイルは別に管理しているため、消えません。\n"
    + "削除すると元に戻せません。");
  if (!ok) { btn.focus(); return; }
  try {
    await api(`/api/runs/${encodeURIComponent(r.id)}`, undefined, "DELETE");
    if (ctx.runId === r.id) newResearch();        // 開いていた履歴なら、購読と再生を閉じて新しい調査に戻る
    await loadRuns();
  } catch (err) { window.alert(`削除できませんでした: ${err.message}`); }
}

// ---- ペインの幅（ドラッグ・矢印キー）と狭い画面の切り替え ----
function relayout(snap = false) {
  tree.layout(); ctx.flow?.layout();
  if (snap) { fitTarget(true); }                  // 隠れていた作業記録を出したときは、今の大きさに合わせ直す
  wake();
}
initLayout((snap) => relayout(snap));

window.addEventListener("resize", () => relayout());
window.researchAgentFrames = ctx.frames;   // 描いた・休んだフレームの数（ブラウザで確かめるため）
reset(); requestAnimationFrame(frame);
const params = new URLSearchParams(location.search);
if (params.has("speed")) speed = +params.get("speed") || 0;   // 再生の速さ（0 = すぐ全部を出す。確認用）
if (params.has("at")) stopAt = +params.get("at");   // この時刻（秒）の状態で止める
const ready = Promise.all([loadAgents(), loadRuns()]);
const first = params.get("run");
ready.then(() => {
  if (["research", "models", "status", "display"].includes(params.get("panel"))) openPanel(params.get("panel"));   // 確認用: 開いた状態で出す
  if (!first) return;
  const r = RUNS[first];
  open(first, !!r && LIVE.includes(r.status), r?.agent, r?.models, r?.settings);
});
