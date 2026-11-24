// JSON Schema（エージェントの名乗りの settings_schema）から設定の欄を作る（docs/telemetry_enhancement_plan.md D-1）。
// 扱う形: boolean（チェック）・integer / number（数。minimum・maximum）・string（enum なら選択）・enum の配列（複数のチェック）。
// それ以外の形は欄を作らない（ゲートウェイも受け付けない）
import { esc } from "./util.js";

const props = (schema) => Object.entries(schema?.properties || {});
export const hasFields = (schema) => props(schema).some(([, s]) => kindOf(s));
function kindOf(s) {
  if (s.type === "boolean") return "bool";
  if (s.type === "integer" || s.type === "number") return "num";
  if (s.type === "string") return s.enum ? "select" : "text";
  if (s.type === "array" && s.items?.enum) return "multi";
  return "";
}
export const defaults = (schema) => Object.fromEntries(props(schema).filter(([, s]) => kindOf(s)).map(([k, s]) => [k, s.default ?? (s.type === "array" ? [] : s.type === "boolean" ? false : "")]));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// 値を形に合わせる（範囲の外は範囲に収める。読めない値は既定に戻す）
export function coerce(schema, name, value) {
  const s = schema.properties[name], def = defaults(schema)[name];
  switch (kindOf(s)) {
    case "bool": return !!value;
    case "num": {
      let v = s.type === "integer" ? Math.round(+value) : +value;
      if (!Number.isFinite(v)) return def;
      if (s.minimum != null) v = Math.max(s.minimum, v);
      if (s.maximum != null) v = Math.min(s.maximum, v);
      return v;
    }
    case "select": return s.enum.includes(value) ? value : def;
    case "multi": return (Array.isArray(value) ? value : []).filter((v) => s.items.enum.includes(v));
    default: return String(value ?? "");
  }
}
// 送る設定: 既定と違うものだけ（エージェントが自分の既定を使えるように）
export function changed(schema, values) {
  const d = defaults(schema);
  return Object.fromEntries(Object.entries(values || {}).filter(([k, v]) => k in d && !same(coerce(schema, k, v), d[k])).map(([k, v]) => [k, coerce(schema, k, v)]));
}

export function fieldsHtml(schema, values) {
  const d = defaults(schema);
  return props(schema).filter(([, s]) => kindOf(s)).map(([name, s]) => {
    const v = name in (values || {}) ? coerce(schema, name, values[name]) : d[name];
    const title = esc(s.title || name), id = `set-${esc(name)}`;
    const range = s.minimum != null || s.maximum != null ? `${s.minimum ?? ""}〜${s.maximum ?? ""}` : "";
    const hint = [s.description, range && `範囲 ${range}`].filter(Boolean).map(esc).join(" · ");
    let input;
    switch (kindOf(s)) {
      case "bool": input = `<input type="checkbox" id="${id}" data-name="${esc(name)}"${v ? " checked" : ""}>`; break;
      case "num": input = `<input type="number" id="${id}" data-name="${esc(name)}" value="${esc(v)}"${s.minimum != null ? ` min="${s.minimum}"` : ""}${s.maximum != null ? ` max="${s.maximum}"` : ""} step="${s.type === "integer" ? 1 : "any"}">`; break;
      case "select": input = `<select id="${id}" data-name="${esc(name)}">${s.enum.map((o) => `<option${o === v ? " selected" : ""}>${esc(o)}</option>`).join("")}</select>`; break;
      case "multi": input = `<span class="multi" data-name="${esc(name)}">${s.items.enum.map((o) => `<label><input type="checkbox" value="${esc(o)}"${v.includes(o) ? " checked" : ""}>${esc(o)}</label>`).join("")}</span>`; break;
      default: input = `<input type="text" id="${id}" data-name="${esc(name)}" value="${esc(v)}">`;
    }
    const isDefault = same(v, d[name]);
    return `<div class="frow${kindOf(s) === "bool" ? " bool" : ""}"><label for="${id}">${title}${isDefault ? "" : `<i class="chg" title="既定（${esc(JSON.stringify(d[name]))}）から変えた"></i>`}</label>${input}${hint ? `<small>${hint}</small>` : ""}</div>`;
  }).join("");
}
// 欄から値を読む
export function readFields(root, schema) {
  const out = {};
  root.querySelectorAll("[data-name]").forEach((el) => {
    const name = el.dataset.name;
    if (el.classList.contains("multi")) out[name] = [...el.querySelectorAll("input:checked")].map((i) => i.value);
    else if (el.type === "checkbox") out[name] = el.checked;
    else out[name] = coerce(schema, name, el.value);
  });
  return out;
}
