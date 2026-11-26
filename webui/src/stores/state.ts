// 画面の部品が共有する状態（Svelte のストア）。部品は読む（subscribe）だけで、変えるのはここの関数か set/update。
// 前は、設定・モデル・一覧が変わるたびに app.js（今の app.ts）へ「描き直して」と知らせる手配線（hooks.*）だった。
//
// - agents      … エージェントの名乗り（/api/agents）。ctx.AGENTS にも写す（図の部品は ctx を読む）
// - catalog     … モデルの窓口の一覧（/api/models。使わない構成・つながらないときは null）
// - agentStatus … 次の依頼のエージェントの接続（/api/status）。modelStatus はモデルの窓口の状態と使用量
// - prefs       … 次の依頼の設定。saved（このブラウザの既定値）と oneTime（この依頼だけ）。next は効く方
// - runs        … 履歴の一覧（/api/runs）。openRunId は開いている依頼、runMenu は行の「…」のメニュー
// - live        … 開いている調査の動き（running / paused）と、答えを待っている依頼
//
// 図（流れの図・木）は毎フレーム描くので、ストアにしない（ctx と描き手のまま）
import { derived, writable } from "svelte/store";
import { api } from "../core/api.ts";
import { ctx } from "../core/ctx.ts";
import type { AgentInfo, AgentStatus, Catalog, RequestPrefs, RunSummary } from "../types/api.ts";

export const agents = writable<Record<string, AgentInfo>>({});
agents.subscribe((v) => { ctx.AGENTS = v; });

export const catalog = writable<Catalog | null>(null);
export const agentStatus = writable<{ data: AgentStatus | null; at: number }>({ data: null, at: 0 });
/** モデルの窓口の状態と使用量（/api/models/status。data = null: つながらない・使っていない） */
export const modelStatus = writable<{ data: any; at: number }>({ data: null, at: 0 });

export const prefs = writable<{ saved: RequestPrefs | null; oneTime: RequestPrefs | null }>({ saved: null, oneTime: null });
export const next = derived(prefs, (p) => p.oneTime || p.saved);
/** 次の依頼のエージェント（同じ値では知らせない） */
export const nextAgent = derived(next, (c) => c?.agent || "");
nextAgent.subscribe((a) => { if (a) ctx.agentId = a; });

export const runs = writable<{ list: RunSummary[]; error: string; loaded: boolean }>({ list: [], error: "", loaded: false });
export const runsById = derived(runs, (r) => Object.fromEntries(r.list.map((x) => [x.id, x])) as Record<string, RunSummary>);

/** 開いている依頼（履歴の一覧で印を付ける。ctx.runId と同じ） */
export const openRunId = writable<string | null>(null);
/** 履歴の行の「…」で開いたメニュー（どの依頼の、どのボタンから） */
export const runMenu = writable<{ run: RunSummary; anchor: HTMLElement } | null>(null);

export const live = writable<{ state: "" | "running" | "paused"; waitingRun: string | null }>({ state: "", waitingRun: null });
export const setLive = (state: "" | "running" | "paused"): void => live.update((l) => ({ ...l, state }));
/** 入力欄を答えの形にする（開いている依頼の答えを待つ）・戻す */
export const waitAnswer = (on: boolean): void => live.update((l) => ({ ...l, waitingRun: on ? ctx.runId : null }));

export async function refreshRuns(): Promise<void> {
  try {
    const { runs: list } = await api<{ runs: RunSummary[] }>("/api/runs");
    runs.set({ list, error: "", loaded: true });
  } catch (err) { runs.update((r) => ({ ...r, error: (err as Error).message, loaded: true })); }
}

/** 答えを待っている依頼に答えを送る。送れなければ例外（呼んだ側がチャットに出す） */
export async function sendAnswer(text: string): Promise<void> {
  let waiting: string | null = null;
  live.subscribe((l) => { waiting = l.waitingRun; })();
  if (!waiting || !text) return;
  await api(`/api/runs/${waiting}/answer`, { answer: text });
  waitAnswer(false);
}
