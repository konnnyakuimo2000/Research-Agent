// 詳細の欄（ノードを押すと右から出る）。d = { title, photo, alt, kv: [[名前, HTML]], facts, dropped, list: [HTML], note, events }
import { $, esc } from "../core/util.ts";

interface Fact { text: string; aspect?: string; quote?: string; why?: string }
/** 詳細の欄の中身。kv の値・list・note は呼ぶ側が esc した HTML */
export interface Details {
  title?: string; photo?: string; alt?: string;
  kv?: [string, string | number | null | undefined][];
  facts?: Fact[]; dropped?: Fact[]; list?: string[]; note?: string; events?: unknown[];
}

const closers: (() => void)[] = [];
export const onDetailsClose = (fn: () => void): number => closers.push(fn);

export function showDetails(d: Details): void {
  $("dTitle").textContent = d.title || "";
  let h = "";
  if (d.photo) h += `<img class="big" src="${d.photo}" alt="${esc(d.alt || "")}">`;
  if (d.kv) h += `<dl class="kv">${d.kv.filter(([, v]) => v !== "" && v != null).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
  if (d.facts?.length) h += `<div><strong>原文に引用が見つかった記述 ${d.facts.length} 件</strong><ul>${d.facts.map((f) =>
    `<li>${esc(f.text)} <span style="color:var(--muted)">· ${esc(f.aspect)}</span><div class="q">「${esc(f.quote)}」</div></li>`).join("")}</ul></div>`;
  if (d.dropped?.length) h += `<div><strong>引用が見つからず除いた記述 ${d.dropped.length} 件</strong><ul>${d.dropped.map((f) =>
    `<li>${esc(f.text)}<div class="q">${esc(f.why)}${f.quote ? `：「${esc(f.quote)}」` : ""}</div></li>`).join("")}</ul></div>`;
  if (d.list?.length) h += `<ul>${d.list.map((x) => `<li>${x}</li>`).join("")}</ul>`;
  if (d.note) h += `<p class="q">${d.note}</p>`;
  // 描き手のないイベント（知らない拡張・新しい版）は、中身をそのまま見せる
  if (d.events?.length) h += `<details class="raw"><summary>描き方のないイベント ${d.events.length} 件</summary>${d.events.map((e) =>
    `<pre>${esc(JSON.stringify(e, null, 1))}</pre>`).join("")}</details>`;
  $("dBody").innerHTML = h; $("drawer").classList.add("open");
}
export const closeDetails = () => $("drawer").classList.remove("open");
$("dClose").addEventListener("click", () => { closeDetails(); closers.forEach((f) => f()); });
