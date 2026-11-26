// research/v1 の木の描き手: 判断ごとの枝、網羅表（依頼 → 網羅表 → 観点）、検索・ページ・データベース・画像・食い違い。
// ページで確かめた事実は、観点のノードへ飛んで溜まる
import { esc, host, tier, icon, card, toggleBtn, link, sameUrl, imageUrl } from "../../core/util.ts";
import { ctx, labelOf } from "../../core/ctx.ts";
import { tree } from "../../views/tree.ts";
import { now } from "../../panels/chat.ts";
import { RunSummary, WORDS, coverageWord, decisionDiff, imageWord } from "./summary.ts";
import type { CoverageRow } from "./summary.ts";
import type { RendererMap } from "../../core/events.ts";
import type { UIEvent } from "../../types/api.ts";

export const ACTION: Record<string, string> = { search: "検索する", read_pages: "ページを読む", find_images: "画像を探す", check_conflicts: "食い違いを調べる",
  respond: "回答する", ask_user: "聞き返す" };
const WORK: Record<string, string> = { search: "検索", read_pages: "ページを読む", find_images: "画像", check_conflicts: "食い違い", respond: "報告",
  ask_user: "聞き返し", report_infeasible: "調べられないことの報告" };
export const SEARCH_KIND: Record<string, string> = { free: "LLM が選んだ言葉", aspect: "観点を狙った検索", site: "サイトを狙った検索", conflict: "食い違いの確かめ直し" };
export const NEEDS_PERMISSION = /all rights reserved|著作権あり|無断転載/i;
const workName = (node: string) => WORK[node] || labelOf(node);

// この拡張の状態（依頼を開き直すたびに捨てる）
const DB_STATE: Record<string, string> = { ok: "取得", empty: "該当なし", failed: "失敗", unconfigured: "設定なし", disabled: "使わない", skipped: "引かない" };
export interface Source { url: string; title: string; tier: number }
export const S: {
  round: string | null;               // 今の判断のノード
  rounds: number;
  dbNode: Record<string, string>;     // 作業のノード → 分野別データベースのノード
  sources: Source[];                  // 報告の出典（番号の順）
  summary: RunSummary;
} = { round: null, rounds: 0, dbNode: {}, sources: [], summary: new RunSummary() };
export function resetTree(): void { Object.assign(S, { round: null, rounds: 0, dbNode: {}, sources: [], summary: new RunSummary() }); }

const T = tree, N = () => tree.N, info = () => tree.info;
/** 今の作業のノード（なければ空。N()[""] はない） */
const cur = (): string => T.work ?? "";
/** 詳細の欄の項目（なければ作る） */
const kv = (id: string) => (info()[id].kv ||= []);
// ---- 網羅表のノード ----
const aspectId = (name: string) => "a:" + name;
function buildCoverage(e: UIEvent): void {
  T.add("cov", "req", 1, card("網羅表", `観点 ${e.aspects.length} つ`, `${e.subject_type} · ${e.origin}が作成`) + toggleBtn,
    { title: "網羅表", kv: [["対象の種類", esc(e.subject_type)], ["観点を作ったもの", esc(e.origin)]],
      note: "出典条件: Tier 1 の出典か、別サイト 2 件以上（1 件以上が Tier 1〜2）。満たしても食い違うことがある。" },
    "done", true);
  e.aspects.forEach((a: string) => T.add(aspectId(a), "cov", 2,
    `<div class="k"><span>観点</span><span class="mono cnt">0</span></div><div class="t">${esc(a)}</div><div class="s">${coverageWord("未発見")}</div><div class="facts"></div>`,
    { title: `観点: ${a}`, kv: [["判定", coverageWord("未発見")]], facts: [] }, "aspect"));
}
function flyFact(pageId: string, f: { text: string; aspect: string; quote?: string }, t: number, delay: number): void {
  const target = N()[aspectId(f.aspect)];
  const land = () => {
    if (!target) return;
    const sq = document.createElement("i"); if (t !== 1) sq.className = "t2"; sq.title = f.text;
    target.el.querySelector(".facts")!.appendChild(sq);
    target.el.querySelector(".cnt")!.textContent = String(target.el.querySelectorAll(".facts i").length);
    // 状態（確認済みなど）は、ページを読み終えたときにプログラムが集計する（coverage のイベント）
    if (!target.el.classList.contains("ok") && !target.el.classList.contains("weak")) T.setSub(target.id, "判定の集計を待っている");
    (info()[target.id].facts ||= []).push(f);
    T.layout();
  };
  const from = N()[pageId];
  if (ctx.instant || !from || !target || from.hidden || target.hidden) { land(); return; }
  setTimeout(() => {
    const chip = document.createElement("div"); chip.className = "fly"; chip.textContent = f.text.length > 13 ? f.text.slice(0, 13) + "…" : f.text;
    chip.style.transform = `translate(${from.cur.x + 14}px, ${from.cur.y + 10}px)`; T.el.appendChild(chip);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      chip.style.transform = `translate(${target.cur.x + 14}px, ${target.cur.y + 30}px) scale(.55)`; chip.style.opacity = "0";
    }));
    setTimeout(() => { chip.remove(); land(); }, 900);
  }, delay);
}
function coverage(rows: CoverageRow[]): void {
  rows.forEach((r) => {
    const id = aspectId(r.aspect), n = N()[id]; if (!n) return;
    const conflicted = S.summary.conflictAspects().has(r.aspect);
    n.el.classList.toggle("ok", r.status === "確認済み"); n.el.classList.toggle("weak", r.status === "出典不足");
    T.setSub(id, coverageWord(r.status) + (r.independent > 1 ? `（別サイト ${r.independent}）` : "") + (conflicted ? " · 食い違い候補あり" : ""));
    n.el.querySelector(".cnt")!.textContent = String(r.facts);
    info()[id].kv = [["判定", esc(coverageWord(r.status))], ["原文に引用が見つかった記述", `${r.facts} 件`],
      ["別サイトとして数えた出典", `${r.independent}`], ["最も高い Tier", r.best_tier ? `T${r.best_tier}` : ""],
      ["食い違い候補", conflicted ? "あり（食い違いの工程を参照）" : ""]];
  });
  if (N().cov) T.setSub("cov", S.summary.coverageText());
}

// 転送されたページ: 本文の発行元（転送先）をノードに覚えさせ、サイト名・アイコンを切り替える。
// 報告の出典は転送先の URL なので、出典番号から飛ぶときはこれで探す
export const redirectHooks: ((from: string, to: string) => void)[] = [];
function redirected(id: string, e: UIEvent): void {
  const final = e.final_url, n = N()[id];
  if (!final || sameUrl(final, e.url) || n.final) return;
  n.final = final;
  const h = host(final);
  const k = n.el.querySelector(".k span"); if (k) k.textContent = `ページ · ${host(e.url)} から転送`;
  const ico = n.el.querySelector(".ico"); if (ico) ico.outerHTML = icon(h);
  info()[id].title = h; kv(id).push(["転送先", link(final)]);
  redirectHooks.forEach((f) => f(e.url, final));
}
// 出典の URL（転送先のこともある）から、ページのノードを探す
export function pageIdFor(url: string): string | undefined {
  return T.order.find((k) => k.startsWith("p:") && (sameUrl(k.slice(2), url) || (N()[k].final && sameUrl(N()[k].final!, url))));
}
function pageNode(e: UIEvent, state: string): void {
  const id = "p:" + e.url;
  if (N()[id]) { if (state !== "planned") T.setState(id, state); return; }
  const parent = e.db && S.dbNode[cur()] ? S.dbNode[cur()] : cur();
  if (!N()[parent]) return;
  T.add(id, parent, N()[parent].depth + 1, card(e.db ? "記録 · " + e.db : "ページ", e.host, state === "planned" ? "読む順番を待っている" : "取得している", tier(e.tier), icon(e.host)) +
    `<div class="steps"><span></span><span></span><span></span></div>`,
    { title: e.host, kv: [["資料", link(e.url)], ["Tier", `${e.tier}`]],
      note: "引用が本文に見つかった記述だけを残す（数値や否定の意味までは確かめていない）。" }, state);
}

// ---- イベント → 木 ----
export const treeRenderers: RendererMap = {
  "step.start": (e) => {
    if (e.node === "decide") {
      S.rounds++; const id = "d" + e.step; S.round = id;
      T.add(id, "req", 1, `<div class="row"><span class="num">判断 ${S.rounds}</span><span class="choice">${WORDS.processing}</span><span class="st"></span></div>`,
        { title: `判断 ${S.rounds}` }, "round running");
      now(`判断 ${S.rounds} · 次の作業を決めている`);
      return;
    }
    if (e.node === "check_feasibility") {
      T.add("w" + e.step, "req", 1, `<div class="row"><span class="num">判定</span><span class="choice">調べられるか</span><span class="st"></span></div>`,
        { title: "公開情報で調べられるか" }, "round running feas");
      now("依頼が公開情報で調べられるかを判定している");
      return;
    }
    const id = "w" + e.step;
    if (N()[id]) { T.setState(id, "running"); return; }   // 聞き返しの答えで再開した
    T.work = id;
    const name = workName(e.node);
    // 工程の始まりだけが分かる段階は「処理中」。モデルへの依頼は、それを示すイベント（page.llm など）で分かったときだけ出す
    T.add(id, S.round || "req", 2, card(name, name, e.node === "respond" ? WORDS.report : WORDS.processing) + toggleBtn,
      { title: name }, "running");
    if (e.node === "respond") now(WORDS.report);
    if (e.node === "check_conflicts") now("観点ごとに、別々のサイトの記述が食い違っていないか調べている");
  },
  "step.end": (e) => {
    const id = e.node === "decide" ? "d" + e.step : "w" + e.step;
    if (e.error) { T.setState(id, "failed"); T.setSub(id, e.error); }
    else if (!N()[id]?.el.classList.contains("failed")) T.setState(id, "done");
    if (info()[id]) info()[id].kv = [...(info()[id].kv || []), ["この工程の時間", e.seconds == null ? "未計測" : `${e.seconds} 秒`],
      ["うち再試行関連", e.llm_wait ? `${e.llm_wait} 秒` : ""]];
  },
  "status": () => {},     // 状態の一行は、木ではなくノードの中身で分かる
  "feasibility": (e) => {
    const id = T.order.find((k) => N()[k].el.classList.contains("feas"));
    if (!id) return;
    N()[id].el.querySelector(".choice")!.textContent = ({ yes: "調べられる", partial: "一部は調べられない", no: "調べられない", unsure: "調べてみる" } as Record<string, string>)[e.verdict] || e.verdict;
    info()[id].kv = [["判定", esc(e.verdict)], ["理由", esc(e.reason)], ["調べられない部分", esc((e.infeasible_parts || []).join("、"))]];
    T.layout();
  },
  "decision": (e) => {
    const r = S.round;
    if (!r || !N()[r]) return;
    // モデルの提案と採用した行動（流れの図と同じ差分）。実際に始まった作業は、次の工程のノードで分かる
    const d = decisionDiff(e), changed = d.kind === "action" || d.kind === "query";
    N()[r].el.querySelector(".choice")!.innerHTML = changed ? `<span class="was">${esc(d.proposal)}</span> → ${esc(d.adopted)}` : esc(d.adopted);
    if (changed) N()[r].el.querySelector(".row")!.insertAdjacentHTML("beforeend", `<span class="tag">${esc(d.label)}</span>`);
    const plan = e.plan ? `点数 ${e.plan.score}（${Object.entries(e.plan.parts || {}).map(([k, v]) => `${esc(k)} ${(+(v as number)).toFixed(2)}`).join(" × ")}）` : "";
    info()[r].kv = [["モデルの提案", esc(d.proposal)], ["提案の理由（モデルの出力）", esc(d.why)],
      ["採用した行動", esc(d.adopted)], ["変更", esc(d.label)], ["変更理由", esc(d.reason)], ["検索の点数", plan],
      ["他の候補", (e.plan?.alternatives || []).map((a: { query: string; score: number }) => `${esc(a.query)}（${a.score}）`).join("<br>")]];
    info()[r].note = changed ? "プログラムがモデルの提案を変えた。" : "";
    T.layout();
  },
  "search.start": (e) => {
    const w = cur(); if (!N()[w]) return;
    N()[w].el.querySelector(".k span")!.textContent = "検索 · " + (SEARCH_KIND[e.kind] || e.kind);
    T.setTitle(w, e.query); T.setSub(w, "SearXNG と Wikidata に問い合わせている");
    info()[w] = { title: "検索", kv: [["検索の言葉", esc(e.query)], ["種類", esc(SEARCH_KIND[e.kind] || e.kind)], ["狙った観点", esc(e.aspect)], ["狙ったサイト", esc(e.site)]] };
    now(`検索している: ${e.query}`);
  },
  "search.done": (e) => {
    const w = cur(); if (!N()[w]) return;
    const rel = e.results.filter((r: { relevant: boolean }) => r.relevant).length;
    T.setSub(w, e.failed ? e.failed : `結果 ${e.results.length} 件 · 対象を含む ${rel} 件${e.target ? " · 対象: " + e.target.label : ""}`);
    if (e.failed) T.setState(w, "failed");
    kv(w).push(["結果", `${e.results.length} 件（対象の名前を含む ${rel} 件）`], ["応答しなかったエンジン", esc((e.unresponsive || []).join("、"))],
      ["やり直し", e.retried ? "0 件だったので時間をおいて 1 回やり直した" : ""], ["公式サイト", esc((e.official || []).join("、"))],
      ["拾った表記ゆれ", esc((e.variants || []).join("、"))]);
    if (e.candidates?.length) {
      T.add(w + "-cand", w, 3, card("同名の候補 · Wikidata", e.target ? e.target.label : `${e.candidates.length} 件`, e.target ? "対象が決まった" : "決まっていない", "", icon("wikidata.org")) +
        `<div class="chips2">${e.candidates.slice(0, 4).map((c: { label: string }) => `<span>${esc(c.label)}</span>`).join("")}</div>`,
        { title: "同名の候補", list: e.candidates.map((c: { id: string; label: string; description: string }) => `${esc(c.id)} ${esc(c.label)}（${esc(c.description)}）`) }, "done");
    }
    if (e.results.length) {
      const hosts = [...new Set<string>(e.results.map((r: { host: string }) => r.host))].slice(0, 10);
      T.add(w + "-res", w, 3, card("検索結果", `${e.results.length} 件 · 対象を含む ${rel} 件`, "Tier の高い順に読む") + `<div class="icons">${hosts.map(icon).join("")}</div>`,
        { title: `検索結果（${e.results.length} 件）`, list: e.results.map((r: { relevant: boolean; title: string; url: string; tier: number }) => `${r.relevant ? "" : "（対象の名前なし）"}${esc(r.title)} — ${link(r.url)} · T${r.tier}`) }, "done");
    }
    if (e.summary_url && !N().sum) {
      T.add("sum", w, 3, card("概要", host(e.summary_url), "対象の概要（報告の冒頭に使う）", tier(2), icon(host(e.summary_url))),
        { title: "概要", kv: [["URL", link(e.summary_url)]] }, "done");
      N().sum!.url = e.summary_url;
    }
  },
  "aspects": (e) => {
    buildCoverage(e);
    const w = cur();
    if (N()[w]) { T.setSub(w, `${e.subject_type} · 観点 ${e.aspects.length} つ`); info()[w].kv = [["対象の種類", esc(e.subject_type)], ["観点", esc(e.aspects.join("、"))]]; }
    now("対象の種類を判定し、網羅表の観点を作った");
  },
  "databases": (e) => {
    const w = cur(); if (!N()[w]) return;
    const id = w + "-db"; S.dbNode[w] = id;
    T.add(id, w, 3, card("分野別データベース · L3", e.used.join(" → ") || "この種類に使えるものがない", `記録 ${e.records.length} 件（${e.seconds} 秒）${e.failures.length ? " · 失敗あり" : ""}`,
      "", e.records[0] ? icon(host(e.records[0].url)) : "") + toggleBtn,
      { title: "分野別データベース（L3）", kv: [["引いたもの", esc(e.used.join("、"))], ["使えなかったもの", esc(e.failures.join("、"))],
          // データベースごとの結果（旧ログにはない: 記録なし。正常に応答して該当なしは 0 件）
          ["結果", e.statuses ? e.statuses.map((s: { name: string; state: string; records: number; reason?: string }) => esc(`${s.name}: ${DB_STATE[s.state] || s.state}${s.state === "ok" ? ` ${s.records} 件` : s.reason ? `（${s.reason}）` : ""}`)).join("<br>") : "記録なし"]],
        list: e.records.map((r: { database: string; kind?: string; source_id?: string; url: string }) => `${esc(r.database)}${r.kind ? `（${esc(r.kind)}${r.source_id ? ` · ${esc(r.source_id)}` : ""}）` : ""}: ${link(r.url)}`) },
      e.failures.length && !e.records.length ? "failed" : "done");
  },
  "page.planned": (e) => pageNode(e, "planned"),
  "page.start": (e) => { pageNode(e, "running"); T.step("p:" + e.url, 0, "work"); T.setSub("p:" + e.url, e.db ? "DB記録を読込中" : "本文を取得中");
    now(`${e.host} の${e.db ? "DB記録を読み込んでいる" : "本文を取得している"}`); },
  "page.fetched": (e) => {
    const id = "p:" + e.url; if (!N()[id]) return;
    redirected(id, e);
    if (!e.ok) { T.setState(id, "failed"); T.step(id, 0, "ng"); T.setSub(id, `本文を取得できない · ${e.reason}`);
      kv(id).push(["段階", "本文を取得できない"], ["理由", esc(e.reason)]); return; }
    const p = S.summary.pages.get(e.url);
    T.step(id, 0, "on"); if (e.title) T.setTitle(id, e.title);
    T.setSub(id, `${p?.db ? "DB記録を読込済み" : "本文取得済み"} · ${e.chars.toLocaleString()} 文字`);
    info()[id].title = e.title || info()[id].title;
  },
  "page.llm": (e) => {
    const id = "p:" + e.url; if (!N()[id]) return;
    T.setState(id, "llm"); T.step(id, 1, "work"); T.setSub(id, `${WORDS.askingModel}（引用付きで記述を抜き出す）`);
    now(`${host(e.url)} の本文から記述を抜き出すよう、モデルに依頼している`);
  },
  "page.done": (e) => {
    const id = "p:" + e.url; if (!N()[id]) return;
    redirected(id, e);
    const stage = S.summary.pages.get(e.url)?.stage || "";
    if (e.error) { T.setState(id, "failed"); T.step(id, 1, "ng"); T.setSub(id, `${stage} · ${e.error}`);
      kv(id).push(["段階", esc(stage)], ["理由", esc(e.error)]); return; }
    T.setState(id, "done"); T.step(id, 1, "on"); T.step(id, 2, "on");
    const moved = e.tier !== e.base_tier ? ` · T${e.base_tier}→T${e.tier}` : "";
    T.setSub(id, `${WORDS.quoteFound(e.facts.length)}${e.dropped.length ? ` · ${WORDS.quoteDropped(e.dropped.length)}` : ""}${moved}`);
    if (moved) N()[id].el.querySelector(".tier")!.outerHTML = tier(e.tier);
    Object.assign(info()[id], { facts: e.facts, dropped: e.dropped });
    kv(id).push(["Tier の調整", e.tier_reason ? esc(`T${e.base_tier}→T${e.tier}（${e.tier_reason}）`) : ""],
      ["見分ける特徴", esc((e.features || []).join("、"))], ["別の呼び方", esc((e.new_terms || []).join("、"))],
      ["同名の別の対象", esc(e.other_entity)]);
    e.facts.forEach((f: { text: string; aspect: string }, i: number) => flyFact(id, f, e.tier, i * 160));
  },
  "page.reprint": (e) => {
    const id = pageIdFor(e.url), orig = pageIdFor(e.original);
    if (!id) return;
    T.setSub(id, `ほぼ同じ文章: ${host(e.original)}（独立した出典に数えない）`);
    if (orig) N()[id].extra.push(orig);
    kv(id).push(["転載", `${link(e.original)} とほぼ同じ文章`]);
  },
  "coverage": (e) => coverage(e.rows),
  "image.candidates": (e) => {
    const w = cur(); if (!N()[w]) return;
    T.setSub(w, `${e.ranked} 候補 · ${e.fetching} 件を取得`);
    info()[w] = { title: "画像の探索と確認", kv: [["候補", esc(Object.entries(e.counts).map(([k, v]) => `${k} ${v}`).join(" · "))]] };
    now("画像の候補を集め、1 枚ずつ確かめている");
  },
  "image.fetched": (e) => {
    const w = cur(); if (!N()[w]) return;
    if (!e.ok) {
      const g = w + "-imgfail";
      if (!N()[g]) T.add(g, w, 3, card("取れなかった画像", "0 件", "取得できない・小さすぎる・画像でない"), { title: "取れなかった画像", list: [] }, "failed");
      const list = (info()[g].list ||= []);
      list.push(`${esc(e.stage)} · ${esc(e.reason)} · ${link(e.src)}`); T.setTitle(g, `${list.length} 件`);
      return;
    }
    const id = "i:" + e.src;
    T.add(id, w, 3, `<div class="k"><span class="row">${icon(e.host)}<span>${esc(e.stage)}</span></span><span class="row">${tier(e.tier)}<span class="st"></span></span></div>
      <div class="row" style="margin-top:4px"><div class="thumb">${e.related ? "参考" : "確認前"}</div><div style="min-width:0"><div class="t">${esc(e.title || host(e.src))}</div><div class="s">取得した · ${esc(e.size)}</div></div></div>
      <div class="steps"><span class="on"></span><span></span><span></span></div>`,
      { title: e.stage, kv: [["画像", link(e.src)], ["掲載ページ", `${link(e.page_url)}（T${e.tier}）`], ["ライセンス", esc(e.license)],
        ["関係", e.related ? esc(`「${e.related}」（${e.relation}）の画像。対象そのものではない`) : ""]] }, "running");
    N()[id].license = e.license;
  },
  "image.llm": (e) => { const id = "i:" + e.src; if (!N()[id]) return; T.setState(id, "llm"); T.step(id, 1, "work"); T.setSub(id, `${WORDS.askingModel}（対象の画像かの判定）`); },
  "image.done": (e) => {
    const id = "i:" + e.src; if (!N()[id]) return;
    const ng = e.level === "不採用";
    T.setState(id, ng ? "failed" : "done"); T.step(id, 1, ng ? "ng" : "on"); T.step(id, 2, ng ? "ng" : "on");
    if (ng) N()[id].el.classList.add("dropped");
    T.setSub(id, `${imageWord(e.level)} · ${e.reason || ""}`);
    const license = e.license || N()[id].license || "";
    const th = N()[id].el.querySelector(".thumb")!;
    if (!ng && !NEEDS_PERMISSION.test(license)) {
      const src = imageUrl(e.src);
      th.innerHTML = `<img src="${src}" alt="${esc(e.description)}">`; info()[id].photo = src; info()[id].alt = e.description;
    } else if (ng && !NEEDS_PERMISSION.test(license)) {
      th.innerHTML = `<img src="${imageUrl(e.src)}" alt="" onerror="this.remove()"><span class="un">不採用</span>`;
    } else th.innerHTML = "許可が<br>必要";
    kv(id).push(["判定", esc(imageWord(e.level))], ["理由", esc(e.reason)], ["写っているもの", esc(e.description)],
      ["根拠: 掲載元・文脈・見た目", e.context ? esc(`掲載元 T${e.tier ?? "?"} · 文脈 ${e.context} · 見た目 ${e.visual}`) : ""],
      ["画像の中の文字", esc(e.visible_text)],
      ["画像で確かめた特徴", esc((e.features_seen || []).join("、"))], ["ライセンス", esc(license)]);
  },
  "image.final": (e) => {
    const w = cur();
    T.order.filter((k) => k.startsWith("i:") && N()[k].parent === w && N()[k].el.classList.contains("running")).forEach((k) => {
      T.setState(k, "planned"); T.setSub(k, "画像モデルで判定していない（必要な枚数がそろった、または判定の上限）"); T.step(k, 1, "ng"); T.step(k, 2, "ng");
      const lic = N()[k].license || "", src = k.slice(2);
      N()[k].el.querySelector(".thumb")!.innerHTML = NEEDS_PERMISSION.test(lic) ? "未確認"
        : `<img src="${imageUrl(src)}" alt="" onerror="this.remove()"><span class="un">未確認</span>`;
    });
    (e.withheld || []).forEach((x: { src: string; why: string }) => { const id = "i:" + x.src; if (!N()[id]) return; N()[id].el.classList.add("dropped"); T.setSub(id, `${imageWord("確認済み")} · 許可が必要なので載せない`); kv(id).push(["報告", esc(x.why)]); });
    if (N()[w]) T.setSub(w, S.summary.imageText());                // 流れの図と同じ集計
  },
  "conflicts": (e) => {
    const w = cur(); if (!N()[w]) return;
    T.setSub(w, `比べた観点 ${e.aspects.length} · ${WORDS.conflicts(e.conflicts.length)}`);
    if (N().cov) T.setSub("cov", S.summary.coverageText());
    type Fact = { text: string; source: string };
    e.conflicts.forEach((c: { aspect: string; topic: string; facts: Fact[] }, i: number) => T.add(`${w}-c${i}`, w, 3, card("食い違い · " + c.aspect, c.topic, c.facts.map((f) => f.text).join(" / ")),
      { title: `食い違い: ${c.topic}`, list: c.facts.map((f) => `${esc(f.text)} — ${link(f.source)}`) }, "done"));
  },
  "report": (e) => {
    S.sources = e.sources || [];
    // 報告に引用した分野別データベースの資料（出典から逆引き。旧ログにはない）
    const db = S.dbNode[cur()];
    if (db && info()[db]) kv(db).push(["報告に引用した資料", e.db_cited ? `${e.db_cited.length} 件` : "記録なし"]);
    const w = cur();
    if (N()[w]) { T.setSub(w, `${WORDS.report} · 出典 ${S.sources.length} 件`); info()[w].list = S.sources.map((s, i) => `[${i + 1}] ${esc(s.title)} — ${link(s.url)} · T${s.tier}`); }
  },
};
