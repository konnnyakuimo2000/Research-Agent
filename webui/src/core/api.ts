// ゲートウェイの API（/api/*）。鍵（AGENT_API_KEY）は ?key= か、鍵の欄（components/KeyDialog）で受け取り、このブラウザに覚える
import { writable } from "svelte/store";

let apiKey = "";
try { apiKey = new URLSearchParams(location.search).get("key") || localStorage.getItem("agentKey") || ""; if (apiKey) localStorage.setItem("agentKey", apiKey); } catch (e) { /* 保存できなくても動く */ }

/** ゲートウェイが鍵を求めた（401。鍵がない・違う）。画面に鍵の欄を出す */
export const needKey = writable(false);
/** 鍵を覚えて、画面を開き直す（イベントの購読も鍵付きでつなぎ直すため） */
export function saveKey(key: string): void {
  try { localStorage.setItem("agentKey", key.trim()); } catch (e) { /* 覚えられなくても、この画面の間は使う */ }
  const url = new URL(location.href); url.searchParams.delete("key");
  location.replace(url.toString());
}

// EventSource と画像はヘッダーを付けられないので、URL に鍵を付ける
export const withKey = (url: string): string => apiKey ? url + (url.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(apiKey) : url;

/** 返事の形は呼ぶ側が決める（例: api<AgentsReply>("/api/agents")） */
export async function api<T = any>(path: string, body?: unknown, method?: string): Promise<T> {
  const r = await fetch(path, { method: method || (body ? "POST" : "GET"), headers: { "Content-Type": "application/json", ...(apiKey ? { Authorization: "Bearer " + apiKey } : {}) },
    body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) needKey.set(true);
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    let detail = "";
    try { detail = JSON.parse(text).detail || ""; } catch (e) { detail = text.slice(0, 200); }   // 研究室の入口の断り（403）は文
    throw new Error(detail || `HTTP ${r.status}`);
  }
  return r.json();
}

// 覚えておく値（ブラウザが保存を断っても動く）
export const store = {
  get<T = any>(key: string, fallback: T | null = null): T | null {
    try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
  },
  set(key: string, value: unknown): void { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* 覚えられなくても動く */ } },
};
