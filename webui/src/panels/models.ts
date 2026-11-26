// モデル（モデルの窓口のもの。docs/telemetry_enhancement_plan.md 6.3・M4）。設定のパネルの「モデル」「接続と使用量」
// （components/settings/）と、チャットの頭・履歴の一覧が使う。
// 選び方（choice = { all, roles }）: 「全部まとめて 1 つ」と、役割ごとの上書き（役割の上書きが優先）。何も選ばなければ窓口の役割の既定。
// 使えないモデル（鍵がない）・役割に要る能力がないモデル・一覧から消えたモデルは送らず、理由と代わりに使うモデルを出す
import { api } from "../core/api.ts";
import { ctx } from "../core/ctx.ts";
import { catalog, modelStatus } from "../stores/state.ts";
import type { Catalog, ModelChoice, ModelEntry, ModelRole } from "../types/api.ts";

let CATALOG: Catalog | null = null;
catalog.subscribe((v) => { CATALOG = v; });

export const roleList = (agentId: string): ModelRole[] => ctx.AGENTS[agentId]?.model_roles || [];
export const modelOf = (id: string): ModelEntry | undefined => CATALOG?.models.find((m) => m.id === id);
export const mTitle = (id: string): string => modelOf(id)?.title || id;
export const fmtUsd = (v: number): string => "$" + (v >= 1 ? v.toFixed(2) : v >= 0.01 ? v.toFixed(3) : v.toFixed(4));
export const fmtNum = (v: number | null | undefined): string => (v || 0).toLocaleString("ja-JP");
export const hasCatalog = (): boolean => !!CATALOG;
export const emptyChoice = (): ModelChoice => ({ all: "", roles: {} });
export const isPriced = (m: ModelEntry | undefined | null): boolean => !!m && Object.keys(m.price || {}).length > 0;
export function priceText(m: ModelEntry | undefined): string {
  const p = m?.price || {};
  return p.input_per_mtok != null ? `入力 $${p.input_per_mtok}・出力 $${p.output_per_mtok}／100 万トークン` : "無料";
}

// そのモデルを、その役割（なければこのエージェントのすべての役割）に使えない理由（使えるなら空）
export function whyNot(m: ModelEntry | undefined, roles: ModelRole[]): string {
  if (!m) return "一覧にない";
  if (m.available === false) return m.unavailable || "使えない";
  for (const r of roles) {
    const miss = (r.needs || []).filter((n) => !(m.capabilities || []).includes(n));
    if (miss.length) return `${r.title}に要る ${miss.join("・")} に対応していない`;
  }
  return "";
}
export const roleDefault = (role: string): string => CATALOG?.roles?.[role] || CATALOG?.default || "";

export interface ResolvedRole extends ModelRole { id: string; asked: string; why: string }
// 選び方を、役割ごとの最終的な割り当てにする。send は窓口に送る選び直し（使えないものは送らない）。
// rows は役割ごとの { role, title, needs, id（実際に使う）, asked（選んだもの）, why（選んだものが使えない理由） }
export function resolveModels(choice: Partial<ModelChoice> | null | undefined, agentId: string): { send: Record<string, string>; rows: ResolvedRole[] } {
  const send: Record<string, string> = {}, rows: ResolvedRole[] = [];
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
export function invalidChoices(choice: Partial<ModelChoice> | null | undefined, agentId: string): string[] {
  return resolveModels(choice, agentId).rows.filter((r) => r.why)
    .map((r) => `${r.title}: ${mTitle(r.asked)} は使えない（${r.why}）→ ${mTitle(r.id)} を使う`);
}
export const usesExternal = (choice: Partial<ModelChoice> | null | undefined, agentId: string): boolean =>
  resolveModels(choice, agentId).rows.some((r) => modelOf(r.id)?.external);
// 短い名前（チャットの上の要約）
export function choiceLabel(choice: Partial<ModelChoice> | null | undefined, agentId: string): string {
  const { rows } = resolveModels(choice, agentId);
  if (!rows.length) return "";
  const ids = [...new Set(rows.map((r) => r.id))], anyAsked = rows.some((r) => r.asked && !r.why);
  if (!anyAsked) return "既定のモデル";
  return ids.length === 1 ? mTitle(ids[0]) : "役割ごとのモデル";
}

// 一覧を読み直す（届いたら、ストアを読む部品が描き直す）
export async function loadModels(): Promise<void> {
  try {
    const v = await api<Catalog>("/api/models");
    catalog.set(v.available ? v : null);
  } catch (err) { catalog.set(null); }
}
// モデルの窓口の状態と使用量（data = null: 窓口につながらない・窓口を使っていない）
export async function loadModelStatus(): Promise<any> {
  let data = null;
  try { const v = await api("/api/models/status"); data = v.available ? v : null; } catch (err) { data = null; }
  modelStatus.set({ data, at: Date.now() });
  return data;
}

// ---- 依頼の記録の表示 ----
// 調査で使ったモデル（役割ごと）。値段のあるモデルを使ったら、費用も出す
function modelSummary(models: Record<string, string> | null | undefined, agentId: string): string {
  if (!CATALOG || !models || !Object.keys(models).length) return "";
  const titles = Object.fromEntries(roleList(agentId).map((r) => [r.role, r.title]));
  const groups: Record<string, string[]> = {};
  for (const [role, id] of Object.entries(models)) (groups[id] = groups[id] || []).push(titles[role] || role);
  return Object.entries(groups).map(([id, rs]) => `${Object.keys(groups).length > 1 ? rs.join("・") + " " : ""}${mTitle(id)}${modelOf(id)?.external ? "（外部）" : ""}`).join(" / ");
}
// 戻り値の priced: 費用を出すか（値段のあるモデルを使った）
export function modelMeta(models: Record<string, string> | null | undefined, agentId: string = ctx.runAgent): { text: string; priced: boolean } | null {
  const text = modelSummary(models, agentId);
  return text ? { text: `役割に割り当てたモデル: ${text}`, priced: Object.values(models!).some((m) => isPriced(modelOf(m))) } : null;
}
export interface Answered { role: string; model: string; upstream?: string[]; calls?: number; failed?: number }
// 実際に応答したモデル（窓口が調査ごと・役割ごとに数えたもの: [{role, model, upstream, calls, failed}]）。
// 役割の割り当てではなく、答えたモデルを出す。同じモデルの役割はまとめる。上流が名乗った名前は title に出す
export function answeredMeta(answered: Answered[] | null | undefined, agentId: string = ctx.runAgent): { text: string; title: string } | null {
  const rows = (answered || []).filter((a) => a.calls || a.failed);
  if (!rows.length) return null;
  const titles = Object.fromEntries(roleList(agentId).map((r) => [r.role, r.title]));
  const groups = new Map<string, { roles: string[]; calls: number; failed: number; upstream: Set<string> }>();
  for (const a of rows) {
    const g = groups.get(a.model) || { roles: [], calls: 0, failed: 0, upstream: new Set<string>() };
    if (!g.roles.includes(titles[a.role] || a.role || "役割なし")) g.roles.push(titles[a.role] || a.role || "役割なし");
    g.calls += a.calls || 0; g.failed += a.failed || 0; (a.upstream || []).forEach((u) => g.upstream.add(u));
    groups.set(a.model, g);
  }
  const many = groups.size > 1;
  const part = (id: string, g: { roles: string[]; calls: number; failed: number }) =>
    `${many ? g.roles.join("・") + " " : ""}${mTitle(id)}${modelOf(id)?.external ? "（外部）" : ""} ${g.calls} 回${g.failed ? `（失敗 ${g.failed}）` : ""}`;
  return {
    text: `応答したモデル: ${[...groups].map(([id, g]) => part(id, g)).join(" / ")}`,
    title: [...groups].map(([id, g]) => `${g.roles.join("・")}: ${mTitle(id)}（上流の名前 ${[...g.upstream].join("・") || "不明"}）`).join("\n"),
  };
}
// 一覧に出す短いモデルの名前（窓口の役割の既定と違うときだけ）
export function runModelTag(models: Record<string, string> | null | undefined): string {
  if (!CATALOG || !models) return "";
  const diff = Object.entries(models).filter(([role, id]) => id !== roleDefault(role));
  if (!diff.length) return "";
  const ids = [...new Set(Object.values(models))];
  return ids.length === 1 ? mTitle(ids[0]) : "役割ごとのモデル";
}
