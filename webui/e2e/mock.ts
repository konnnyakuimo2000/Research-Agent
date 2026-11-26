// /api の返事を決める（ゲートウェイの代わり）。形は src/types/api.ts と同じ
import type { Page, Route } from "@playwright/test";

export const AGENT = {
  id: "research", name: "Research-Agent", version: "1", description: "", levels: [0, 1], extensions: ["research/v1"],
  capabilities: ["questions", "pause", "cancel"], ok: true, error: null, default: true,
  settings_schema: { type: "object", properties: {
    max_seconds: { type: "integer", title: "時間の上限（秒）", default: 900, minimum: 420, maximum: 3600 },
    max_pages: { type: "integer", title: "読むページの数", default: 12, minimum: 2, maximum: 30 },
    images: { type: "boolean", title: "画像も探す", default: true } } },
  presets: { quick: { title: "すばやく", values: { max_seconds: 420, max_pages: 6 } } },
  model_roles: [{ role: "decide", title: "判断", needs: ["json_schema"] }],
  graph: { nodes: [{ id: "start", label: "依頼", kind: "terminal" }, { id: "decide", label: "判断", kind: "hub" },
    { id: "search", label: "検索", kind: "step" }, { id: "respond", label: "報告", kind: "step" }, { id: "end", label: "終わり", kind: "terminal" }],
    edges: [["start", "decide"], ["decide", "search"], ["search", "decide"], ["decide", "respond"], ["respond", "end"]] },
};

export const DONE_RUN = {
  id: "20261125-134518-f70c", agent: "research", request: "東京タワーの高さ", status: "done", question: "", created: 1795700000,
  events: 12, seconds: 40, models: {}, settings: { images: false }, effective_settings: { max_seconds: 900, max_pages: 12, images: false },
};

const PAGE = "https://ja.wikipedia.org/wiki/東京タワー";
// 終わった調査の記録（研究エージェントの拡張のイベントは「research/v1:種類」）
const EVENTS: [string, Record<string, unknown>][] = [
  ["run.start", { request: "東京タワーの高さ" }],
  ["step.start", { node: "decide", step: 1 }],
  ["research/v1:decision", { action: "search", query: "東京タワー 高さ", llm: { action: "search", query: "東京タワー 高さ", reason: "まず調べる" } }],
  ["step.end", { node: "decide", step: 1, seconds: 1 }],
  ["step.start", { node: "search", step: 2 }],
  ["research/v1:search.start", { query: "東京タワー 高さ", kind: "free" }],
  ["research/v1:search.done", { results: [{ title: "東京タワー - Wikipedia", url: PAGE, host: "ja.wikipedia.org", tier: 2, relevant: true }],
    unresponsive: [], official: [], variants: [] }],
  ["step.end", { node: "search", step: 2, seconds: 2 }],
  ["step.start", { node: "read_pages", step: 3 }],
  ["research/v1:page.planned", { url: PAGE, host: "ja.wikipedia.org", tier: 2 }],
  ["research/v1:page.start", { url: PAGE, host: "ja.wikipedia.org", tier: 2 }],
  ["research/v1:page.fetched", { url: PAGE, ok: true, chars: 1000, title: "東京タワー" }],
  ["research/v1:page.done", { url: PAGE, tier: 2, base_tier: 2, dropped: [],
    facts: [{ text: "高さは 333 m", aspect: "高さ", quote: "333メートル" }] }],
  ["step.end", { node: "read_pages", step: 3, seconds: 4 }],
  ["step.start", { node: "respond", step: 4 }],
  ["research/v1:report", { sources: [{ url: PAGE, title: "東京タワー - Wikipedia", tier: 2 }] }],
  ["step.end", { node: "respond", step: 4, seconds: 3 }],
  ["run.end", { report: "## 答え\n東京タワーの高さは 333 m です [1]。" }],
];
export function sse(runId: string): string {
  const lines = EVENTS.map(([type, data], i) => {
    const env = { v: 1, seq: i + 1, t: i * 2, run_id: runId, task_id: "", type, data };
    return `id: ${env.seq}\ndata: ${JSON.stringify(env)}\n\n`;
  });
  return lines.join("") + "event: end\ndata: {}\n\n";
}

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

/** 画面が呼ぶ /api をすべて受ける。送られた依頼（POST /api/runs の中身）を posted に積む */
export async function mockApi(page: Page, runs: Record<string, unknown>[] = [DONE_RUN]) {
  const posted: any[] = [];
  await page.route("**/api/**", async (route) => {
    const req = route.request(), path = new URL(req.url()).pathname;
    if (path === "/api/agents") return json(route, { agents: [AGENT], default: "research" });
    if (path === "/api/models" || path === "/api/models/status") return json(route, { available: false });
    if (path === "/api/status") return json(route, { agent: "research", ok: true, checks: {} });
    if (path === "/api/runs" && req.method() === "POST") {
      const body = req.postDataJSON(); posted.push(body);
      return json(route, { id: "20261127-120000-new0", agent: body.agent, models: body.models, settings: body.settings });
    }
    if (path === "/api/runs") return json(route, { runs });
    if (path.endsWith("/usage")) return json(route, { answered: [], usd: 0 });
    if (path.endsWith("/events")) {
      const id = decodeURIComponent(path.split("/")[3]);
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(id) });
    }
    if (path.startsWith("/api/favicon")) return route.fulfill({ status: 404 });
    return json(route, { detail: `テストで決めていない: ${path}` }, 404);
  });
  return posted;
}
