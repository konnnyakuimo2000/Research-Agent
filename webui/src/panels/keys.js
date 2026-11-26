// 設定のパネルの「API キー」: 外部の Web API の鍵を入れる。鍵の形から提供元を判別し、その提供元のモデル一覧で確かめてから、
// 選んだモデルを窓口に足す（modelgw/keys.py）。鍵はモデルの窓口だけが持ち、画面には末尾 4 文字だけが返る。
// 入れた鍵は、足すか「やめる」までこの画面の中だけに持つ（ブラウザには保存しない）
import { esc } from "../core/util.js";
import { api } from "../core/api.js";
import { loadModels } from "./models.js";

const SHOW = 60;                     // 一覧に出すモデルの数（多い提供元は絞り込みで探す）
let list = null, loadError = "", checked = null, pendingKey = "", picks = {}, filter = "", busy = "", message = "", problem = "";

export async function loadKeys(rerender) {
  try { list = (await api("/api/models/keys")).keys || []; loadError = ""; }
  catch (err) { list = []; loadError = err.message; }
  rerender();
}
const capLabel = { vision: "画像を読める", json_schema: "JSON スキーマに沿える" };
const num = (v) => (v == null || v === "" ? "" : String(v));

export function keysTabHtml() {
  const shown = checked ? checked.models.filter((m) => !filter || (m.model + " " + m.title).toLowerCase().includes(filter.toLowerCase())) : [];
  const chosen = Object.keys(picks);
  return `<p class="note">鍵の形から提供元（Anthropic・OpenAI・Gemini・OpenRouter）を判別して確かめます。鍵は窓口だけが持ちます。</p>
    <h4>入れてある鍵</h4>
    ${list === null ? `<p class="note">読み込み中…</p>` : loadError ? `<p class="ngline"><i class="dot ng"></i>${esc(loadError)}</p>`
      : list.length ? `<table class="usage"><thead><tr><th>提供元</th><th>鍵</th><th>足したモデル</th><th></th></tr></thead><tbody>
        ${list.map((k) => `<tr><td>${esc(k.provider_title)}</td><td class="mono">${esc(k.key)}</td><td>${k.models.map((m) => esc(m.title)).join("<br>")}</td>
          <td><button type="button" class="linkbtn" data-remove="${esc(k.id)}">外す</button></td></tr>`).join("")}</tbody></table>`
      : `<p class="note">まだありません。</p>`}
    <h4>鍵を足す</h4>
    <div class="keyrow"><input type="password" id="keyInput" autocomplete="off" spellcheck="false" placeholder="sk-ant-… / sk-… / AIza… / sk-or-…" aria-label="API キー"${checked ? " disabled" : ""}>
      ${checked ? "" : `<button type="button" id="keyCheck"${busy ? " disabled" : ""}>${busy === "check" ? "確かめています…" : "判別して確かめる"}</button>`}</div>
    ${problem ? `<p class="notice warn" role="alert">${esc(problem)}</p>` : ""}
    ${message ? `<p class="notice" role="status">${esc(message)}</p>` : ""}
    ${checked ? `<p class="spend"><b>${esc(checked.provider_title)}</b> の鍵です。足すモデルを選んでください（${checked.models.length} 件）。</p>
      ${checked.models.length > 12 ? `<input type="search" id="keyFilter" placeholder="モデル名で絞り込む" value="${esc(filter)}" aria-label="モデル名で絞り込む">` : ""}
      <div class="keymodels">${shown.slice(0, SHOW).map((m) => { const p = picks[m.model];
        return `<div class="kmodel${p ? " on" : ""}"><label class="kpick"><input type="checkbox" data-pick="${esc(m.model)}"${p ? " checked" : ""}>
          <span>${esc(m.title)}${m.title !== m.model ? ` <small class="mono">${esc(m.model)}</small>` : ""}</span></label>
          ${p ? `<div class="kdetail">
            ${m.capabilities === null ? `<small class="warn">能力を選んでください</small>` : ""}
            <span class="kcaps">${Object.entries(capLabel).map(([c, label]) => `<label><input type="checkbox" data-cap="${c}" data-model="${esc(m.model)}"${p.capabilities.includes(c) ? " checked" : ""}>${label}</label>`).join("")}</span>
            <span class="kprice">100 万トークン 入力 $<input type="number" min="0" step="any" data-price="input_per_mtok" data-model="${esc(m.model)}" value="${esc(num(p.price.input_per_mtok))}" aria-label="${esc(m.title)} の入力の値段">
              出力 $<input type="number" min="0" step="any" data-price="output_per_mtok" data-model="${esc(m.model)}" value="${esc(num(p.price.output_per_mtok))}" aria-label="${esc(m.title)} の出力の値段"></span>
            ${m.price ? "" : `<small>値段を入れてください（無料なら 0）</small>`}
          </div>` : ""}</div>`; }).join("")}
        ${shown.length > SHOW ? `<p class="since">ほかに ${shown.length - SHOW} 件（絞り込んでください）</p>` : ""}</div>
      <div class="fbtns"><button type="button" class="primary" id="keyAdd"${!chosen.length || busy ? " disabled" : ""}>${busy === "add" ? "足しています…" : `選んだ ${chosen.length} 件を足す`}</button>
        <button type="button" id="keyCancel">やめる</button></div>` : ""}`;
}

function reset() { checked = null; pendingKey = ""; picks = {}; filter = ""; busy = ""; }
export function wireKeysTab(root, rerender) {
  root.querySelector("#keyCheck")?.addEventListener("click", async () => {
    const key = root.querySelector("#keyInput").value.trim();
    if (!key) { problem = "鍵を入れてください"; rerender(); return; }
    busy = "check"; problem = ""; message = ""; rerender();
    try { checked = await api("/api/models/keys/check", { key }); pendingKey = key; picks = {}; }
    catch (err) { problem = err.message; checked = null; }
    busy = ""; rerender();
  });
  root.querySelector("#keyFilter")?.addEventListener("input", (ev) => { filter = ev.target.value; rerender(); root.ownerDocument?.getElementById?.("keyFilter")?.focus(); });
  root.querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("change", () => {
    const m = checked.models.find((x) => x.model === b.dataset.pick);
    if (b.checked) picks[m.model] = { title: m.title, capabilities: [...(m.capabilities || [])], price: { ...(m.price || {}) }, context_tokens: m.context_tokens || 0 };
    else delete picks[m.model];
    rerender();
  }));
  root.querySelectorAll("[data-cap]").forEach((b) => b.addEventListener("change", () => {
    const p = picks[b.dataset.model], c = b.dataset.cap;
    p.capabilities = b.checked ? [...new Set([...p.capabilities, c])] : p.capabilities.filter((x) => x !== c);
  }));
  root.querySelectorAll("[data-price]").forEach((b) => b.addEventListener("change", () => {
    const p = picks[b.dataset.model];
    if (b.value === "") delete p.price[b.dataset.price]; else p.price[b.dataset.price] = +b.value;
  }));
  root.querySelector("#keyCancel")?.addEventListener("click", () => { reset(); message = ""; problem = ""; rerender(); });
  root.querySelector("#keyAdd")?.addEventListener("click", async () => {
    const models = Object.entries(picks).map(([model, p]) => ({ model, ...p }));
    const missing = models.filter((m) => m.price.input_per_mtok == null || m.price.output_per_mtok == null);
    if (missing.length) { problem = `値段を入れてください: ${missing.map((m) => m.title).join("・")}（無料なら 0）`; rerender(); return; }
    busy = "add"; problem = ""; rerender();
    try {
      const entry = await api("/api/models/keys", { key: pendingKey, models });
      message = `${entry.provider_title} のモデルを ${entry.models.length} 件足しました`;
      reset(); await loadModels(); await loadKeys(rerender);
    } catch (err) { problem = err.message; busy = ""; rerender(); }
  });
  root.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", async () => {
    const k = list.find((x) => x.id === b.dataset.remove);
    if (!window.confirm(`${k.provider_title} の鍵（${k.key}）とモデル ${k.models.length} 件を外しますか？`)) return;
    try { await api(`/api/models/keys/${encodeURIComponent(k.id)}`, undefined, "DELETE"); message = `${k.provider_title} の鍵を外しました`; problem = ""; }
    catch (err) { problem = err.message; }
    await loadModels(); await loadKeys(rerender);
  }));
}
