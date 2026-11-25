// イベントの受け取りと、描き手の登録（docs/telemetry_enhancement_plan.md 8.1 の差し込み口）
//
// - 共通の画面は、契約の 0・1 段のイベントを描く（registerRenderer で登録した描き手）
// - 2 段の拡張（research/v1 など）の部品は extensions/<名前>/index.js にあり、registerExtension で自分を登録する。
//   画面は、依頼のエージェントが名乗った extensions のものだけを読み込む
// - 拡張の描き手は、同じ種類のイベントの共通の描き手に代わる（木）か、共通の描き手に足される（チャット）
// - 知らない種類のイベントでも止まらない（木では、そのときの作業の詳細に中身を出すだけ）
import { store } from "./api.js";
import { ctx } from "./ctx.js";

const extensions = {};                       // 名前 → 拡張の部品
const loading = {};                          // 名前 → 読み込み（Promise）
const renderers = { chat: {}, tree: {} };    // 共通の描き手（画面の部分 → イベントの種類 → 関数）
const nodeKinds = {};                        // 流れの図のノードの種類 → 見た目と詳細
const actions = {};                          // 報告などに付ける操作

export function registerExtension(name, def) {
  extensions[name] = { name, renderers: {}, layouts: {}, actions: {}, ...def };
}
export function registerRenderer(type, fn, view = "tree") { (renderers[view] ||= {})[type] = fn; }
export function registerNodeKind(kind, def) { nodeKinds[kind] = def; }
export const nodeKind = (kind) => nodeKinds[kind] || nodeKinds.step;
export function registerAction(where, action) { (actions[where] ||= []).push(action); }
export const actionsFor = (where) => [...(actions[where] || []), ...active().flatMap((x) => x.actions[where] || [])];

// 拡張の名前（research/v1）→ 置き場（extensions/research-v1/）
export const slug = (name) => name.replace(/[^A-Za-z0-9]+/g, "-");
export function loadExtensions(names) {
  return Promise.all(names.map((n) => (loading[n] ||= import(`../extensions/${slug(n)}/index.js`)
    .then(() => !!extensions[n])
    .catch((err) => { console.warn(`拡張 ${n} の部品を読み込めません`, err); return false; }))));
}

// 依頼のエージェントが名乗った拡張。エージェントにつながらないとき（記録の再生など）は、前に名乗ったものを使う
export function runExtensions(id = ctx.runAgent) {
  const m = ctx.AGENTS[id];
  if (m && m.ok !== false && Array.isArray(m.extensions)) return m.extensions;
  return store.get("ext:" + id, m?.extensions || []);
}
export function rememberExtensions(agents) { agents.filter((a) => a.ok).forEach((a) => store.set("ext:" + a.id, a.extensions || [])); }
// 開いている依頼で使う拡張の部品（読み込めたものだけ）
export const active = () => runExtensions().map((n) => extensions[n]).filter(Boolean);
export const layoutFor = (view) => active().find((x) => x.layouts[view])?.layouts[view] || null;

// 契約の封筒（{v, seq, t, type, data}）を、画面の中の平らな形にする。使っている拡張のイベントは拡張の名前を外す。
// 知らない拡張のイベントは名前がそのまま残る（描き手がなければ、詳細に中身を出すだけ）
export function norm(m) {
  if (!m.v) return m;
  let type = m.type || ""; const i = type.indexOf(":");
  if (i > 0 && runExtensions().includes(type.slice(0, i))) type = type.slice(i + 1);
  return { ...(m.data || {}), seq: m.seq, t: m.t, type, task_id: m.task_id };
}

// イベントを画面の各部分へ配る
export function dispatch(e) {
  const exts = active();
  exts.forEach((x) => x.observe?.(e));         // 拡張の集計を先に進める（流れの図・木・詳細が同じ数を読む）
  renderers.chat[e.type]?.(e);
  exts.forEach((x) => x.renderers.chat?.[e.type]?.(e));
  const own = exts.find((x) => x.renderers.tree?.[e.type]);
  const fn = own ? own.renderers.tree[e.type] : renderers.tree[e.type] || renderers.tree["*"];
  fn?.(e);
}
// 依頼を開き直したとき、拡張の部品にも状態を捨てさせる
export const resetExtensions = () => active().forEach((x) => x.reset?.());
