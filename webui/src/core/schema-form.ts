// JSON Schema（エージェントの名乗りの settings_schema）から設定の欄の形を作る（docs/telemetry_enhancement_plan.md D-1）。
// 描くのは components/settings/SchemaFields.svelte。
// 扱う形: boolean（チェック）・integer / number（数。minimum・maximum）・string（enum なら選択）・enum の配列（複数のチェック）。
// それ以外の形は欄を作らない（ゲートウェイも受け付けない）
import type { SchemaProp, SettingsSchema, SettingsValues } from "../types/api.ts";

type Kind = "bool" | "num" | "select" | "text" | "multi" | "";

const props = (schema: SettingsSchema | null | undefined): [string, SchemaProp][] => Object.entries(schema?.properties || {});
export const hasFields = (schema: SettingsSchema | null | undefined): boolean => props(schema).some(([, s]) => kindOf(s));
function kindOf(s: SchemaProp): Kind {
  if (s.type === "boolean") return "bool";
  if (s.type === "integer" || s.type === "number") return "num";
  if (s.type === "string") return s.enum ? "select" : "text";
  if (s.type === "array" && s.items?.enum) return "multi";
  return "";
}
export const defaults = (schema: SettingsSchema): SettingsValues => Object.fromEntries(props(schema).filter(([, s]) => kindOf(s)).map(([k, s]) => [k, s.default ?? (s.type === "array" ? [] : s.type === "boolean" ? false : "")]));
const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);

// 値を形に合わせる（範囲の外は範囲に収める。読めない値は既定に戻す）
export function coerce(schema: SettingsSchema, name: string, value: any): any {
  const s = schema.properties![name], def = defaults(schema)[name];
  switch (kindOf(s)) {
    case "bool": return !!value;
    case "num": {
      let v = s.type === "integer" ? Math.round(+value) : +value;
      if (!Number.isFinite(v)) return def;
      if (s.minimum != null) v = Math.max(s.minimum, v);
      if (s.maximum != null) v = Math.min(s.maximum, v);
      return v;
    }
    case "select": return s.enum!.includes(value) ? value : def;
    case "multi": return (Array.isArray(value) ? value : []).filter((v) => s.items!.enum!.includes(v));
    default: return String(value ?? "");
  }
}
// 送る設定: 既定と違うものだけ（エージェントが自分の既定を使えるように）
export function changed(schema: SettingsSchema, values: SettingsValues | null | undefined): SettingsValues {
  const d = defaults(schema);
  return Object.fromEntries(Object.entries(values || {}).filter(([k, v]) => k in d && !same(coerce(schema, k, v), d[k])).map(([k, v]) => [k, coerce(schema, k, v)]));
}

/** 設定の欄 1 つ（components/settings/SchemaFields.svelte が描く） */
export interface Field {
  name: string;
  kind: Exclude<Kind, "">;
  id: string;
  title: string;
  value: any;
  def: any;                // 既定
  isDefault: boolean;
  hint: string;            // 説明 · 既定 · 範囲
  min?: number;
  max?: number;
  step: string;
  options: string[];       // 選択・複数のチェックの候補
}
export function fieldList(schema: SettingsSchema, values: SettingsValues | null | undefined): Field[] {
  const d = defaults(schema);
  return props(schema).filter(([, s]) => kindOf(s)).map(([name, s]) => {
    const value = name in (values || {}) ? coerce(schema, name, values![name]) : d[name];
    const range = s.minimum != null || s.maximum != null ? `${s.minimum ?? ""}〜${s.maximum ?? ""}` : "";
    const shown = (x: any) => (s.type === "boolean" ? (x ? "はい" : "いいえ") : Array.isArray(x) ? x.join("・") || "なし" : x);
    return {
      name, kind: kindOf(s) as Exclude<Kind, "">, id: `set-${name}`, title: s.title || name, value, def: d[name],
      isDefault: same(value, d[name]),
      hint: [s.description, `既定 ${shown(d[name])}`, range && `範囲 ${range}`].filter(Boolean).join(" · "),
      min: s.minimum, max: s.maximum, step: s.type === "integer" ? "1" : "any",
      options: s.enum || s.items?.enum || [],
    };
  });
}
