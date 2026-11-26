// 設定のパネルの状態と操作（.claude/ui-gateway-settings-plan.md）。画面は components/settings/SettingsPanel.svelte。
// 左の一覧の下の「設定」から、同じページの中で開く（広い画面では右側、狭い画面では全面）。
// タブ: 調査・モデル・API キー・接続と使用量・表示。履歴の詳細も同じパネルに出す。
//
// 値の優先順位: この依頼だけの上書き（oneTime）→ このブラウザの既定値（saved。エージェントごと）→ エージェント・窓口の既定。
// 「この依頼に適用」は次に送る 1 件だけに効き、送ったら消える。「このブラウザの既定値に保存」は次の依頼から毎回効く。
// 「標準に戻す」は、名乗りの既定と窓口の既定モデルに戻す（適用か保存で反映）。ブラウザの保存の形は前と同じ
// （settings:<エージェント> = { preset, values }、models:<エージェント> = { all, roles }、agent = エージェント）
import { derived, writable } from "svelte/store";
import { api, store } from "../core/api.ts";
import { ctx } from "../core/ctx.ts";
import { changed, coerce, defaults, hasFields } from "../core/schema-form.ts";
import { choiceLabel, emptyChoice, invalidChoices, loadModelStatus, resolveModels, usesExternal } from "./models.ts";
import { agentStatus, agents, catalog, prefs } from "../stores/state.ts";
import type { AgentStatus, Preset, RequestPrefs, RunSummary, SchemaProp, SettingsSchema, SettingsValues } from "../types/api.ts";

// 「この設定を使う」で新しい調査の画面にする（app.js が入れる）
export const hooks = { startNew() {} };

export type Tab = "research" | "models" | "keys" | "status" | "display" | "run";
export const TABS: [Tab, string][] = [["research", "調査"], ["models", "モデル"], ["keys", "API キー"], ["status", "接続と使用量"], ["display", "表示"]];
export interface PanelState {
  open: boolean;
  tab: Tab;
  run: RunSummary | null;         // 履歴の詳細で見ている依頼
  draft: RequestPrefs | null;     // パネルで編集中の値（適用か保存で効く）
  notice: string;                 // 操作の結果（「標準に戻しました」など）
}
export const panel = writable<PanelState>({ open: false, tab: "research", run: null, draft: null, notice: "" });
let S: PanelState;
panel.subscribe((v) => { S = v; });

export const schemaOf = (agent: string): SettingsSchema => ctx.AGENTS[agent]?.settings_schema || {};
export const presetsOf = (agent: string): Record<string, Preset> => ctx.AGENTS[agent]?.presets || {};
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x));
const sameValues = (a: object, b: object) => JSON.stringify(Object.entries(a).sort()) === JSON.stringify(Object.entries(b).sort());

export function loadSaved(agent: string): RequestPrefs {
  const s = store.get<any>("settings:" + agent, null) || {}, m = store.get<any>("models:" + agent, null) || {};
  return { agent, settings: { preset: s.preset || "", values: { ...(s.values || {}) } }, models: { all: m.all || "", roles: { ...(m.roles || {}) } } };
}
let P: { saved: RequestPrefs | null; oneTime: RequestPrefs | null } = { saved: null, oneTime: null };
let ST: { data: AgentStatus | null; at: number } = { data: null, at: 0 };
prefs.subscribe((v) => { P = v; });
agentStatus.subscribe((v) => { ST = v; });
export const current = (): RequestPrefs => (P.oneTime || P.saved)!;
export const isOneTime = (): boolean => !!P.oneTime;
const setPrefs = (saved: RequestPrefs | null, oneTime: RequestPrefs | null) => prefs.set({ saved, oneTime });

// エージェントの一覧が届いたら呼ぶ（覚えているエージェントがもうなければ既定に）
export function initSettings(defaultAgent: string): void {
  const a = store.get<string>("agent", "") || "";
  setPrefs(loadSaved(ctx.AGENTS[a] ? a : defaultAgent), P.oneTime && ctx.AGENTS[P.oneTime.agent] ? P.oneTime : null);
}
// 送る中身（設定は既定と違うものだけ。モデルは使えるものだけ）
export function requestBody(): { agent: string; settings: SettingsValues; models: Record<string, string> } {
  const c = current(), sc = schemaOf(c.agent);
  return { agent: c.agent, settings: hasFields(sc) ? changed(sc, c.settings.values) : {}, models: resolveModels(c.models, c.agent).send };
}
// 送ったら「この依頼だけ」を消す（次の依頼に持ち越さない）
export function consumeOneTime(): void { if (P.oneTime) setPrefs(P.saved, null); }

// 「標準」「すばやく」など。どのプリセットとも違えば「カスタム」。設定の欄がなければ空
export function presetName(agent: string, values: SettingsValues | null | undefined): string {
  const sc = schemaOf(agent);
  if (!hasFields(sc)) return "";
  const d = defaults(sc), mine = changed(sc, values);
  if (!Object.keys(mine).length) return "標準";
  for (const [id, p] of Object.entries(presetsOf(agent))) if (sameValues(changed(sc, { ...d, ...(p.values || p) }), mine)) return p.title || id;
  return "カスタム";
}
// チャットの上の要約（次の依頼に効くもの）
export function summary() {
  const c = P.oneTime || P.saved; if (!c) return null;
  return { agent: ctx.AGENTS[c.agent]?.name || c.agent, preset: presetName(c.agent, c.settings.values), models: choiceLabel(c.models, c.agent),
    oneTime: P.oneTime ? P.oneTime.label || "この依頼だけ" : "", external: usesExternal(c.models, c.agent), warnings: invalidChoices(c.models, c.agent) };
}

// ---- 接続（エージェントと、エージェントが使うモデル・検索） ----
export async function loadAgentStatus(agent: string = ctx.agentId): Promise<AgentStatus> {
  let data: AgentStatus;
  try { data = await api<AgentStatus>(`/api/status?agent=${encodeURIComponent(agent)}`); }
  catch (err) { data = { agent, ok: false, error: (err as Error).message || "応答なし", checks: {} }; }
  agentStatus.set({ data, at: Date.now() });
  return data;
}
export const refreshStatus = (): Promise<unknown> => Promise.all([loadAgentStatus(), loadModelStatus()]);
// 送信に響く接続の異常（チャットの上に短く出す。なければ空）
export function connectionProblem(): string {
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

// ---- パネルを開く・閉じる ----
let opener: HTMLElement | null = null;
export function openPanel(which: Tab = "research", from: HTMLElement | null = null, run: RunSummary | null = null): void {
  opener = from || (typeof document !== "undefined" ? document.activeElement as HTMLElement : null);
  panel.set({ open: true, tab: run ? "run" : which, run, draft: clone(current()), notice: "" });
}
export function closePanel(): void {
  if (!S.open) return;
  panel.set({ ...S, open: false, run: null, draft: null });
  const back = opener; opener = null;
  if (back && back.isConnected !== false) back.focus?.();        // 開いたボタンに戻す
}
export const panelOpen = (): boolean => S.open;
export const setTab = (tab: Tab): void => panel.set({ ...S, tab, notice: "" });
export const setNotice = (notice: string): void => panel.set({ ...S, notice });
/** 編集中の値を変える（写しを変えて入れ替えるので、部品が変わったことに気づく） */
export function editDraft(fn: (d: RequestPrefs) => void): void {
  if (!S.draft) return;
  const draft = clone(S.draft); fn(draft); panel.set({ ...S, draft });
}
export const draftState = (): RequestPrefs | null => S.draft;

export function chooseAgent(agent: string): void { panel.set({ ...S, draft: loadSaved(agent), notice: "" }); }
export function choosePreset(id: string): void {
  editDraft((d) => {
    const p = presetsOf(d.agent)[id], dv = defaults(schemaOf(d.agent));
    d.settings = { preset: id, values: p ? { ...dv, ...(p.values || p) } : {} };
  });
}
/** 設定の欄を 1 つ変えた（プリセットからは外れる） */
export function setField(name: string, value: unknown): void {
  editDraft((d) => {
    const sc = schemaOf(d.agent);
    d.settings = { preset: "", values: { ...defaults(sc), ...d.settings.values, [name]: coerce(sc, name, value) } };
  });
}

// 適用・保存したら、チャットに戻って上の要約を変える
function closeToChat(): void { closePanel(); (typeof document !== "undefined" ? document.getElementById("input") : null)?.focus(); }
export function applyOnce(): void { const once = { ...clone(S.draft!), label: "この依頼だけ" }; closeToChat(); setPrefs(P.saved, once); }
export function saveDefault(): void {
  const saved = clone(S.draft!);
  store.set("agent", saved.agent); store.set("settings:" + saved.agent, saved.settings); store.set("models:" + saved.agent, saved.models);
  closeToChat(); setPrefs(saved, null);
}
export function resetDraft(): void {
  editDraft((d) => { d.settings = { preset: "", values: {} }; d.models = emptyChoice(); });
  setNotice("標準に戻しました（適用か保存で反映）");
}

// ---- 履歴の詳細（開始時に効いた設定と、役割ごとのモデル。応答したモデルは記録があれば） ----
const LABEL: Record<string, string> = { running: "調べている", paused: "一時停止中", waiting: "答え待ち", finishing: "報告を保存中", done: "完了",
  error: "エラー", stopped: "途中で止まった", cancelled: "止めた" };
export const statusLabel = (s: string): string => LABEL[s] || s;
export function valueText(spec: SchemaProp | undefined, v: unknown): string {
  if (!spec) return Array.isArray(v) ? v.join("・") : String(v);
  return spec.type === "boolean" ? (v ? "はい" : "いいえ") : Array.isArray(v) ? v.join("・") : String(v);
}
/** 開始時に効いた設定の行（項目・値・既定か変更か） */
export function runSettingRows(r: RunSummary): { title: string; value: string; mark: string }[] {
  const props = schemaOf(r.agent).properties || {}, eff = r.effective_settings, changedSet = r.settings || {};
  const names = [...new Set([...Object.keys(props), ...Object.keys(eff || {}), ...Object.keys(changedSet)])];
  return names.map((k) => {
    const has = eff ? k in eff : k in changedSet, v = eff ? eff[k] : changedSet[k];
    return { title: props[k]?.title || k, value: has ? valueText(props[k], v) : "記録なし", mark: k in changedSet ? "変更" : eff ? "既定" : "" };
  });
}
// 過去の設定を、新しい依頼の「この依頼だけ」に読み込む（使えなくなったモデルは、チャットの上に理由と代わりを出す）
export function useRun(r: RunSummary): void {
  const sc = schemaOf(r.agent), props = sc.properties || {};
  const base = r.effective_settings ? r.effective_settings : { ...defaults(sc), ...(r.settings || {}) };
  const values = Object.fromEntries(Object.entries(base).filter(([k]) => k in props).map(([k, v]) => [k, coerce(sc, k, v)]));
  closePanel();
  setPrefs(P.saved, { agent: r.agent, settings: { preset: "", values }, models: { all: "", roles: { ...(r.models || {}) } }, label: "過去の設定" });
  hooks.startNew(); (typeof document !== "undefined" ? document.getElementById("input") : null)?.focus();
}

// 依頼で使った設定（チャットの頭に出す）。エージェントの欄の名前で
export function settingsMeta(agentId: string, values: SettingsValues | null | undefined): string {
  const sc = ctx.AGENTS[agentId]?.settings_schema || {};
  const parts = Object.entries(values || {}).filter(([k]) => sc.properties?.[k]).map(([k, v]) => {
    const s = sc.properties![k], val = coerce(sc, k, v);
    return `${s.title || k}: ${s.type === "boolean" ? (val ? "はい" : "いいえ") : Array.isArray(val) ? val.join("・") : val}`;
  });
  return parts.length ? `設定: ${parts.join("、")}` : "";
}
