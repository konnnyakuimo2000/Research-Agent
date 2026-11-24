// 依頼の設定（エージェントの settings_schema から作る欄と、presets。docs/telemetry_enhancement_plan.md D-1・D-2）。
// 設定の形を持たないエージェントでは、ボタンを出さない。選んだ設定はエージェントごとに覚える
import { $, esc } from "../core/util.js";
import { store } from "../core/api.js";
import { ctx } from "../core/ctx.js";
import { changed, coerce, defaults, fieldsHtml, hasFields, readFields } from "../core/schema-form.js";
import { placePop } from "./popover.js";

const pop = $("setPop"), btn = $("setBtn");
let saved = { preset: "", values: {} };
const schema = () => ctx.AGENTS[ctx.agentId]?.settings_schema || {};
const presets = () => ctx.AGENTS[ctx.agentId]?.presets || {};

export function loadSettings() {
  saved = store.get("settings:" + ctx.agentId, null) || { preset: "", values: {} };
  renderSettings();
}
const save = () => store.set("settings:" + ctx.agentId, saved);
// 送る設定（既定と違うものだけ）
export const chosenSettings = () => hasFields(schema()) ? changed(schema(), saved.values) : {};

// 設定がどれかのプリセットと同じなら、その名前
function presetOf(values) {
  const sc = schema(), d = defaults(sc), mine = changed(sc, values);
  for (const [id, p] of Object.entries(presets())) {
    const theirs = changed(sc, { ...d, ...(p.values || p) });
    if (JSON.stringify(Object.entries(theirs).sort()) === JSON.stringify(Object.entries(mine).sort())) return id;
  }
  return "";
}
export function renderSettings() {
  const sc = schema();
  btn.hidden = !hasFields(sc);
  if (btn.hidden) { closeSettings(); return; }
  // ボタンはアイコンだけ（変えたら点が付く）。プリセットの名前と中身は、ボタンの説明に出す
  const mine = chosenSettings(), p = presets()[saved.preset], on = Object.keys(mine).length > 0;
  $("setLabel").textContent = !on ? "" : p ? (p.title || saved.preset) : "変更";
  btn.classList.toggle("on", on);
  btn.title = on ? `設定（${$("setLabel").textContent}）: ` + Object.entries(mine).map(([k, v]) => `${sc.properties[k].title || k} ${v}`).join("、")
    : "設定（エージェントの既定で調べる）";
  btn.setAttribute("aria-label", btn.title);
  if (!pop.hidden) renderPop();
}
function renderPop() {
  const sc = schema(), ps = Object.entries(presets()), d = defaults(sc);
  // 何も変えていなければ「既定」、プリセットと同じならそのプリセットを選んだ表示にする（どれとも違えば、どれも選ばない）
  const anyChange = Object.keys(chosenSettings()).length > 0, current = anyChange ? saved.preset : "";
  pop.innerHTML = `<h4>設定 <span class="since">${esc(ctx.AGENTS[ctx.agentId]?.name || "")}の次の依頼から</span></h4>
    ${ps.length ? `<div class="presets" role="group" aria-label="プリセット">
      <button type="button" class="pre${!anyChange ? " on" : ""}" data-p="">既定</button>
      ${ps.map(([id, p]) => `<button type="button" class="pre${anyChange && current === id ? " on" : ""}" data-p="${esc(id)}" title="${esc(Object.entries(p.values || p).map(([k, v]) => `${sc.properties[k]?.title || k}: ${v}`).join("\n"))}">${esc(p.title || id)}</button>`).join("")}
    </div>` : ""}
    <div class="fields">${fieldsHtml(sc, { ...d, ...saved.values })}</div>`;
  pop.querySelectorAll(".pre").forEach((b) => b.addEventListener("click", () => {
    const p = presets()[b.dataset.p];
    saved = { preset: b.dataset.p, values: p ? { ...d, ...(p.values || p) } : {} };
    save(); renderSettings();
  }));
  pop.querySelector(".fields").addEventListener("change", () => {
    const values = readFields(pop.querySelector(".fields"), sc);
    saved = { preset: presetOf(values), values };
    save(); renderSettings();
  });
}
function openSettings() { pop.hidden = false; btn.setAttribute("aria-expanded", "true"); renderPop(); placePop(pop); }
export function closeSettings() { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); }
btn.addEventListener("click", () => (pop.hidden ? openSettings() : closeSettings()));
// 窓の外を押したら閉じる。窓の中は押すと描き直すので、押した時点の経路で確かめる
document.addEventListener("click", (ev) => { const path = ev.composedPath(); if (!pop.hidden && !path.includes(pop) && !path.includes(btn)) closeSettings(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !pop.hidden) { closeSettings(); btn.focus(); } });

// 依頼で使った設定（チャットの頭に出す）。エージェントの欄の名前で
export function settingsMeta(agentId, values) {
  const sc = ctx.AGENTS[agentId]?.settings_schema || {};
  const parts = Object.entries(values || {}).filter(([k]) => sc.properties?.[k]).map(([k, v]) => {
    const s = sc.properties[k], val = coerce(sc, k, v);
    return `${s.title || k}: ${s.type === "boolean" ? (val ? "はい" : "いいえ") : Array.isArray(val) ? val.join("・") : val}`;
  });
  return parts.length ? `設定: ${parts.join("、")}` : "";
}
