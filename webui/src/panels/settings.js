// 設定のパネル（.claude/ui-gateway-settings-plan.md）。左の一覧の下の「設定」から、同じページの中で開く
// （広い画面では右側、狭い画面では全面）。タブ: 調査・モデル・接続と使用量・表示。履歴の詳細も同じパネルに出す。
//
// 値の優先順位: この依頼だけの上書き（oneTime）→ このブラウザの既定値（saved。エージェントごと）→ エージェント・窓口の既定。
// 「この依頼に適用」は次に送る 1 件だけに効き、送ったら消える。「このブラウザの既定値に保存」は次の依頼から毎回効く。
// 「標準に戻す」は、名乗りの既定と窓口の既定モデルに戻す（適用か保存で反映）。ブラウザの保存の形は前と同じ
// （settings:<エージェント> = { preset, values }、models:<エージェント> = { all, roles }、agent = エージェント）
import { $, esc } from "../core/util.ts";
import { api, store, withKey } from "../core/api.ts";
import { ctx } from "../core/ctx.ts";
import { changed, coerce, defaults, fieldsHtml, hasFields, readFields } from "../core/schema-form.ts";
import { answeredMeta, choiceLabel, emptyChoice, fmtUsd, gatewayStatusHtml, invalidChoices, loadModelStatus, modelOf,
  modelTabHtml, mTitle, resolveModels, roleList, usesExternal, wireModelTab } from "./models.js";
import { keysTabHtml, loadKeys, wireKeysTab } from "./keys.js";
import { resetWidths, widthsText } from "./layout.js";
import { agentStatus, agents, catalog, prefs } from "../stores/state.ts";
import { derived } from "svelte/store";

// 「この設定を使う」で新しい調査の画面にする（app.js が入れる。画面を Svelte の部品にしたら、部品の引数にする）
export const hooks = { startNew() {} };

const schemaOf = (agent) => ctx.AGENTS[agent]?.settings_schema || {};
const presetsOf = (agent) => ctx.AGENTS[agent]?.presets || {};
const clone = (x) => JSON.parse(JSON.stringify(x));
const sameValues = (a, b) => JSON.stringify(Object.entries(a).sort()) === JSON.stringify(Object.entries(b).sort());
const TABS = [["research", "調査"], ["models", "モデル"], ["keys", "API キー"], ["status", "接続と使用量"], ["display", "表示"]];

function loadSaved(agent) {
  const s = store.get("settings:" + agent, null) || {}, m = store.get("models:" + agent, null) || {};
  return { agent, settings: { preset: s.preset || "", values: { ...(s.values || {}) } }, models: { all: m.all || "", roles: { ...(m.roles || {}) } } };
}
let draft = null, tab = "research", runView = null, opener = null, notice = "";
let P = { saved: null, oneTime: null }, ST = { data: null, at: 0 };
prefs.subscribe((v) => { P = v; });
agentStatus.subscribe((v) => { ST = v; });
export const current = () => P.oneTime || P.saved;
export const isOneTime = () => !!P.oneTime;
const setPrefs = (saved, oneTime) => prefs.set({ saved, oneTime });

// エージェントの一覧が届いたら呼ぶ（覚えているエージェントがもうなければ既定に）
export function initSettings(defaultAgent) {
  const a = store.get("agent", "");
  setPrefs(loadSaved(ctx.AGENTS[a] ? a : defaultAgent), P.oneTime && ctx.AGENTS[P.oneTime.agent] ? P.oneTime : null);
}
// 送る中身（設定は既定と違うものだけ。モデルは使えるものだけ）
export function requestBody() {
  const c = current(), sc = schemaOf(c.agent);
  return { agent: c.agent, settings: hasFields(sc) ? changed(sc, c.settings.values) : {}, models: resolveModels(c.models, c.agent).send };
}
// 送ったら「この依頼だけ」を消す（次の依頼に持ち越さない）
export function consumeOneTime() { if (P.oneTime) setPrefs(P.saved, null); }

// 「標準」「すばやく」など。どのプリセットとも違えば「カスタム」。設定の欄がなければ空
export function presetName(agent, values) {
  const sc = schemaOf(agent);
  if (!hasFields(sc)) return "";
  const d = defaults(sc), mine = changed(sc, values);
  if (!Object.keys(mine).length) return "標準";
  for (const [id, p] of Object.entries(presetsOf(agent))) if (sameValues(changed(sc, { ...d, ...(p.values || p) }), mine)) return p.title || id;
  return "カスタム";
}
// チャットの上の要約（次の依頼に効くもの）
export function summary() {
  const c = current(); if (!c) return null;
  return { agent: ctx.AGENTS[c.agent]?.name || c.agent, preset: presetName(c.agent, c.settings.values), models: choiceLabel(c.models, c.agent),
    oneTime: P.oneTime ? P.oneTime.label || "この依頼だけ" : "", external: usesExternal(c.models, c.agent), warnings: invalidChoices(c.models, c.agent) };
}

// ---- 接続（エージェントと、エージェントが使うモデル・検索） ----
export async function loadAgentStatus(agent = ctx.agentId) {
  let data;
  try { data = await api(`/api/status?agent=${encodeURIComponent(agent)}`); }
  catch (err) { data = { agent, ok: false, error: err.message || "応答なし", checks: {} }; }
  agentStatus.set({ data, at: Date.now() });
  if (!panel.hidden && tab === "status") render();
  return data;
}
// 送信に響く接続の異常（チャットの上に短く出す。なければ空）
export function connectionProblem() {
  const st = ST.data;
  if (!st) return "";
  if (!st.ok) return "エージェントにつながらない";
  const bad = Object.values(st.checks || {}).find((c) => !c.ok);
  return bad ? bad.hint || `${bad.title}につながらない` : "";
}
// チャットの上の要約と、送信に響く異常。どれかのストアが変われば作り直す
export const headState = derived([prefs, agents, catalog, agentStatus], () => {
  const s = summary();
  if (!s) return null;
  const warnings = [connectionProblem(), ...s.warnings, s.external ? "外部のモデルに依頼文と読んだページを送ります" : ""].filter(Boolean);
  return { ...s, warnings };
});
function agentStatusHtml() {
  const st = ST.data, name = ctx.AGENTS[ctx.agentId]?.name || ctx.agentId;
  if (!st) return `<p class="note">まだ確かめていません。</p>`;
  const at = `<p class="since">${new Date(ST.at).toLocaleTimeString("ja-JP")} に確認</p>`;
  if (!st.ok) return `${at}<p class="ngline"><i class="dot ng"></i>${esc(name)}につながらない（${esc(st.error || "応答なし")}）</p>`;
  const checks = Object.values(st.checks || {});
  return `${at}<table class="usage"><thead><tr><th>確かめたこと</th><th>結果</th></tr></thead><tbody>
    <tr><td>${esc(name)}（エージェント）</td><td><i class="dot ok"></i>応答あり</td></tr>
    ${checks.map((c) => `<tr><td>${esc(c.title)}</td><td>${c.ok ? `<i class="dot ok"></i>${c.seconds ?? "?"} 秒で応答` : `<i class="dot ng"></i>${esc(c.error || "応答なし")}${c.hint ? ` · ${esc(c.hint)}` : ""}`}</td></tr>`).join("")}
    ${st.model ? `<tr><td>エージェントの設定のモデル</td><td class="mono">${esc(st.model)}</td></tr>` : ""}
  </tbody></table>`;
}

// ---- パネル ----
const panel = $("setPanel");
export function openPanel(which = "research", from = null, run = null) {
  opener = from || document.activeElement; runView = run; tab = run ? "run" : which; notice = "";
  draft = clone(current());
  panel.hidden = false; $("panelScrim").hidden = false;
  render(); $("setTitle").focus();
  if (tab === "status") refreshStatus();
  if (tab === "keys") loadKeys(render);
}
export function closePanel() {
  if (panel.hidden) return;
  panel.hidden = true; $("panelScrim").hidden = true; runView = null; draft = null;
  const back = opener; opener = null;
  if (back && back.isConnected !== false) back.focus?.();        // 開いたボタンに戻す
}
export const panelOpen = () => !panel.hidden;
async function refreshStatus() {
  await Promise.all([loadAgentStatus(), loadModelStatus()]);
  if (!panel.hidden && tab === "status") render();
}

function render() {
  const tabs = runView ? [...TABS, ["run", "履歴の詳細"]] : TABS;
  $("setTitle").textContent = tab === "run" ? "履歴の詳細" : "設定";
  $("setTabs").innerHTML = tabs.map(([id, label]) => `<button type="button" role="tab" data-tab="${id}" aria-selected="${tab === id}">${esc(label)}</button>`).join("");
  $("setTabs").querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
    tab = b.dataset.tab; notice = ""; render(); if (tab === "status") refreshStatus(); if (tab === "keys") loadKeys(render);
  }));
  const focused = document.activeElement?.id;
  const body = $("setBody");
  body.innerHTML = { research: researchHtml, models: () => modelTabHtml(draft.models, draft.agent), keys: keysTabHtml, status: statusHtml, display: displayHtml, run: () => runHtml(runView) }[tab]();
  if (tab === "research") wireResearch(body);
  if (tab === "models") wireModelTab(body, draft.models, render);
  if (tab === "keys") wireKeysTab(body, render);
  if (tab === "status") { body.querySelector("#recheck")?.addEventListener("click", refreshStatus); fillRunCost(); }
  if (tab === "display") body.querySelector("#resetWidths")?.addEventListener("click", () => { resetWidths(); notice = "幅を既定に戻した"; render(); });
  if (tab === "run") fillAnswered(runView);
  renderFoot();
  if (focused && $(focused) && body.contains($(focused))) $(focused).focus();   // 欄を変えて描き直しても、その欄に戻す
}
function renderFoot() {
  const foot = $("setFoot");
  if (tab === "research" || tab === "models") {
    foot.hidden = false;
    foot.innerHTML = `${notice ? `<p class="notice" role="status">${esc(notice)}</p>` : ""}
      <div class="fbtns"><button type="button" class="primary" id="applyOnce">この依頼に適用</button>
      <button type="button" id="saveDefault">このブラウザの既定値に保存</button>
      <button type="button" id="resetStd">標準に戻す</button></div>
      <p class="since">適用: 次の 1 件だけ · 保存: 毎回</p>`;
    $("applyOnce").addEventListener("click", applyOnce);
    $("saveDefault").addEventListener("click", saveDefault);
    $("resetStd").addEventListener("click", resetDraft);
  } else if (tab === "run" && runView) {
    foot.hidden = false;
    const known = !!ctx.AGENTS[runView.agent];
    const bad = known ? invalidChoices({ all: "", roles: runView.models || {} }, runView.agent) : [];
    foot.innerHTML = `${bad.length ? `<p class="notice warn">今は使えない役割があります:<br>${bad.map(esc).join("<br>")}</p>` : ""}
      <div class="fbtns"><button type="button" class="primary" id="useRun"${known ? "" : " disabled"}>この設定を使う</button></div>
      <p class="since">${known ? "新しい依頼に読み込みます" : "このエージェントは今つながっていません"}</p>`;
    $("useRun").addEventListener("click", () => useRun(runView));
  } else { foot.hidden = true; foot.innerHTML = ""; }
}
// 適用・保存したら、チャットに戻って上の要約を変える
function closeToChat() { closePanel(); $("input").focus(); }
// パネルのボタンの動き（テストからも呼ぶ）。draft はパネルで編集中の値
export const draftState = () => draft;
export function applyOnce() { const once = { ...clone(draft), label: "この依頼だけ" }; closeToChat(); setPrefs(P.saved, once); }
export function saveDefault() {
  const saved = clone(draft);
  store.set("agent", saved.agent); store.set("settings:" + saved.agent, saved.settings); store.set("models:" + saved.agent, saved.models);
  closeToChat(); setPrefs(saved, null);
}
export function resetDraft() {
  draft.settings = { preset: "", values: {} }; draft.models = emptyChoice();
  notice = "標準に戻しました（適用か保存で反映）"; render();
}

function researchHtml() {
  const agents = Object.values(ctx.AGENTS), sc = schemaOf(draft.agent), d = defaults(sc), ps = Object.entries(presetsOf(draft.agent));
  const name = presetName(draft.agent, draft.settings.values);
  return `${agents.length > 1 ? `<label class="rrow"><span>対象エージェント</span><select id="pAgent">${agents.map((a) => `<option value="${esc(a.id)}"${a.id === draft.agent ? " selected" : ""}${a.ok ? "" : " disabled"}>${esc(a.name)}${a.ok ? "" : "（つながらない）"}</option>`).join("")}</select></label>` : ""}
    ${!hasFields(sc) ? `<p class="note">変えられる設定はありません。</p>` : `
    ${ps.length ? `<div class="presets" role="group" aria-label="プリセット">
      <button type="button" class="pre${name === "標準" ? " on" : ""}" data-p="" aria-pressed="${name === "標準"}">標準</button>
      ${ps.map(([id, p]) => `<button type="button" class="pre${name === (p.title || id) ? " on" : ""}" data-p="${esc(id)}" aria-pressed="${name === (p.title || id)}" title="${esc(Object.entries(p.values || p).map(([k, v]) => `${sc.properties[k]?.title || k}: ${v}`).join("\n"))}">${esc(p.title || id)}</button>`).join("")}
      ${name === "カスタム" ? `<span class="pre on custom" aria-label="今の設定はカスタム">カスタム</span>` : ""}
    </div>` : ""}
    <div class="fields">${fieldsHtml(sc, { ...d, ...draft.settings.values })}</div>`}`;
}
function wireResearch(body) {
  body.querySelector("#pAgent")?.addEventListener("change", (ev) => { draft = loadSaved(ev.target.value); notice = ""; render(); });
  const sc = schemaOf(draft.agent), d = defaults(sc);
  body.querySelectorAll(".pre[data-p]").forEach((b) => b.addEventListener("click", () => {
    const p = presetsOf(draft.agent)[b.dataset.p];
    draft.settings = { preset: b.dataset.p, values: p ? { ...d, ...(p.values || p) } : {} }; render();
  }));
  body.querySelector(".fields")?.addEventListener("change", () => {
    draft.settings = { preset: "", values: readFields(body.querySelector(".fields"), sc) }; render();
  });
}
function statusHtml() {
  return `<h4>エージェント</h4>${agentStatusHtml()}
    <h4>モデルの窓口</h4>${gatewayStatusHtml()}
    ${ctx.runId ? `<h4>開いている調査の費用</h4><p class="spend" id="runCostLine">読み込み中…</p>` : ""}
    <div class="fbtns"><button type="button" id="recheck">確かめ直す</button></div>`;
}
async function fillRunCost() {
  const el = $("runCostLine"); if (!el || !ctx.runId) return;
  try {
    const u = await api(`/api/runs/${encodeURIComponent(ctx.runId)}/usage`);
    el.textContent = `${fmtUsd(u.usd || 0)}${u.unmeasured ? ` · 未計測 ${u.unmeasured} 件` : ""}${u.limit_usd ? ` · 上限 ${fmtUsd(u.limit_usd)}` : ""}`;
  } catch (err) { el.textContent = `読めません（${err.message}）`; }
}
function displayHtml() {
  return `${notice ? `<p class="notice" role="status">${esc(notice)}</p>` : ""}
    <p class="note">境界のドラッグか矢印キーで幅を変えられます。</p>
    <p class="since">${esc(widthsText())}</p>
    <div class="fbtns"><button type="button" id="resetWidths">幅を既定に戻す</button></div>`;
}

// ---- 履歴の詳細（開始時に効いた設定と、役割ごとのモデル。応答したモデルは記録があれば） ----
const LABEL = { running: "調べている", paused: "一時停止中", waiting: "答え待ち", finishing: "報告を保存中", done: "完了",
  error: "エラー", stopped: "途中で止まった", cancelled: "止めた" };
export const statusLabel = (s) => LABEL[s] || s;
function valueText(spec, v) {
  if (!spec) return Array.isArray(v) ? v.join("・") : String(v);
  return spec.type === "boolean" ? (v ? "はい" : "いいえ") : Array.isArray(v) ? v.join("・") : String(v);
}
function runHtml(r) {
  if (!r) return "";
  const sc = schemaOf(r.agent), props = sc.properties || {}, eff = r.effective_settings, changedSet = r.settings || {};
  const names = [...new Set([...Object.keys(props), ...Object.keys(eff || {}), ...Object.keys(changedSet)])];
  const when = r.created ? new Date(r.created * 1000).toLocaleString("ja-JP") : "";
  const roles = Object.fromEntries(roleList(r.agent).map((x) => [x.role, x.title]));
  return `<dl class="kv"><dt>依頼</dt><dd>${esc(r.request || "")}</dd><dt>エージェント</dt><dd>${esc(ctx.AGENTS[r.agent]?.name || r.agent)}</dd>
      <dt>状態</dt><dd>${esc(statusLabel(r.status))}</dd><dt>始めた時刻</dt><dd>${esc(when)}</dd><dt>ID</dt><dd class="mono">${esc(r.id)}</dd></dl>
    <h4>開始時に適用した調査の設定</h4>
    ${!eff ? `<p class="note">開始時の既定値は記録なし（変えた項目のみ）</p>` : ""}
    ${names.length ? `<table class="usage"><thead><tr><th>項目</th><th>値</th><th></th></tr></thead><tbody>
      ${names.map((k) => { const has = eff ? k in eff : k in changedSet, v = eff ? eff[k] : changedSet[k];
        return `<tr><td>${esc(props[k]?.title || k)}</td><td>${has ? esc(valueText(props[k], v)) : "記録なし"}</td><td>${k in changedSet ? "変更" : eff ? "既定" : ""}</td></tr>`; }).join("")}
    </tbody></table>` : `<p class="note">設定の記録はありません。</p>`}
    ${eff && presetName(r.agent, eff) ? `<p class="since">プリセット: ${esc(presetName(r.agent, eff))}</p>` : ""}
    <h4>役割ごとのモデル（開始時の割り当て）</h4>
    ${Object.keys(r.models || {}).length ? `<table class="usage"><tbody>${Object.entries(r.models).map(([role, id]) => `<tr><td>${esc(roles[role] || role)}</td><td>${esc(mTitle(id))}${modelOf(id) ? "" : "（今は一覧にない）"}</td></tr>`).join("")}</tbody></table>`
      : `<p class="note">記録なし</p>`}
    <h4>実際に応答したモデル</h4><p class="spend" id="runAnswered">読み込み中…</p>
    <h4>LLM とのやり取り</h4>
    <p class="fbtns"><a class="linkbtn" href="${esc(withKey(`/api/runs/${encodeURIComponent(r.id)}/llm-log`))}" target="_blank" rel="noopener">テキストで開く</a>
      <a class="linkbtn" href="${esc(withKey(`/api/runs/${encodeURIComponent(r.id)}/llm-log?download=1`))}" download>保存</a></p>
    <p class="since">送った全文と応答（記録を始めた後の調査だけ）</p>`;
}
async function fillAnswered(r) {
  const el = $("runAnswered"); if (!el || !r) return;
  try {
    const u = await api(`/api/runs/${encodeURIComponent(r.id)}/usage`);
    if (runView?.id !== r.id || !$("runAnswered")) return;
    const a = answeredMeta(u.answered, r.agent);
    el.textContent = a ? a.text.replace(/^応答したモデル: /, "") : "記録なし";
    if (a) el.title = a.title;
    if (u.usd || u.unmeasured) el.textContent += ` · 費用 ${fmtUsd(u.usd || 0)}${u.unmeasured ? `（未計測 ${u.unmeasured} 件）` : ""}`;
  } catch (err) { el.textContent = `読めません（${err.message}）`; }
}
// 過去の設定を、新しい依頼の「この依頼だけ」に読み込む（使えなくなったモデルは、チャットの上に理由と代わりを出す）
export function useRun(r) {
  const sc = schemaOf(r.agent), props = sc.properties || {};
  const base = r.effective_settings ? r.effective_settings : { ...defaults(sc), ...(r.settings || {}) };
  const values = Object.fromEntries(Object.entries(base).filter(([k]) => k in props).map(([k, v]) => [k, coerce(sc, k, v)]));
  closePanel();
  setPrefs(P.saved, { agent: r.agent, settings: { preset: "", values }, models: { all: "", roles: { ...(r.models || {}) } }, label: "過去の設定" });
  hooks.startNew(); $("input").focus();
}

$("setClose").addEventListener("click", closePanel);
$("panelScrim").addEventListener("click", closePanel);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !panel.hidden) { ev.stopPropagation(); closePanel(); } });

// 依頼で使った設定（チャットの頭に出す）。エージェントの欄の名前で
export function settingsMeta(agentId, values) {
  const sc = ctx.AGENTS[agentId]?.settings_schema || {};
  const parts = Object.entries(values || {}).filter(([k]) => sc.properties?.[k]).map(([k, v]) => {
    const s = sc.properties[k], val = coerce(sc, k, v);
    return `${s.title || k}: ${s.type === "boolean" ? (val ? "はい" : "いいえ") : Array.isArray(val) ? val.join("・") : val}`;
  });
  return parts.length ? `設定: ${parts.join("、")}` : "";
}
