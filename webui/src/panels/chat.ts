// チャット（0 段）: 依頼・状態の一行・質問・文の続き・報告。どのエージェントでも出す。
// イベントの描き手がここで吹き出しの一覧（messages ストア）を変え、components/chat/ChatLog.svelte が描く
import { writable } from "svelte/store";
import { esc, imageUrl } from "../core/util.ts";
import { ctx, hasFlow } from "../core/ctx.ts";
import { registerAction, registerRenderer } from "../core/events.ts";
import { refreshRuns, sendAnswer, setLive, waitAnswer } from "../stores/state.ts";
import type { UIEvent } from "../types/api.ts";

// 動き（running / paused）・答え待ち・一覧の読み直しはストア（stores/state.ts）に出す。
// app.ts がそれを読んで、ボタンと入力欄を変える。番号の答えのボタンは hooks.sendAnswer を呼ぶ（テストで差し替える）
export const hooks = {
  sendAnswer: (text: string): Promise<void> | void =>
    sendAnswer(text).catch((err) => { chatAgent(`<p>答えを送れませんでした: ${esc(err.message)}</p>`); }),
};

/** 吹き出し 1 つ。u は利用者（文だけ）、a はエージェント（HTML・状態の一行・質問・報告） */
export interface ChatMsg {
  id: number;
  who: "u" | "a";
  text?: string;                 // 利用者の文
  html?: string;                 // エージェントの本文（ここで esc して作った HTML）
  status?: string | null;        // 「調べています」の一行（本文がまだないとき）
  question?: { text: string; opts: { a: string; label: string }[]; readOnly: boolean };
  report?: string;               // 報告の Markdown（下に操作を出す）
  done?: boolean;                // 報告・止めたで終わった吹き出し
  saved?: boolean;               // ゲートウェイが報告と成果物を保存した
}
export const messages = writable<ChatMsg[]>([]);
/** 下まで送る合図（数が変わったら送る。報告は頭から読めるよう、送らない） */
export const scrollRequest = writable(0);
/** 開いている調査の頭（使ったモデル・設定・費用。押すと履歴の詳細） */
export const runMeta = writable<{ models: string; modelsTitle: string; settings: string; cost: string } | null>(null);

let list: ChatMsg[] = [], seq = 0, statusId: number | null = null;
let questions = new Set<string>(), streamText = "", lastReport = "";
const commit = (scroll = true) => { messages.set(list); if (scroll) scrollRequest.update((n) => n + 1); };
const patch = (id: number, p: Partial<ChatMsg>) => { list = list.map((m) => (m.id === id ? { ...m, ...p } : m)); };
const find = (id: number | null) => list.find((m) => m.id === id);

export function resetChat(): void {
  list = []; statusId = null; questions = new Set(); streamText = ""; lastReport = "";
  messages.set(list); runMeta.set(null);
}
// チャットが空のときは、見出しと入力欄を真ん中に置く（会話が始まったら入力欄を下に移す）
export const setEmpty = (on: boolean): void => { document.getElementById("chatpane")?.classList.toggle("empty", on); };
export function chatUser(text: string): void { setEmpty(false); list = [...list, { id: ++seq, who: "u", text }]; commit(); }
export function chatAgent(html: string, extra: Partial<ChatMsg> = {}): number {
  setEmpty(false);
  const m: ChatMsg = { id: ++seq, who: "a", html, ...extra };
  list = [...list, m]; commit();
  return m.id;
}
export function statusBubble(): void { if (statusId === null) statusId = chatAgent("", { status: "調べています" }); }
// 状態の一行（吹き出しの「調べています」の文）。文の続きが出始めた吹き出しには出さない
export function now(text: string): void {
  const m = find(statusId);
  if (m && !m.html) { patch(m.id, { status: text }); commit(false); }
}

// 質問。番号のボタンは、エージェントが「番号で答える選択肢がある」と知らせたとき（choices）だけ出す。
// LLM が書いた質問の番号付きの項目（「1. 松田教授のお名前」）を押すと「1」とだけ答えてしまうため。
// 履歴の再生では、質問と選択肢は読むだけ（押しても答えを送らない）。答えられるのはライブで答えを待っている依頼だけ
function showQuestion(q: string, choices = false): void {
  const opts = choices ? q.split("\n").filter((l) => /^\d+\.\s/.test(l)).map((l) => ({ a: l.match(/^(\d+)/)![1], label: l })) : [];
  dropStatus();
  chatAgent("", { question: { text: q, opts, readOnly: ctx.replay } });
}
// 状態の一行だけの吹き出し（本文がまだない「調べています」）を消す。聞き返し・エラーで作業が止まったとき。
// 前は手放すだけだったので、点が回ったままの「判断 5 · 次の作業を決めている」が報告の後も残っていた（2026-11-28）
function dropStatus(): void {
  const m = find(statusId);
  if (m && !m.html) { list = list.filter((x) => x.id !== m.id); commit(false); }
  statusId = null;
}

// 報告の Markdown（見出し・箇条書き・太字・画像・URL・出典番号）を安全に描く
export function renderReport(md: string): string {
  const inline = (s: string) => esc(s)
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, src) => `<img src="${imageUrl(src.replace(/&amp;/g, "&"))}" alt="${alt}">`)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    // Markdown のリンク [文字](URL)（画像の出典など）
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (m, label, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${label}</a>`)
    // URL の終わり: 空白・括弧（全角も）。過去の報告の「https://…（Tier 1）」から「（Tier」を URL に含めない。
    // 「">」の直後（Markdown のリンクとして描いた後）は、もう一度リンクにしない
    .replace(/(^|[^"=>])(https?:\/\/[^\s<（）()]+)/g, (m, pre, u) => `${pre}<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`)
    .replace(/\[(\d{1,3})\]/g, '<button type="button" class="cite" data-src="$1">$1</button>');
  const out: string[] = []; let inList = false;
  for (const line of md.split("\n")) {
    const l = line.trimEnd();
    if (/^\s*[-*]\s+/.test(l)) { if (!inList) { out.push("<ul>"); inList = true; } out.push(`<li>${inline(l.replace(/^\s*[-*]\s+/, ""))}</li>`); continue; }
    if (inList) { out.push("</ul>"); inList = false; }
    if (/^#{1,4}\s/.test(l)) out.push(`<h4>${inline(l.replace(/^#+\s*/, ""))}</h4>`);
    else if (l.trim()) out.push(`<p>${inline(l)}</p>`);
  }
  if (inList) out.push("</ul>");
  return out.join("");
}
function finish(html: string, report = ""): void {
  const id = statusId ?? chatAgent("");
  patch(id, { status: null, html, report, done: true }); commit(false);   // 報告は頭から読めるよう、送らない
  statusId = null; setLive("");
}

registerAction("report", { label: "報告をコピー", run: async (report: string) => { await navigator.clipboard.writeText(report); return "コピーした"; } });

// ---- 0 段のイベント ----
registerRenderer("run.start", (e: UIEvent) => { chatUser(e.request); statusBubble(); now("始めた"); }, "chat");
registerRenderer("question", (e: UIEvent) => { if (!questions.has(e.question)) { questions.add(e.question); showQuestion(e.question, !!e.choices); } }, "chat");
registerRenderer("answer", (e: UIEvent) => {
  const key = "a:" + e.answer + e.question; if (!questions.has(key)) { questions.add(key); chatUser(e.answer); }
  waitAnswer(false); statusBubble();
}, "chat");
registerRenderer("run.waiting", (e: UIEvent) => {
  if (!questions.has(e.question)) { questions.add(e.question); showQuestion(e.question); }
  if (!ctx.replay) waitAnswer(true);                  // 履歴の再生では、入力欄を答えの形にしない
}, "chat");
registerRenderer("run.end", (e: UIEvent) => {
  lastReport = e.report ?? e.answer ?? "";
  finish(renderReport(lastReport), lastReport); waitAnswer(false); refreshRuns();
}, "chat");
registerRenderer("run.error", (e: UIEvent) => { dropStatus(); chatAgent(`<p>エラーで続けられませんでした: ${esc(e.error)}</p>`); setLive(""); }, "chat");
registerRenderer("run.paused", () => { if (!ctx.replay) setLive("paused"); now("一時停止を受け付けた（区切りで止まる）"); }, "chat");
// 一時停止: 作業の区切りで実際に止まった（run.paused は受け付けただけ）
registerRenderer("run.held", () => { if (!ctx.replay) setLive("paused"); now("作業の区切りで止まった（再開すると続く）"); }, "chat");
// ゲートウェイが報告と成果物を保存した（最後のエージェントの吹き出しの下に小さく出す）
registerRenderer("run.saved", () => {
  const last = [...list].reverse().find((m) => m.who === "a");
  if (last && !last.saved) { patch(last.id, { saved: true }); commit(false); }
  // 一覧は run.end の時点では「報告を保存中」。保存が済んだら読み直して「完了」にする
  if (!ctx.replay) refreshRuns();
}, "chat");
registerRenderer("run.resumed", () => { if (!ctx.replay) setLive("running"); now("再開した"); }, "chat");
registerRenderer("run.cancelled", () => {
  finish((streamText ? renderReport(streamText) : "") + `<p class="q">止めました。</p>`, streamText); waitAnswer(false); refreshRuns();
}, "chat");
// 文の続きをそのまま描く
registerRenderer("message.delta", (e: UIEvent) => {
  statusBubble(); streamText += e.text || "";
  patch(statusId!, { status: null, html: renderReport(streamText) }); commit();
}, "chat");
// 状態の一行: 流れの図があるエージェントでは図に出るので、吹き出しには出さない
registerRenderer("status", (e: UIEvent) => { if (!hasFlow(ctx.runAgent)) now(e.text); }, "chat");
