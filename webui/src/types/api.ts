// ゲートウェイ（/api/*）とイベントの契約の形。サーバー側の正本:
// - 封筒とイベントの種類: src/research_agent/agentkit/protocol.py
// - エージェントの一覧: gateway/core.py Gateway.agents()（名乗り = 各エージェントの MANIFEST）
// - 依頼の一覧: gateway/core.py _summary()

// ---- イベント ----

/** 0 段のイベント（どのエージェントも出す） */
export type Level0Type = "run.start" | "run.end" | "run.error" | "run.cancelled" | "message.delta" | "status";
/** 1 段のイベント（作業・段階を名乗ったエージェントが出す） */
export type Level1Type = "task.created" | "task.start" | "task.end" | "step.start" | "step.end" | "question" | "answer"
  | "run.waiting" | "run.paused" | "run.resumed" | "artifact" | "run.held" | "run.saved";
export type CoreType = Level0Type | Level1Type;

/** 契約の封筒。拡張のイベントは type が「拡張の名前:種類」（research/v1:page.done） */
export interface Envelope<D = Record<string, unknown>> {
  v: number;
  seq: number;
  t: number;              // 依頼の開始からの秒数
  run_id: string;
  task_id: string;
  type: CoreType | string;
  data: D;
}

/** 画面の中で扱う平らな形（norm()）。使っている拡張の名前は type から外れ、data の中身が上に出る */
export interface UIEvent {
  type: string;
  seq?: number;
  t?: number;
  task_id?: string;
  [key: string]: any;     // data の中身（種類ごとに違う）
}

// ---- 設定（JSON Schema の一部） ----

export interface SchemaProp {
  type?: "boolean" | "integer" | "number" | "string" | "array" | "object";
  title?: string;
  description?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  enum?: string[];
  items?: { enum?: string[] };
}
export interface SettingsSchema {
  type?: "object";
  properties?: Record<string, SchemaProp>;
}
export type SettingsValues = Record<string, any>;
export interface Preset { title: string; values: SettingsValues }

// ---- エージェント（/api/agents） ----

export interface GraphNode { id: string; label?: string; kind?: string }
export interface ModelRole { role: string; title: string; needs: string[] }
export interface AgentInfo {
  id: string;
  name: string;
  version: string;
  description: string;
  levels: number[];
  extensions: string[];
  capabilities: string[];
  graph: { nodes: GraphNode[]; edges: [string, string][] } | null;
  model_roles: ModelRole[];
  settings_schema: SettingsSchema;
  presets: Record<string, Preset>;
  ok: boolean;            // 名乗りを受け取れた
  error: string | null;
  default: boolean;
}
export interface AgentsReply { agents: AgentInfo[]; default: string }

// ---- 依頼（/api/runs） ----

export type RunStatus = "running" | "paused" | "waiting" | "finishing" | "done" | "error" | "cancelled" | string;
export interface RunSummary {
  id: string;
  agent: string;
  request: string;
  status: RunStatus;
  question: string;
  created: string;
  events: number;
  seconds: number | null;
  models: Record<string, string>;        // 依頼ごとに選び直したモデル（"*" は全部の役割）
  settings: SettingsValues;              // 依頼ごとに変えた設定
  effective_settings: SettingsValues | null;   // 始めたときに効いた設定（前の版の記録にはない）
}
export interface RunsReply { runs: RunSummary[] }
