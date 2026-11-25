// モデル（モデルの窓口のもの。docs/telemetry_enhancement_plan.md 6.3・M4）
// ふだんは「全部まとめて 1 つ」を選ぶ。詳しくは役割ごとに選べる（役割の選択が優先）。何も選ばなければ窓口の役割の既定。
// 使えないモデル（鍵がない）・役割に要る能力がないモデルは選べない。外部のモデルを選んでいるときは「外部」と出す
import { $, esc } from "../core/util.js";
import { api, store } from "../core/api.js";
import { ctx } from "../core/ctx.js";
import { placePop } from "./popover.js";

let CATALOG = null, choice = { all: "", roles: {} }, modelStatus = null, statusTimer = null;
export const hooks = { afterLoad() {} };
const roleList = (id) => ctx.AGENTS[id]?.model_roles || [];
const modelOf = (id) => CATALOG?.models.find((m) => m.id === id);
const mTitle = (id) => modelOf(id)?.title || id;
export const fmtUsd = (v) => "$" + (v >= 1 ? v.toFixed(2) : v >= 0.01 ? v.toFixed(3) : v.toFixed(4));
const fmtNum = (v) => (v || 0).toLocaleString("ja-JP");
// そのモデルを、その役割（なければこのエージェントのすべての役割）に使えない理由（使えるなら空）
function whyNot(m, roles) {
  if (!m) return "一覧にない";
  if (m.available === false) return m.unavailable || "使えない";
  for (const r of roles) {
    const miss = (r.needs || []).filter((n) => !(m.capabilities || []).includes(n));
    if (miss.length) return `${r.title}に要る ${miss.join("・")} に対応していない`;
  }
  return "";
}
export function loadChoice() {
  choice = store.get("models:" + ctx.agentId, null) || { all: "", roles: {} };
  choice.roles = choice.roles || {};
}
const saveChoice = () => store.set("models:" + ctx.agentId, choice);
// 送る選び直し（役割 → モデル）。使えなくなったモデル（鍵を外したなど）は送らない（窓口の既定に戻る）
export function chosenModels() {
  if (!CATALOG) return {};
  const out = {};
  for (const r of roleList(ctx.agentId)) {
    const v = choice.roles[r.role] || choice.all;
    if (v && !whyNot(modelOf(v), [r])) out[r.role] = v;
  }
  return out;
}
function effective(role) { return chosenModels()[role] || CATALOG?.roles?.[role] || CATALOG?.default || ""; }
function priceText(m) {
  const p = m.price || {};
  return p.input_per_mtok != null ? `入力 $${p.input_per_mtok}・出力 $${p.output_per_mtok}（100 万トークン）` : "費用なし";
}
export async function loadModels() {
  try {
    const v = await api("/api/models");
    CATALOG = v.available ? v : null;
  } catch (err) { CATALOG = null; }
  renderModels(); await hooks.afterLoad();
  if (new URLSearchParams(location.search).get("panel") === "models" && CATALOG && $("modelPop").hidden) openModels();   // 確認用: 開いた状態で出す
}
export function renderModels() {
  const btn = $("modelBtn"), roles = roleList(ctx.agentId);
  btn.hidden = !CATALOG || !roles.length;
  $("modelName").hidden = !btn.hidden;            // 窓口のモデルを選べるときは、エージェントの設定のモデル名は出さない
  if (btn.hidden) { closeModels(); return; }
  const overrides = roles.filter((r) => choice.roles[r.role] && chosenModels()[r.role]);
  const all = choice.all && chosenModels()[roles[0].role] === choice.all ? choice.all : "";
  $("modelLabel").textContent = overrides.length ? "役割ごと" : all ? mTitle(all) : "既定のモデル";
  const used = [...new Set(roles.map((r) => effective(r.role)))];
  $("modelExt").hidden = !used.some((id) => modelOf(id)?.external);
  btn.title = roles.map((r) => `${r.title}: ${mTitle(effective(r.role))}${modelOf(effective(r.role))?.external ? "（外部）" : ""}`).join("\n");
  if (!$("modelPop").hidden) renderPop();
}
function defaultsText(roles) {
  const ids = [...new Set(roles.map((r) => CATALOG.roles?.[r.role] || CATALOG.default))];
  return ids.length === 1 ? `役割の既定（${mTitle(ids[0])}）` : "役割ごとの既定（窓口の設定どおり）";
}
function renderPop() {
  const roles = roleList(ctx.agentId), st = modelStatus?.models || {}, spend = modelStatus?.spend || {};
  const dot = (id) => { const s = st[id]; return `<i class="dot ${s?.ok ? "ok" : s && s.ok === false ? "ng" : ""}" title="${esc(s?.ok ? `応答 ${s.seconds ?? "?"} 秒` : s?.error || "状態を確かめていない")}"></i>`; };
  const opt = (id, title, sub, why) => `<button type="button" class="opt${(choice.all || "") === id ? " on" : ""}" data-all="${esc(id)}"${why ? ` disabled title="${esc(why)}"` : ""}>
      <span class="ot">${id ? dot(id) : ""}${esc(title)}${id && modelOf(id).external ? `<span class="ext">外部</span>` : ""}</span><span class="os">${esc(why || sub)}</span></button>`;
  const anyRole = roles.some((r) => choice.roles[r.role]);
  const limits = spend.limits || {};
  $("modelPop").innerHTML = `
    <h4>モデル</h4>
    <div class="opts">${opt("", "既定", defaultsText(roles), "")}
      ${CATALOG.models.map((m) => opt(m.id, m.title, `${m.external ? "外部の Web API" : "研究室・手元"} · ${priceText(m)}`, whyNot(m, roles))).join("")}</div>
    ${roles.length > 1 ? `<details class="roles"${anyRole ? " open" : ""}><summary>役割ごとに選ぶ</summary>
      ${roles.map((r) => `<label class="rrow"><span>${esc(r.title)}</span><select data-role="${esc(r.role)}">
        <option value="">上と同じ（${esc(mTitle(choice.all && !whyNot(modelOf(choice.all), [r]) ? choice.all : CATALOG.roles?.[r.role] || CATALOG.default))}）</option>
        ${CATALOG.models.map((m) => { const why = whyNot(m, [r]); return `<option value="${esc(m.id)}"${choice.roles[r.role] === m.id ? " selected" : ""}${why ? " disabled" : ""}>${esc(m.title)}${m.external ? "（外部）" : ""}${why ? `（${esc(why)}）` : ""}</option>`; }).join("")}
      </select></label>`).join("")}</details>` : ""}
    ${CATALOG.models.some((m) => m.external) ? `<p class="note">外部のモデルを選ぶと、依頼文と読んだページの本文がその会社に送られます。</p>` : ""}
    <h4>状態と使用量 <span class="since">窓口を立ち上げてから</span></h4>
    ${modelStatus ? `<table class="usage"><thead><tr><th>モデル</th><th>呼び出し</th><th>トークン（入力・出力）</th><th>費用</th></tr></thead><tbody>
      ${CATALOG.models.map((m) => { const s = st[m.id] || {}; return `<tr><td>${dot(m.id)}${esc(m.title)}</td><td class="mono">${fmtNum(s.calls)}${s.errors ? `（失敗 ${fmtNum(s.errors)}）` : ""}${s.refused ? `（上限で断った ${fmtNum(s.refused)}）` : ""}${s.unmeasured ? `（費用を測れなかった ${fmtNum(s.unmeasured)}）` : ""}</td>
        <td class="mono">${fmtNum(s.prompt_tokens)}・${fmtNum(s.completion_tokens)}</td><td class="mono">${m.price && Object.keys(m.price).length ? fmtUsd(s.cost_usd || 0) : "—"}</td></tr>`; }).join("")}
      </tbody></table>
      <p class="spend">今日の費用 <b class="mono">${fmtUsd(spend.today_usd || 0)}</b>${spend.unmeasured_today ? `（ほかに費用を測れなかった呼び出し ${fmtNum(spend.unmeasured_today)} 件）` : ""}${limits.daily_usd ? ` / 上限 ${fmtUsd(limits.daily_usd)}` : "（上限なし）"}${limits.run_usd ? ` · 1 つの調査の上限 ${fmtUsd(limits.run_usd)}` : ""}</p>`
      : `<p class="note">モデルの窓口の状態を読めません。</p>`}`;
  $("modelPop").querySelectorAll(".opt").forEach((b) => b.addEventListener("click", () => {
    choice.all = b.dataset.all; saveChoice(); renderModels();
  }));
  $("modelPop").querySelectorAll("select[data-role]").forEach((s) => s.addEventListener("change", () => {
    if (s.value) choice.roles[s.dataset.role] = s.value; else delete choice.roles[s.dataset.role];
    saveChoice(); renderModels();
  }));
}
async function loadModelStatus() {
  try { const v = await api("/api/models/status"); modelStatus = v.available ? v : null; } catch (err) { modelStatus = null; }
  if (!$("modelPop").hidden) renderPop();
}
function openModels() {
  $("modelPop").hidden = false; $("modelBtn").setAttribute("aria-expanded", "true");
  renderPop(); placePop($("modelPop")); loadModelStatus(); statusTimer = setInterval(loadModelStatus, 10000);
}
export function closeModels() {
  $("modelPop").hidden = true; $("modelBtn").setAttribute("aria-expanded", "false");
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
}
$("modelBtn").addEventListener("click", () => ($("modelPop").hidden ? openModels() : closeModels()));
document.addEventListener("click", (ev) => {
  if (!$("modelPop").hidden && !$("modelPop").contains(ev.target) && !$("modelBtn").contains(ev.target)) closeModels();
});
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !$("modelPop").hidden) { closeModels(); $("modelBtn").focus(); } });

// 調査で使ったモデル（役割ごと）を、チャットの頭に小さく出す。値段のあるモデルを使ったら、費用も出す
function modelSummary(models) {
  if (!CATALOG || !models || !Object.keys(models).length) return "";
  const titles = Object.fromEntries((ctx.AGENTS[ctx.runAgent]?.model_roles || []).map((r) => [r.role, r.title]));
  const groups = {};
  for (const [role, id] of Object.entries(models)) (groups[id] = groups[id] || []).push(titles[role] || role);
  return Object.entries(groups).map(([id, rs]) => `${Object.keys(groups).length > 1 ? rs.join("・") + " " : ""}${mTitle(id)}${modelOf(id)?.external ? "（外部）" : ""}`).join(" / ");
}
// 戻り値: 費用を出したか（値段のあるモデルを使った）
export function modelMeta(models) {
  const text = modelSummary(models);
  return text ? { text: `役割に割り当てたモデル: ${text}`, priced: Object.values(models).some((m) => Object.keys(modelOf(m)?.price || {}).length) } : null;
}
// 一覧に出す短いモデルの名前（窓口の役割の既定と違うときだけ）
export function runModelTag(models) {
  if (!CATALOG || !models) return "";
  const diff = Object.entries(models).filter(([role, id]) => id !== (CATALOG.roles?.[role] || CATALOG.default));
  if (!diff.length) return "";
  const ids = [...new Set(Object.values(models))];
  return ids.length === 1 ? mTitle(ids[0]) : "役割ごとのモデル";
}
export const hasCatalog = () => !!CATALOG;
