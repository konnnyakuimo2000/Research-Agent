// ゲートウェイの API（/api/*）。鍵（AGENT_API_KEY）は ?key= で受け取り、覚えておく
let apiKey = "";
try { apiKey = new URLSearchParams(location.search).get("key") || localStorage.getItem("agentKey") || ""; if (apiKey) localStorage.setItem("agentKey", apiKey); } catch (e) { /* 保存できなくても動く */ }

// EventSource と画像はヘッダーを付けられないので、URL に鍵を付ける
export const withKey = (url: string): string => apiKey ? url + (url.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(apiKey) : url;

/** 返事の形は呼ぶ側が決める（例: api<AgentsReply>("/api/agents")） */
export async function api<T = any>(path: string, body?: unknown, method?: string): Promise<T> {
  const r = await fetch(path, { method: method || (body ? "POST" : "GET"), headers: { "Content-Type": "application/json", ...(apiKey ? { Authorization: "Bearer " + apiKey } : {}) },
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

// 覚えておく値（ブラウザが保存を断っても動く）
export const store = {
  get<T = any>(key: string, fallback: T | null = null): T | null {
    try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
  },
  set(key: string, value: unknown): void { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* 覚えられなくても動く */ } },
};
