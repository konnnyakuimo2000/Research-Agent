// モデル（モデルの窓口のもの。docs/telemetry_enhancement_plan.md 6.3・M4）。設定のパネルの「モデル」と「接続と使用量」に出す部品。
// 選び方（choice = { all, roles }）: 「全部まとめて 1 つ」と、役割ごとの上書き（役割の上書きが優先）。何も選ばなければ窓口の役割の既定。
// 使えないモデル（鍵がない）・役割に要る能力がないモデル・一覧から消えたモデルは送らず、理由と代わりに使うモデルを出す
import { esc } from "../core/util.js";
import { api } from "../core/api.js";
import { ctx } from "../core/ctx.js";

let CATALOG = null, modelStatus = null, statusAt = 0;
export const hooks = { afterLoad() {} };
export const roleList = (agentId) => ctx.AGENTS[agentId]?.model_roles || [];
export const modelOf = (id) => CATALOG?.models.find((m) => m.id === id);
export const mTitle = (id) => modelOf(id)?.title || id;
export const fmtUsd = (v) => "$" + (v >= 1 ? v.toFixed(2) : v >= 0.01 ? v.toFixed(3) : v.toFixed(4));
const fmtNum = (v) => (v || 0).toLocaleString("ja-JP");
export const hasCatalog = () => !!CATALOG;
export const emptyChoice = () => ({ all: "", roles: {} });
const isPriced = (m) => !!m && Object.keys(m.price || {}).length > 0;

// そのモデルを、その役割（なければこのエージェントのすべての役割）に使えない理由（使えるなら空）
export function whyNot(m, roles) {
  if (!m) return "一覧にない";
  if (m.available === false) return m.unavailable || "使えない";
  for (const r of roles) {
    const miss = (r.needs || []).filter((n) => !(m.capabilities || []).includes(n));
    if (miss.length) return `${r.title}に要る ${miss.join("・")} に対応していない`;
  }
  return "";
}
const roleDefault = (role) => CATALOG?.roles?.[role] || CATALOG?.default || "";

// 選び方を、役割ごとの最終的な割り当てにする。send は窓口に送る選び直し（使えないものは送らない）。
// rows は役割ごとの { role, title, needs, id（実際に使う）, asked（選んだもの）, why（選んだものが使えない理由） }
export function resolveModels(choice, agentId) {
  const send = {}, rows = [];
  if (!CATALOG) return { send, rows };
  for (const r of roleList(agentId)) {
    const asked = (choice?.roles || {})[r.role] || choice?.all || "";
    const why = asked ? whyNot(modelOf(asked), [r]) : "";
    if (asked && !why) send[r.role] = asked;
    rows.push({ role: r.role, title: r.title, needs: r.needs || [], asked, why, id: send[r.role] || roleDefault(r.role) });
  }
  return { send, rows };
}
// 選んだのに使えないもの（送る前に、理由と代わりに使うモデルを出す）
export function invalidChoices(choice, agentId) {
  return resolveModels(choice, agentId).rows.filter((r) => r.why)
    .map((r) => `${r.title}: ${mTitle(r.asked)} は使えない（${r.why}）→ ${mTitle(r.id)} を使う`);
}
export const usesExternal = (choice, agentId) => resolveModels(choice, agentId).rows.some((r) => modelOf(r.id)?.external);
// 短い名前（チャットの上の要約）
export function choiceLabel(choice, agentId) {
  const { rows } = resolveModels(choice, agentId);
  if (!rows.length) return "";
  const ids = [...new Set(rows.map((r) => r.id))], anyAsked = rows.some((r) => r.asked && !r.why);
  if (!anyAsked) return "既定のモデル";
  return ids.length === 1 ? mTitle(ids[0]) : "役割ごとのモデル";
}

export async function loadModels() {
  try {
    const v = await api("/api/models");
    CATALOG = v.available ? v : null;
  } catch (err) { CATALOG = null; }
  await hooks.afterLoad();
}
// モデルの窓口の状態と使用量（null = 窓口につながらない・窓口を使っていない）
export async function loadModelStatus() {
  try { const v = await api("/api/models/status"); modelStatus = v.available ? v : null; } catch (err) { modelStatus = null; }
  statusAt = Date.now();
  return modelStatus;
}
function priceText(m) {
  const p = m.price || {};
  return p.input_per_mtok != null ? `入力 $${p.input_per_mtok}・出力 $${p.output_per_mtok}（100 万トークンあたり）` : "費用なし";
}
const dot = (id) => {
  const s = modelStatus?.models?.[id];
  return `<i class="dot ${s?.ok ? "ok" : s && s.ok === false ? "ng" : ""}" title="${esc(s?.ok ? `一覧 API が ${s.seconds ?? "?"} 秒で応答` : s?.error || "状態を確かめていない")}"></i>`;
};

// ---- 設定のパネルの「モデル」 ----
export function modelTabHtml(choice, agentId) {
  const roles = roleList(agentId);
  if (!CATALOG) return `<p class="note">モデルの窓口につながらないため、モデルを選べません。エージェントの設定のモデルで調べます。</p>`;
  if (!roles.length) return `<p class="note">このエージェントは、モデルを選べる役割を名乗っていません。</p>`;
  const opt = (id, title, sub, why) => `<button type="button" class="opt${(choice.all || "") === id ? " on" : ""}" data-all="${esc(id)}" aria-pressed="${(choice.all || "") === id}"${why ? ` disabled` : ""}>
      <span class="ot">${id ? dot(id) : ""}${esc(title)}${id && modelOf(id).external ? `<span class="ext">外部</span>` : ""}</span><span class="os">${esc(why ? `選べない: ${why}` : sub)}</span></button>`;
  const ids = [...new Set(roles.map((r) => roleDefault(r.role)))];
  const { rows } = resolveModels(choice, agentId);
  const external = rows.filter((r) => modelOf(r.id)?.external);
  return `
    <h4>全役割共通</h4>
    <div class="opts">${opt("", "既定", ids.length === 1 ? `役割の既定（${mTitle(ids[0])}）` : "役割ごとの既定（窓口の設定どおり）", "")}
      ${CATALOG.models.map((m) => opt(m.id, m.title, `${m.external ? "外部の Web API" : "研究室・手元"} · ${priceText(m)}`, whyNot(m, roles))).join("")}</div>
    <h4>役割ごとの上書き</h4>
    ${roles.map((r) => `<label class="rrow"><span>${esc(r.title)}${r.needs?.length ? `<small>要る能力: ${esc(r.needs.join("・"))}</small>` : ""}</span><select data-role="${esc(r.role)}">
        <option value="">上と同じ</option>
        ${CATALOG.models.map((m) => { const why = whyNot(m, [r]); return `<option value="${esc(m.id)}"${(choice.roles || {})[r.role] === m.id ? " selected" : ""}${why ? " disabled" : ""}>${esc(m.title)}${m.external ? "（外部）" : ""}${why ? `（選べない: ${esc(why)}）` : ""}</option>`; }).join("")}
      </select></label>`).join("")}
    <h4>最終的な割り当て</h4>
    <table class="usage assign"><thead><tr><th>役割</th><th>使うモデル</th><th>メモ</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${esc(r.title)}</td><td>${esc(mTitle(r.id))}${modelOf(r.id)?.external ? `<span class="ext">外部</span>` : ""}</td>
        <td>${r.why ? `${esc(mTitle(r.asked))} は使えない（${esc(r.why)}）。代わりにこのモデル` : r.asked ? "選んだモデル" : "窓口の既定"}</td></tr>`).join("")}
    </tbody></table>
    ${external.length ? `<p class="note warn">外部のモデル（${esc([...new Set(external.map((r) => mTitle(r.id)))].join("・"))}）に、依頼文と読んだページの本文が送られます。料金: ${esc([...new Set(external.map((r) => `${mTitle(r.id)} ${priceText(modelOf(r.id))}`))].join(" / "))}</p>` : ""}`;
}
export function wireModelTab(root, choice, onChange) {
  root.querySelectorAll(".opt[data-all]").forEach((b) => b.addEventListener("click", () => { choice.all = b.dataset.all; onChange(); }));
  root.querySelectorAll("select[data-role]").forEach((s) => s.addEventListener("change", () => {
    if (s.value) choice.roles[s.dataset.role] = s.value; else delete choice.roles[s.dataset.role];
    onChange();
  }));
}

// ---- 設定のパネルの「接続と使用量」のモデルの窓口の部分 ----
export function gatewayStatusHtml() {
  if (!modelStatus) return `<p class="ngline"><i class="dot ng"></i>モデルの窓口につながらない（状態と使用量を読めない）。調査は、窓口が戻るまで始められないことがあります。</p>`;
  const st = modelStatus.models || {}, spend = modelStatus.spend || {}, limits = spend.limits || {};
  const models = CATALOG?.models || Object.keys(st).map((id) => ({ id, title: id }));
  const unmeasured = Object.values(st).reduce((n, s) => n + (s.unmeasured || 0), 0);
  return `
    <p class="since">確かめた時刻 ${new Date(statusAt).toLocaleTimeString("ja-JP")} · 接続はモデルごとの一覧 API への応答（推論は確かめていない）</p>
    <table class="usage"><thead><tr><th>モデル</th><th>接続</th><th>呼び出し</th><th>トークン（入力・出力）</th><th>費用（USD）</th></tr></thead><tbody>
      ${models.map((m) => { const s = st[m.id] || {}; return `<tr><td>${esc(m.title)}</td><td>${dot(m.id)}${s.ok ? `${s.seconds ?? "?"} 秒` : esc(s.error || "応答なし")}</td>
        <td class="mono">${fmtNum(s.calls)}${s.errors ? `（失敗 ${fmtNum(s.errors)}）` : ""}${s.refused ? `（上限で断った ${fmtNum(s.refused)}）` : ""}${s.unmeasured ? `（費用を測れなかった ${fmtNum(s.unmeasured)}）` : ""}</td>
        <td class="mono">${fmtNum(s.prompt_tokens)}・${fmtNum(s.completion_tokens)}</td><td class="mono">${isPriced(m) ? fmtUsd(s.cost_usd || 0) : "—"}</td></tr>`; }).join("")}
    </tbody></table>
    <p class="since">呼び出しとトークンは、窓口を立ち上げてからの合計</p>
    <p class="spend">今日（${esc(spend.day || "")}）の推計費用 <b class="mono">${fmtUsd(spend.today_usd || 0)}</b>
      ${unmeasured || spend.unmeasured_today ? `<br><span class="warn">費用を測れなかった呼び出し ${fmtNum(spend.unmeasured_today || unmeasured)} 件（この分は費用に含まない）</span>` : ""}</p>
    <p class="spend">${limits.daily_usd || limits.run_usd
      ? `上限: ${limits.daily_usd ? `1 日 ${fmtUsd(limits.daily_usd)}` : ""}${limits.daily_usd && limits.run_usd ? "・" : ""}${limits.run_usd ? `1 つの調査 ${fmtUsd(limits.run_usd)}` : ""}。上限到達後に有料呼び出しを停止（計上済みの額で、次の送信の前に判定する）`
      : "費用の上限なし"}</p>`;
}

// ---- 依頼の記録の表示 ----
// 調査で使ったモデル（役割ごと）。値段のあるモデルを使ったら、費用も出す
function modelSummary(models, agentId) {
  if (!CATALOG || !models || !Object.keys(models).length) return "";
  const titles = Object.fromEntries(roleList(agentId).map((r) => [r.role, r.title]));
  const groups = {};
  for (const [role, id] of Object.entries(models)) (groups[id] = groups[id] || []).push(titles[role] || role);
  return Object.entries(groups).map(([id, rs]) => `${Object.keys(groups).length > 1 ? rs.join("・") + " " : ""}${mTitle(id)}${modelOf(id)?.external ? "（外部）" : ""}`).join(" / ");
}
// 戻り値: 費用を出したか（値段のあるモデルを使った）
export function modelMeta(models, agentId = ctx.runAgent) {
  const text = modelSummary(models, agentId);
  return text ? { text: `役割に割り当てたモデル: ${text}`, priced: Object.values(models).some((m) => isPriced(modelOf(m))) } : null;
}
// 実際に応答したモデル（窓口が調査ごと・役割ごとに数えたもの: [{role, model, upstream, calls, failed}]）。
// 役割の割り当てではなく、答えたモデルを出す。同じモデルの役割はまとめる。上流が名乗った名前は title に出す
export function answeredMeta(answered, agentId = ctx.runAgent) {
  const rows = (answered || []).filter((a) => a.calls || a.failed);
  if (!rows.length) return null;
  const titles = Object.fromEntries(roleList(agentId).map((r) => [r.role, r.title]));
  const groups = new Map();
  for (const a of rows) {
    const g = groups.get(a.model) || { roles: [], calls: 0, failed: 0, upstream: new Set() };
    if (!g.roles.includes(titles[a.role] || a.role || "役割なし")) g.roles.push(titles[a.role] || a.role || "役割なし");
    g.calls += a.calls || 0; g.failed += a.failed || 0; (a.upstream || []).forEach((u) => g.upstream.add(u));
    groups.set(a.model, g);
  }
  const many = groups.size > 1;
  const part = (id, g) => `${many ? g.roles.join("・") + " " : ""}${mTitle(id)}${modelOf(id)?.external ? "（外部）" : ""} ${g.calls} 回${g.failed ? `（失敗 ${g.failed}）` : ""}`;
  return {
    text: `応答したモデル: ${[...groups].map(([id, g]) => part(id, g)).join(" / ")}`,
    title: [...groups].map(([id, g]) => `${g.roles.join("・")}: ${mTitle(id)}（上流の名前 ${[...g.upstream].join("・") || "不明"}）`).join("\n"),
  };
}
// 一覧に出す短いモデルの名前（窓口の役割の既定と違うときだけ）
export function runModelTag(models) {
  if (!CATALOG || !models) return "";
  const diff = Object.entries(models).filter(([role, id]) => id !== roleDefault(role));
  if (!diff.length) return "";
  const ids = [...new Set(Object.values(models))];
  return ids.length === 1 ? mTitle(ids[0]) : "役割ごとのモデル";
}
