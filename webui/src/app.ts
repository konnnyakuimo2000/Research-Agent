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
//
// 部品の間で共有する状態（エージェント・モデルの一覧・次の依頼の設定・履歴・調査の動き）は stores/state.ts のストア。
// ここはそれを読んで（subscribe）、チャットの上・履歴・ボタンを描き直す
import "./style.css";
import { $, esc } from "./core/util.ts";
import { api, withKey } from "./core/api.ts";
import { ctx, hasFlow, caps, wake } from "./core/ctx.ts";
import { dispatch, layoutFor, loadExtensions, norm, rememberExtensions, resetExtensions, runExtensions } from "./core/events.ts";
import { fitMode, fitTarget, resetCamera, stepCamera, useView } from "./core/camera.ts";
import { tree } from "./views/tree.ts";
import { GraphFlow } from "./views/flow.ts";
import { chatAgent, resetChat, runMeta, setEmpty, now } from "./panels/chat.ts";
import { closeDetails } from "./panels/details.ts";
import { answeredMeta, fmtUsd, loadModels, modelMeta } from "./panels/models.ts";
import { consumeOneTime, headState, hooks as setHooks, initSettings, loadAgentStatus, openPanel,
  panelOpen, requestBody, settingsMeta } from "./panels/settings.ts";
import { initLayout, showPane } from "./panels/layout.ts";
import { agents, catalog, live as liveStore, nextAgent, openRunId, refreshRuns, runMenu, runsById, sendAnswer, setLive, waitAnswer } from "./stores/state.ts";
import { mount } from "svelte";
import SettingsPanel from "./components/settings/SettingsPanel.svelte";
import RunList from "./components/history/RunList.svelte";
import RunMenu from "./components/history/RunMenu.svelte";
import Recent from "./components/history/Recent.svelte";
import ChatLog from "./components/chat/ChatLog.svelte";
import type { AgentsReply, RunSummary, SettingsValues, UIEvent } from "./types/api.ts";

/** チャットの上の行（panels/settings.ts の headState） */
type Head = { agent: string; preset: string; models: string; oneTime: string; external: boolean; warnings: string[] } | null;
const appEl = () => document.querySelector<HTMLElement>(".app")!;

// ---- エージェント（ゲートウェイがつないでいるもの） ----
function setLayout(id: string): void {
  const only = !hasFlow(id);
  appEl().classList.toggle("chatonly", only);
  if (only) showPane("chat");
  // 一時停止・再開のボタンは作業記録の上に置く。作業記録のないエージェントでは、チャットの上に置く
  (only ? $("headCtl") : document.querySelector(".hud-top")!).appendChild($("runCtl"));
}
let defaultAgent = "research";
async function loadAgents() {
  try {
    const { agents: list, default: def } = await api<AgentsReply>("/api/agents");
    defaultAgent = def;
    rememberExtensions(list);
    await loadExtensions([...new Set(list.flatMap((a) => a.extensions || []))]);
    agents.set(Object.fromEntries(list.map((a) => [a.id, a])));
    initSettings(def);                                   // 覚えているエージェントと設定（ctx.agentId を決める）
    if (!ctx.runId) { ctx.runAgent = ctx.agentId; reset(); }
    setLayout(ctx.runId ? ctx.runAgent : ctx.agentId);   // 名乗りが届く前に開いた依頼も、段に合わせて出し直す
    loadModels();
  } catch (err) { /* 一覧が取れなければ、既定のエージェントに送る */ }
}
// 次の依頼のエージェントが変わったとき。開いている履歴と、ライブの購読はそのまま
nextAgent.subscribe((a) => {
  if (!a) return;
  if (!ctx.runId) { ctx.runAgent = a; reset(); setLayout(a); }
  loadAgentStatus(a);
});
setHooks.startNew = () => newResearch();

// チャットの上: 次の依頼に効くエージェント・プリセット・モデルと、送信に響く異常（外部モデル・接続・使えなくなったモデル）
headState.subscribe(renderHead);
function renderHead(s: Head): void {
  const btn = $("headSummary");
  if (!s) { btn.textContent = ""; $("headWarn").hidden = true; return; }
  const parts = [s.agent, s.preset, s.models].filter(Boolean);
  btn.innerHTML = `<span class="lbl">次の依頼</span><span class="txt">${esc(parts.join(" · "))}</span>`
    + (s.oneTime ? `<span class="badge">${esc(s.oneTime)}</span>` : "") + (s.external ? `<span class="ext">外部</span>` : "");
  btn.title = `次の依頼: ${parts.join(" · ")}${s.oneTime ? `（${s.oneTime}）` : ""}`;   // 幅が狭くて切れても読めるように
  btn.setAttribute("aria-label", `次の依頼:${parts.join("、")}${s.oneTime ? `（${s.oneTime}）` : ""}${s.external ? "、外部のモデルを使う" : ""}。押すと設定を開く`);
  $("headWarn").hidden = !s.warnings.length;
  $("headWarn").innerHTML = s.warnings.map(esc).join("<br>");
}
$("headSummary").addEventListener("click", (ev) => openPanel("research", ev.currentTarget as HTMLElement));
$("settingsBtn").addEventListener("click", (ev) => { closeSide(); openPanel("research", narrow() ? $("runsBtn") : ev.currentTarget as HTMLElement); });

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
function setView(v: "flow" | "tree"): void {
  ctx.view = v;
  $("graph").hidden = v !== "flow"; tree.el.hidden = v !== "tree";
  activeView().snap();
  $("flowEmpty").hidden = !activeView().isEmpty();
  document.querySelectorAll<HTMLElement>("[data-view]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === v)));
  useView(activeView()); fitMode(true); fitTarget(true);   // 切り替えたら、カメラもすぐ全体に合わせる
  wake();
}
document.querySelectorAll<HTMLElement>("[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view as "flow" | "tree")));
function handle(e: UIEvent): void { dispatch(e); ctx.flow.on(e); wake(); }

// ---- 調査の開始・答え・一覧 ----
// liveState（動いている調査の状態: running / paused / ""）と waitingRun（答えを待っている依頼）は、ストアの写し
let source: EventSource | null = null, waitingRun: string | null = null, liveState = "";
function showComposer() {
  $("composer").classList.toggle("answer", !!waitingRun);
  $<HTMLTextAreaElement>("input").placeholder = waitingRun ? "答えを入力（番号でも可）" : "調べたいことを入力";
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
  const b = $<HTMLButtonElement>("sendBtn"), liveBusy = stop && !ctx.replay;
  b.disabled = liveBusy;
  b.title = liveBusy ? "調査中" : waitingRun ? "答えを送る" : "送る";
  b.setAttribute("aria-label", b.title);
}
async function togglePause() {
  if (ctx.replay) { playing = !playing; updateSend(); now(playing ? "再生している" : "一時停止している"); return; }
  const action = !caps(ctx.runAgent).includes("pause") ? "cancel" : liveState === "paused" ? "resume" : "pause";
  try { await api(`/api/runs/${ctx.runId}/${action}`, {}); }
  catch (err) { chatAgent(`<p>${action === "resume" ? "再開できませんでした" : "止められませんでした"}: ${esc((err as Error).message)}</p>`); }
}
$("runCtl").addEventListener("click", togglePause);
$("composer").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (busy() && !ctx.replay) return;
  const text = input.value.trim(); if (!text) return;
  if (waitingRun) {
    input.value = ""; growInput();
    sendAnswer(text).catch((err) => chatAgent(`<p>答えを送れませんでした: ${esc((err as Error).message)}</p>`));
    return;
  }
  const body = requestBody();
  try {
    const r = await api("/api/runs", { request: text, ...body });
    input.value = ""; growInput();
    consumeOneTime();                                  // 「この依頼だけ」は次の依頼に持ち越さない
    open(r.id, true, r.agent, r.models, r.settings || body.settings); refreshRuns();
  } catch (err) { chatAgent(`<p>始められませんでした: ${esc((err as Error).message)}</p>`); }   // 送れなければ、入力した文は残す
});
function newResearch() {
  openSeq++; closeSource(); ctx.runId = null; ctx.runAgent = ctx.agentId; reset(); setLayout(ctx.agentId); resetChat(); setEmpty(true);
  ctx.replay = false; setLive(""); showMode(); waitAnswer(false); openRunId.set(null); $("input").focus();
}
$("newBtn").addEventListener("click", () => { closeSide(); newResearch(); });
// 履歴: 広い画面では列を出す・隠す。狭い画面（列にしない幅）では、チャットの上に重ねて開く
const narrow = () => window.matchMedia("(max-width: 1180px)").matches;
const closeSide = () => appEl().classList.remove("sideopen");
$("runsBtn").addEventListener("click", () => {
  const app = appEl();
  if (narrow()) app.classList.toggle("sideopen");
  else { app.classList.toggle("noside"); setTimeout(() => { fitMode(true); relayout(); }, 50); }
});
$("scrim").addEventListener("click", closeSide);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !panelOpen()) { closeSide(); runMenu.set(null); } });

// 入力欄: Enter で送る（Shift+Enter は改行。日本語の変換を確定する Enter では送らない）。行が増えたら高さを伸ばす
const input = $<HTMLTextAreaElement>("input");
function growInput() { input.style.height = "auto"; input.style.height = Math.min(180, input.scrollHeight) + "px"; }
input.addEventListener("input", growInput);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); $<HTMLFormElement>("composer").requestSubmit(); }
});

setInterval(() => loadAgentStatus(), 30000);
setInterval(loadModels, 60000);

// 依頼で使ったモデル・設定・費用を、チャットの頭に小さく出す（押すと履歴の詳細。描くのは ChatLog）
let meta = { assigned: "", priced: false };
function showRunMeta(id: string, models?: Record<string, string>, settings?: SettingsValues): void {
  const m = modelMeta(models), s = settingsMeta(ctx.runAgent, settings);
  meta = { assigned: m?.text || "", priced: !!m?.priced };
  runMeta.set({ models: m?.text || "", modelsTitle: "", settings: s, cost: "" });   // モデルの一覧が後から届いたら、作り直す
  if (m) showRunCost(id);
}
// 窓口が数えた、この調査の費用と応答したモデル。応答したモデルが分かれば、割り当ての表示をそれに置き換える
// （割り当ては title に残す）。分からない（窓口を通していない古い記録など）ときは、割り当てのまま
async function showRunCost(id: string): Promise<void> {
  try {
    const u = await api(`/api/runs/${encodeURIComponent(id)}/usage`);
    if (id !== ctx.runId) return;
    const a = answeredMeta(u.answered);
    runMeta.update((cur) => cur && {
      ...cur,
      ...(a && cur.models ? { models: a.text, modelsTitle: `${a.title}\n${meta.assigned}` } : {}),
      cost: meta.priced ? `費用 ${fmtUsd(u.usd || 0)}${u.limit_usd ? ` / 上限 ${fmtUsd(u.limit_usd)}` : ""}` : "",
    });
  } catch (err) { /* 費用を読めなくても、調査の表示は続ける */ }
}

// 再生: 終わった調査は、記録の時刻どおりに（速さを掛けて）流す。動いている調査は届いたらすぐ描く
let RUNS: Record<string, RunSummary> = {};   // 一覧の依頼（id → 要約。使ったモデル・設定を出すため）
let queue: UIEvent[] = [], clock = 0, speed = 8, playing = true, lastTs: number | null = null, stopAt = Infinity, openSeq = 0, skipped = 0, lastSeen = 0;
// ライブか履歴の再生か。再生は実際の速さと、飛ばした無イベントの時間も出す
function showMode(text?: string): void { $("modeBadge").textContent = text ?? (ctx.replay
  ? (speed === 0 ? "履歴（すぐに表示）" : `履歴を${speed}倍で再生`) + (skipped ? ` · 無イベントの ${Math.round(skipped)} 秒を省略` : "")
  : ctx.runId ? "ライブ" : ""); }
function closeSource() { if (source) { source.close(); source = null; } queue = []; }
async function open(id: string, live: boolean, agent?: string, models?: Record<string, string>, settings?: SettingsValues): Promise<void> {
  const seq = ++openSeq;
  closeSource();
  const agentId = agent || RUNS[id]?.agent || defaultAgent;
  await loadExtensions(runExtensions(agentId));   // その依頼のエージェントが名乗った拡張の部品
  if (seq !== openSeq) return;                    // 待っている間に別の依頼を開いた
  ctx.runId = id; ctx.runAgent = agentId; reset(); setLayout(agentId); resetChat();
  showRunMeta(id, models || RUNS[id]?.models, settings || RUNS[id]?.settings);
  ctx.replay = !live; clock = 0; playing = true; skipped = 0; lastSeen = Date.now();
  setLive(live ? "running" : ""); showMode();
  waitAnswer(false); openRunId.set(id);
  source = new EventSource(withKey(`/api/runs/${encodeURIComponent(id)}/events`));
  source.onmessage = (m) => { lastSeen = Date.now(); const e = norm(JSON.parse(m.data)); if (ctx.replay) queue.push(e); else handle(e); };
  // 通信が切れて実行の状況を確かめられないときは、最後に受け取った時刻を出す（EventSource はつなぎ直す）
  source.onerror = () => { if (!ctx.replay && source) showMode(`接続が切れた（最後に受け取った ${new Date(lastSeen).toLocaleTimeString("ja-JP")}）`); };
  source.onopen = () => showMode();
  source.addEventListener("end", () => { if (source) { source.close(); source = null; } updateSend(); if (ctx.runId === id) showRunCost(id); });   // 終わったら、応答したモデルと費用を読み直す
  source.addEventListener("missing", () => { if (source) { source.close(); source = null; } chatAgent("<p>この調査の記録が見つかりません。</p>"); });
}
// イベントの時刻（依頼の開始からの秒。届いた封筒には必ずある）
const at = (e: UIEvent): number => e.t ?? 0;
function frame(ts: number): void {
  if (lastTs === null) lastTs = ts;
  const dt = Math.min(0.1, (ts - lastTs) / 1000); lastTs = ts;
  if (ctx.replay && playing && queue.length) {
    if (speed === 0) { ctx.instant = true; while (queue.length && at(queue[0]) <= stopAt) handle(queue.shift()!); tree.layout(); ctx.flow.layout(); ctx.instant = false; }
    else {
      clock += dt * speed;
      if (at(queue[0]) - clock > 20) { skipped += at(queue[0]) - clock; clock = at(queue[0]); showMode(); }   // 利用者の答えを待った時間などの長い空白は飛ばす
      while (queue.length && at(queue[0]) <= clock) handle(queue.shift()!);
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

// ---- 履歴の一覧（components/history/。各行の「…」: 詳細・削除） ----
const LIVE = ["running", "waiting", "paused"];
async function deleteRun(r: RunSummary, btn: HTMLElement): Promise<void> {
  const ok = window.confirm(`「${r.request || r.id}」を削除しますか？\n`
    + "履歴・報告・作業記録を消します（Open WebUI の会話は残ります）。元に戻せません。");
  if (!ok) { btn.focus(); return; }
  try {
    await api(`/api/runs/${encodeURIComponent(r.id)}`, undefined, "DELETE");
    if (ctx.runId === r.id) newResearch();        // 開いていた履歴なら、購読と再生を閉じて新しい調査に戻る
    await refreshRuns();
  } catch (err) { window.alert(`削除できませんでした: ${(err as Error).message}`); }
}

// ---- ペインの幅（ドラッグ・矢印キー）と狭い画面の切り替え ----
function relayout(snap = false) {
  tree.layout(); ctx.flow?.layout();
  if (snap) { fitTarget(true); }                  // 隠れていた作業記録を出したときは、今の大きさに合わせ直す
  wake();
}
initLayout((snap) => relayout(snap));

window.addEventListener("resize", () => relayout());
(window as any).researchAgentFrames = ctx.frames;   // 描いた・休んだフレームの数（ブラウザで確かめるため）

// ストアを読む（ここで購読するのは、上の変数がすべて用意できてから呼ばれるようにするため）
liveStore.subscribe((l) => { liveState = l.state; waitingRun = l.waitingRun; showComposer(); updateSend(); });
runsById.subscribe((v) => { RUNS = v; });
// モデルの一覧が後から届いたら、開いている調査の頭（使ったモデル・費用）を作り直す
catalog.subscribe(() => { if (ctx.runId && RUNS[ctx.runId]) showRunMeta(ctx.runId, RUNS[ctx.runId].models, RUNS[ctx.runId].settings); });

mount(SettingsPanel, { target: $("settingsRoot") });
mount(RunList, { target: $("runs"), props: { onOpen: (r) => { closeSide(); open(r.id, LIVE.includes(r.status), r.agent); } } });
mount(RunMenu, { target: $("runMenuRoot"), props: {
  onDetail: (r, anchor) => { closeSide(); openPanel("run", anchor, r); },
  onDelete: (r, anchor) => deleteRun(r, anchor),
} });
mount(Recent, { target: $("examples"), props: { onPick: (q) => { input.value = q; growInput(); input.focus(); } } });
mount(ChatLog, { target: $("chat"), props: { onMeta: (btn) => { const r = ctx.runId ? RUNS[ctx.runId] : undefined; if (r) openPanel("run", btn, r); } } });
reset(); requestAnimationFrame(frame);
const params = new URLSearchParams(location.search);
if (params.has("speed")) speed = +(params.get("speed") || 0);   // 再生の速さ（0 = すぐ全部を出す。確認用）
if (params.has("at")) stopAt = +(params.get("at") || 0);   // この時刻（秒）の状態で止める
const ready = Promise.all([loadAgents(), refreshRuns()]);
const first = params.get("run");
ready.then(() => {
  const panelTab = params.get("panel") as "research" | "models" | "status" | "display" | null;
  if (panelTab && ["research", "models", "status", "display"].includes(panelTab)) openPanel(panelTab);   // 確認用: 開いた状態で出す
  if (!first) return;
  const r = RUNS[first];
  open(first, !!r && LIVE.includes(r.status), r?.agent, r?.models, r?.settings);
});
