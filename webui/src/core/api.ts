// ゲートウェイの API（/api/*）。
// - ブラウザ: 画面と同じ出どころの /api を呼ぶ。鍵（AGENT_API_KEY）は ?key= か鍵の欄で受け取り、このブラウザに覚える
// - デスクトップアプリ: つなぐ先（ゲートウェイの住所）を覚え、そこの /api を呼ぶ。鍵は Windows の資格情報マネージャーに
//   つなぐ先ごとに置く（アプリの本体の get_key / set_key）。つなぐ先と鍵は、つなぐ先の欄（components/ConnectDialog）で決める
import { writable } from "svelte/store";
import { DESKTOP, invoke } from "./desktop.ts";

let apiKey = "", base = "";
try { apiKey = new URLSearchParams(location.search).get("key") || localStorage.getItem("agentKey") || ""; if (apiKey) localStorage.setItem("agentKey", apiKey); } catch (e) { /* 保存できなくても動く */ }

const GATEWAY = "gatewayUrl";
/** ゲートウェイの住所の形をそろえる（末尾の / を外す。スキームがなければ http://） */
export function normalizeGateway(url: string): string {
  let u = url.trim().replace(/\/+$/, "").replace(/\/ui$/, "");
  if (u && !/^https?:\/\//i.test(u)) u = "http://" + u;
  return u;
}
/** つなぐ先（デスクトップアプリ）。ブラウザでは空（同じ出どころ） */
export const gatewayBase = (): string => base;
export const apiUrl = (path: string): string => base + path;

/** デスクトップアプリ: 覚えたつなぐ先と、その鍵を読む。画面の部品が API を呼ぶ前に 1 回待つ（app.ts） */
export async function initApi(): Promise<void> {
  if (!DESKTOP) return;
  try { base = normalizeGateway(localStorage.getItem(GATEWAY) || ""); } catch (e) { base = ""; }
  apiKey = base ? (await invoke<string | null>("get_key", { gateway: base }).catch(() => null)) || "" : "";
  if (!base) needConnect.set(true);
}

/** ゲートウェイが鍵を求めた（401。鍵がない・違う）。画面に鍵の欄を出す（デスクトップアプリでは、つなぐ先の欄） */
export const needKey = writable(false);
/** デスクトップアプリで、つなぐ先を決める欄を出す（初めて開いたとき・設定から変えるとき） */
export const needConnect = writable(false);

/** 鍵を覚えて、画面を開き直す（イベントの購読も鍵付きでつなぎ直すため） */
export async function saveKey(key: string): Promise<void> {
  if (DESKTOP) await invoke("set_key", { gateway: base, key: key.trim() });
  else try { localStorage.setItem("agentKey", key.trim()); } catch (e) { /* 覚えられなくても、この画面の間は使う */ }
  const url = new URL(location.href); url.searchParams.delete("key");
  location.replace(url.toString());
}

/** デスクトップアプリ: つなぐ先と鍵を確かめてから覚える。だめなら理由を投げる（ConnectDialog が出す） */
export async function connect(url: string, key: string, fetcher: typeof fetch = fetch): Promise<string> {
  const gw = normalizeGateway(url);
  if (!gw) throw new Error("住所を入れてください");
  let r: Response;
  try { r = await fetcher(gw + "/api/agents", { headers: key.trim() ? { Authorization: "Bearer " + key.trim() } : {} }); }
  catch (e) { throw new Error("つながりません（住所・VPN・研究室の証明書を入れたかを確かめてください）"); }
  if (r.status === 401) throw new Error("鍵が違います");
  if (r.status === 403) throw new Error("ゲートウェイが鍵を設定していません（研究室の入口は AGENT_API_KEY が要ります）");
  if (!r.ok) throw new Error(`ゲートウェイの返事が HTTP ${r.status} でした`);
  if (DESKTOP) await invoke("set_key", { gateway: gw, key: key.trim() });
  try { localStorage.setItem(GATEWAY, gw); } catch (e) { /* 覚えられなくても、この画面の間は使う */ }
  base = gw; apiKey = key.trim();
  return gw;
}

// EventSource と画像はヘッダーを付けられないので、URL に鍵を付ける。デスクトップアプリでは、つなぐ先の住所も付ける
export const withKey = (url: string): string => {
  const full = apiUrl(url);
  return apiKey ? full + (full.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(apiKey) : full;
};

/** 返事の形は呼ぶ側が決める（例: api<AgentsReply>("/api/agents")） */
export async function api<T = any>(path: string, body?: unknown, method?: string): Promise<T> {
  const r = await fetch(apiUrl(path), { method: method || (body ? "POST" : "GET"), headers: { "Content-Type": "application/json", ...(apiKey ? { Authorization: "Bearer " + apiKey } : {}) },
    body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) (DESKTOP ? needConnect : needKey).set(true);
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
