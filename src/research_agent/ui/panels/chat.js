// チャット（0 段）: 依頼・状態の一行・質問・文の続き・報告。どのエージェントでも出す
import { $, esc, imageUrl } from "../core/util.js";
import { ctx, hasFlow } from "../core/ctx.js";
import { active, actionsFor, registerAction, registerRenderer } from "../core/events.js";

// 画面の他の部分（入力欄・一覧）とつなぐところ。app.js が入れる
export const hooks = { setComposer() {}, loadRuns() {}, updateSend() {}, setLive() {}, sendAnswer() {} };

let statusEl = null, questions = new Set(), streamText = "", lastReport = "";
export function resetChat() { statusEl = null; questions = new Set(); streamText = ""; lastReport = ""; $("chat").innerHTML = ""; }
// チャットが空のときは、見出しと入力欄を真ん中に置く（会話が始まったら入力欄を下に移す）
export const setEmpty = (on) => $("chatpane").classList.toggle("empty", on);
export function scrollChat() { $("chat").scrollTop = $("chat").scrollHeight; }
export function chatUser(text) { setEmpty(false); const d = document.createElement("div"); d.className = "u"; d.textContent = text; $("chat").appendChild(d); scrollChat(); }
export function chatAgent(html) {
  setEmpty(false);
  const d = document.createElement("div"); d.className = "a"; d.innerHTML = `<div class="avatar">R</div><div class="body">${html}</div>`;
  $("chat").appendChild(d); scrollChat(); return d;
}
export function statusBubble() {
  if (!statusEl) statusEl = chatAgent(`<div class="status"><span class="dots"><i></i><i></i><i></i></span><span class="txt">調べています</span></div>`);
}
// 状態の一行（吹き出しの「調べています」の文）
export function now(text) { const txt = statusEl && statusEl.querySelector(".txt"); if (txt) txt.textContent = text; }

// 質問。番号のボタンは、エージェントが「番号で答える選択肢がある」と知らせたとき（choices）だけ出す。
// LLM が書いた質問の番号付きの項目（「1. 松田教授のお名前」）を押すと「1」とだけ答えてしまうため
function showQuestion(q, choices = false) {
  const lines = q.split("\n"), opts = choices ? lines.filter((l) => /^\d+\.\s/.test(l)) : [];
  // 履歴の再生では、質問と選択肢は読むだけ（押しても答えを送らない）。答えられるのはライブで答えを待っている依頼だけ
  const readOnly = ctx.replay;
  const d = chatAgent(`<div class="question">${esc(q)}${opts.length ? `<div class="opts">${opts.map((o) => `<button type="button" data-a="${esc(o.match(/^(\d+)/)[1])}">${esc(o)}</button>`).join("")}</div>` : ""}</div>`);
  d.querySelectorAll(".opts button").forEach((b) => {
    if (readOnly) { b.disabled = true; b.title = "履歴の再生中は答えられません"; return; }
    b.addEventListener("click", () => hooks.sendAnswer(b.dataset.a));
  });
  statusEl = null;
}

// 報告の Markdown（見出し・箇条書き・太字・画像・URL・出典番号）を安全に描く
export function renderReport(md) {
  const inline = (s) => esc(s)
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, src) => `<img src="${imageUrl(src.replace(/&amp;/g, "&"))}" alt="${alt}">`)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    // Markdown のリンク [文字](URL)（画像の出典など）
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (m, label, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${label}</a>`)
    // URL の終わり: 空白・括弧（全角も）。過去の報告の「https://…（Tier 1）」から「（Tier」を URL に含めない。
    // 「">」の直後（Markdown のリンクとして描いた後）は、もう一度リンクにしない
    .replace(/(^|[^"=>])(https?:\/\/[^\s<（）()]+)/g, (m, pre, u) => `${pre}<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`)
    .replace(/\[(\d{1,3})\]/g, '<button type="button" class="cite" data-src="$1">$1</button>');
  const out = []; let list = false;
  for (const line of md.split("\n")) {
    const l = line.trimEnd();
    if (/^\s*[-*]\s+/.test(l)) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(l.replace(/^\s*[-*]\s+/, ""))}</li>`); continue; }
    if (list) { out.push("</ul>"); list = false; }
    if (/^#{1,4}\s/.test(l)) out.push(`<h4>${inline(l.replace(/^#+\s*/, ""))}</h4>`);
    else if (l.trim()) out.push(`<p>${inline(l)}</p>`);
  }
  if (list) out.push("</ul>");
  return out.join("");
}
// 出典番号: 押したら、拡張の部品（cite）がその出典を図の中で見せる。拡張がなければ押せない
function wireCites(el) {
  const ext = active().find((x) => x.cite);
  el.querySelectorAll(".cite").forEach((b) => {
    if (!ext) { b.disabled = true; return; }
    b.addEventListener("click", () => ext.cite(+b.dataset.src));
  });
}
// 報告の下の操作（registerAction("report", …)。拡張も足せる）
function wireActions(el, report) {
  const list = actionsFor("report").filter((a) => !a.when || a.when(report));
  if (!list.length || !report) return;
  const row = document.createElement("div"); row.className = "ractions";
  list.forEach((a) => {
    const b = document.createElement("button"); b.type = "button"; b.textContent = a.label;
    b.addEventListener("click", async () => { try { const done = await a.run(report, ctx); if (done) { b.textContent = done; setTimeout(() => { b.textContent = a.label; }, 1600); } } catch (err) { b.textContent = "できませんでした"; } });
    row.appendChild(b);
  });
  el.querySelector(".body").appendChild(row);
}
function finish(html, report = "") {
  const el = statusEl || chatAgent("");
  el.querySelector(".body").innerHTML = html; wireCites(el); wireActions(el, report);   // 報告は頭から読めるよう、送らない
  statusEl = null; hooks.setLive("");
  return el;
}

registerAction("report", { label: "報告をコピー", run: async (report) => { await navigator.clipboard.writeText(report); return "コピーした"; } });

// ---- 0 段のイベント ----
registerRenderer("run.start", (e) => { chatUser(e.request); statusBubble(); now("始めた"); }, "chat");
registerRenderer("question", (e) => { if (!questions.has(e.question)) { questions.add(e.question); showQuestion(e.question, !!e.choices); } }, "chat");
registerRenderer("answer", (e) => {
  const key = "a:" + e.answer + e.question; if (!questions.has(key)) { questions.add(key); chatUser(e.answer); }
  hooks.setComposer(false); statusBubble();
}, "chat");
registerRenderer("run.waiting", (e) => {
  if (!questions.has(e.question)) { questions.add(e.question); showQuestion(e.question); }
  if (!ctx.replay) hooks.setComposer(true);                  // 履歴の再生では、入力欄を答えの形にしない
}, "chat");
registerRenderer("run.end", (e) => {
  lastReport = e.report ?? e.answer ?? "";
  finish(renderReport(lastReport), lastReport); hooks.setComposer(false); hooks.loadRuns();
}, "chat");
registerRenderer("run.error", (e) => { chatAgent(`<p>エラーで続けられませんでした: ${esc(e.error)}</p>`); statusEl = null; hooks.setLive(""); hooks.updateSend(); }, "chat");
registerRenderer("run.paused", () => { if (!ctx.replay) hooks.setLive("paused"); now("一時停止を受け付けた（区切りで止まる）"); hooks.updateSend(); }, "chat");
registerRenderer("run.resumed", () => { if (!ctx.replay) hooks.setLive("running"); now("再開した"); hooks.updateSend(); }, "chat");
registerRenderer("run.cancelled", () => {
  finish((streamText ? renderReport(streamText) : "") + `<p class="q">止めました。</p>`, streamText); hooks.setComposer(false); hooks.loadRuns();
}, "chat");
// 文の続きをそのまま描く
registerRenderer("message.delta", (e) => {
  statusBubble(); streamText += e.text || "";
  statusEl.querySelector(".body").innerHTML = renderReport(streamText); scrollChat();
}, "chat");
// 状態の一行: 流れの図があるエージェントでは図に出るので、吹き出しには出さない
registerRenderer("status", (e) => { if (!hasFlow(ctx.runAgent)) now(e.text); }, "chat");
