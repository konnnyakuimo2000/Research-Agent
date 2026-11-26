// イベントの受け取りと、描き手の登録（docs/telemetry_enhancement_plan.md 8.1 の差し込み口）
//
// - 共通の画面は、契約の 0・1 段のイベントを描く（registerRenderer で登録した描き手）
// - 2 段の拡張（research/v1 など）の部品は extensions/<名前>/index.ts にあり、registerExtension で自分を登録する。
//   画面は、依頼のエージェントが名乗った extensions のものだけを読み込む
// - 拡張の描き手は、同じ種類のイベントの共通の描き手に代わる（木）か、共通の描き手に足される（チャット）
// - 知らない種類のイベントでも止まらない（木では、そのときの作業の詳細に中身を出すだけ）
import { store } from "./api.ts";
import { ctx } from "./ctx.ts";
import type { AgentInfo, Envelope, UIEvent } from "../types/api.ts";

export type Renderer = (e: UIEvent) => void;
export type RendererMap = Record<string, Renderer>;
/** 画面の部分（chat・tree）ごとの描き手 */
export type ViewRenderers = Record<string, RendererMap>;
export interface Action { label: string; [key: string]: any }

/** 拡張の部品（extensions/<名前>/index.ts が registerExtension で渡す） */
export interface ExtensionDef {
  renderers?: ViewRenderers;
  layouts?: Record<string, any>;           // 図の並べ方（flow → クラス）
  actions?: Record<string, Action[]>;
  observe?: (e: UIEvent) => void;          // 描く前に集計を進める
  reset?: () => void;
  cite?: (n: number) => void;              // 報告の出典番号を押したとき
  [key: string]: any;
}
export interface Extension extends ExtensionDef {
  renderers: ViewRenderers;
  layouts: Record<string, any>;
  actions: Record<string, Action[]>;
  name: string;
}
export interface NodeKindDef { [key: string]: any }

const extensions: Record<string, Extension> = {};                 // 名前 → 拡張の部品
const loading: Record<string, Promise<boolean>> = {};             // 名前 → 読み込み
const renderers: ViewRenderers = { chat: {}, tree: {} };          // 共通の描き手（画面の部分 → イベントの種類 → 関数）
const nodeKinds: Record<string, NodeKindDef> = {};                // 流れの図のノードの種類 → 見た目と詳細
const actions: Record<string, Action[]> = {};                     // 報告などに付ける操作

export function registerExtension(name: string, def: ExtensionDef): void {
  extensions[name] = { name, renderers: {}, layouts: {}, actions: {}, ...def } as Extension;
}
export function registerRenderer(type: string, fn: Renderer, view = "tree"): void { (renderers[view] ||= {})[type] = fn; }
export function registerNodeKind(kind: string, def: NodeKindDef): void { nodeKinds[kind] = def; }
export const nodeKind = (kind: string): NodeKindDef => nodeKinds[kind] || nodeKinds.step;
export function registerAction(where: string, action: Action): void { (actions[where] ||= []).push(action); }
export const actionsFor = (where: string): Action[] => [...(actions[where] || []), ...active().flatMap((x) => x.actions[where] || [])];

// 拡張の名前（research/v1）→ 置き場（extensions/research-v1/）
export const slug = (name: string): string => name.replace(/[^A-Za-z0-9]+/g, "-");
export function loadExtensions(names: string[]): Promise<boolean[]> {
  return Promise.all(names.map((n) => (loading[n] ||= import(`../extensions/${slug(n)}/index.ts`)
    .then(() => !!extensions[n])
    .catch((err) => { console.warn(`拡張 ${n} の部品を読み込めません`, err); return false; }))));
}

// 依頼のエージェントが名乗った拡張。エージェントにつながらないとき（記録の再生など）は、前に名乗ったものを使う
export function runExtensions(id: string = ctx.runAgent): string[] {
  const m = ctx.AGENTS[id];
  if (m && m.ok !== false && Array.isArray(m.extensions)) return m.extensions;
  return store.get<string[]>("ext:" + id, m?.extensions || []) || [];
}
export function rememberExtensions(agents: AgentInfo[]): void {
  agents.filter((a) => a.ok).forEach((a) => store.set("ext:" + a.id, a.extensions || []));
}
// 開いている依頼で使う拡張の部品（読み込めたものだけ）
export const active = (): Extension[] => runExtensions().map((n) => extensions[n]).filter(Boolean);
export const layoutFor = (view: string): any => active().find((x) => x.layouts[view])?.layouts[view] || null;

// 契約の封筒（{v, seq, t, type, data}）を、画面の中の平らな形にする。使っている拡張のイベントは拡張の名前を外す。
// 知らない拡張のイベントは名前がそのまま残る（描き手がなければ、詳細に中身を出すだけ）
export function norm(m: Envelope | UIEvent): UIEvent {
  if (!m.v) return m as UIEvent;
  let type = m.type || ""; const i = type.indexOf(":");
  if (i > 0 && runExtensions().includes(type.slice(0, i))) type = type.slice(i + 1);
  return { ...(m.data || {}), seq: m.seq, t: m.t, type, task_id: m.task_id };
}

// イベントを画面の各部分へ配る
export function dispatch(e: UIEvent): void {
  const exts = active();
  exts.forEach((x) => x.observe?.(e));         // 拡張の集計を先に進める（流れの図・木・詳細が同じ数を読む）
  renderers.chat[e.type]?.(e);
  exts.forEach((x) => x.renderers.chat?.[e.type]?.(e));
  const own = exts.find((x) => x.renderers.tree?.[e.type]);
  const fn = own ? own.renderers.tree[e.type] : renderers.tree[e.type] || renderers.tree["*"];
  fn?.(e);
}
// 依頼を開き直したとき、拡張の部品にも状態を捨てさせる
export const resetExtensions = (): void => active().forEach((x) => x.reset?.());
