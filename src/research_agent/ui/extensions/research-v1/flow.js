// research/v1 の流れの図: 判断を中心に作業が並ぶ並べ方（共通の GraphFlow の代わりに使う）。
// 配置: 左から「依頼 → 判定 → 判断 → 作業 → 集まったもの」の 5 列。作業は決まった順のうち出てきたものだけを縦に詰め、
//       判断・判定・依頼は作業の並びの真ん中に揃える。判断と作業の間の行きと帰りの線は、判断の右端に扇形に分けた平行な 2 本。
// 名乗りの graph にない作業（新しい版のエージェントが足したノード）は、作業の列の最後の行に、名乗りの名前で出る。
// 情報の流れ（検索結果・ページ・事実・画像・出典）は、線を流れる粒で描く。
import { $, esc, host, icon, link, fmtPt, imageUrl } from "../../core/util.js";
import { ctx, labelOf } from "../../core/ctx.js";
import { FlowBase } from "../../views/flow.js";
import { tree } from "../../views/tree.js";
import { showDetails } from "../../panels/details.js";
import { ACTION, SEARCH_KIND, NEEDS_PERMISSION, redirectHooks } from "./tree.js";

const STAGE = { check_feasibility: "feas", decide: "hub", search: "search", read_pages: "read", find_images: "images",
  check_conflicts: "conflicts", ask_user: "ask", respond: "respond", report_infeasible: "infeasible" };
const LLM_STAGES = new Set(["feas", "hub", "conflicts", "respond", "infeasible"]);
const KNOWN_ORDER = ["ask", "search", "read", "images", "conflicts", "respond"];
const ROW_DATA = { read: ["pages", "cov"], images: ["imgs"], respond: ["end"] };   // 作業の行に並べる、集まったもののノード
const COL = { start: 0, feas: 210, infeasible: 210, hub: 420, stage: 740, data1: 1050, data2: 1350 };
const FLOW_DEF = {
  start:      { col: "start", w: 180, label: "依頼", cls: "start terminal" },
  feas:       { col: "feas", w: 170, label: "判定" },
  hub:        { col: "hub", w: 230, label: "判断", cls: "hub" },
  infeasible: { col: "infeasible", w: 190, label: "調べられないことの報告" },
  ask:        { col: "stage", w: 240, label: "聞き返し" },
  search:     { col: "stage", w: 240, label: "検索" },
  read:       { col: "stage", w: 240, label: "ページを読む" },
  images:     { col: "stage", w: 240, label: "画像" },
  conflicts:  { col: "stage", w: 240, label: "食い違い" },
  respond:    { col: "stage", w: 240, label: "報告" },
  end:        { col: "data1", w: 180, label: "終わり", cls: "terminal" },
  pages:      { col: "data1", w: 250, label: "読んだページ", cls: "data" },
  cov:        { col: "data2", w: 250, label: "網羅表", cls: "data" },
  imgs:       { col: "data1", w: 250, label: "画像の候補", cls: "data" },
};
const stageOf = (node) => STAGE[node] || node;

export class ResearchFlow extends FlowBase {
  reset() {
    super.reset();
    this.ghosts = []; this.pageChip = {}; this.covRow = {}; this.thumb = {}; this.extraStages = [];
    this.unchecked = 0; this.uncheckedWhy = ""; this.reportSources = 0;
    this.labels = {};
  }
  // 作業の行の順: 決まった順の後に、名乗りで足された作業を出てきた順に
  get stageOrder() { return [...KNOWN_ORDER, ...this.extraStages]; }
  isStage(id) { return this.stageOrder.includes(id); }
  def(id) {
    if (FLOW_DEF[id]) return FLOW_DEF[id];
    if (!this.extraStages.includes(id)) this.extraStages.push(id);
    return { col: "stage", w: 240, label: this.labels[id] || labelOf(id) };
  }
  body(id, d) {
    if (id === "pages") return `<div class="chipgrid"></div>`;
    if (id === "cov") return `<div class="covrows"></div>`;
    if (id === "imgs") return `<div class="thumbs"></div>`;
    return `<ul class="hist"></ul>`;
  }
  labelled(a, b, kind) { return a === "hub" && kind !== "back"; }
  layout() {
    if ($("graph").hidden) return;   // 非表示の間は高さが測れない。表示を切り替えたときに並べ直す
    const h = (id) => this.n[id]?.el.offsetHeight || 0;
    const rows = this.stageOrder.filter((s) => this.n[s]);
    let y = 0;
    for (const s of rows) {
      const members = (ROW_DATA[s] || []).filter((m) => this.n[m]);
      const rh = Math.max(h(s), ...members.map(h));
      this.n[s].tgt = { x: COL.stage, y };
      members.forEach((m) => { this.n[m].tgt = { x: COL[FLOW_DEF[m].col], y }; });
      y += rh + 30;
    }
    const mid = rows.length ? (y - 30) / 2 : 40;
    for (const id of ["start", "feas", "hub"]) if (this.n[id]) this.n[id].tgt = { x: COL[FLOW_DEF[id].col], y: mid - h(id) / 2 };
    if (this.n.infeasible) {
      const f = this.n.feas;
      this.n.infeasible.tgt = { x: COL.infeasible, y: (f ? f.tgt.y + h("feas") : mid) + 70 };
      if (this.n.end && !this.n.respond) this.n.end.tgt = { x: COL.data1, y: this.n.infeasible.tgt.y };
    }
    if (ctx.instant) Object.values(this.n).forEach((n) => { n.cur = { ...n.tgt }; });
  }
  // 線の形。判断と作業の間は、判断の右端に作業ごとの出口を扇形に分け、行きと帰りを平行な 2 本にする
  geometry(e) {
    const a = this.boxOf(e.a), b = this.boxOf(e.b);
    const rows = this.stageOrder.filter((s) => this.n[s]);
    const hubY = (stage) => {
      const hub = this.boxOf("hub"), i = rows.indexOf(stage), k = rows.length;
      return hub.y + 16 + (k <= 1 ? 0.5 : i / (k - 1)) * (hub.h - 32);
    };
    const anchor = (n) => n.y + Math.min(24, n.h / 2);
    let p0, p3;
    if (e.a === "hub" && this.isStage(e.b)) { p0 = { x: a.x + a.w, y: hubY(e.b) - 5 }; p3 = { x: b.x, y: anchor(b) - 5 }; }
    else if (e.b === "hub" && this.isStage(e.a)) { p0 = { x: a.x, y: anchor(a) + 5 }; p3 = { x: b.x + b.w, y: hubY(e.a) + 5 }; }
    else if (e.a === "feas" && e.b === "infeasible") {
      p0 = { x: a.x + a.w / 2, y: a.y + a.h }; p3 = { x: b.x + b.w / 2, y: b.y };
      return [p0, { x: p0.x, y: p0.y + 30 }, { x: p3.x, y: p3.y - 30 }, p3];
    } else { p0 = { x: a.x + a.w, y: anchor(a) }; p3 = { x: b.x, y: anchor(b) }; }
    const mx = (p0.x + p3.x) / 2;
    return [p0, { x: mx, y: p0.y }, { x: mx, y: p3.y }, p3];
  }
  // 集まったもののノードが忙しい間（ページを読んでいる、画像を確かめている）は、そこへの線とノードを光らせる
  beforeDraw() {
    this.imgsBusy = Object.values(this.thumb).some((th) => th.classList.contains("llm")) || this.particles.some((q) => q.e.b === "imgs");
    this.pagesBusy = Object.values(this.pageChip).some((c) => c.classList.contains("running") || c.classList.contains("llm"));
    this.n.imgs?.el.classList.toggle("busy", this.imgsBusy); this.n.pages?.el.classList.toggle("busy", this.pagesBusy);
  }
  isHot(e) { return super.isHot(e) || (e.b === "imgs" && this.imgsBusy) || (e.b === "pages" && this.pagesBusy); }
  // LLM が選んだが、プログラムが変えた行き先: 点線（その作業がまだなければ文字）を 2.5 秒かけて消す
  extraFx(t) {
    let fx = "";
    this.ghosts = this.ghosts.filter((g) => {
      const u = (t - g.t0) / 2500; if (u >= 1 || !this.n.hub) return false;
      const op = (1 - u).toFixed(2), hub = this.n.hub;
      const e = g.to && this.e[`hub>${g.to}`];
      if (e?.pts) {
        const [p0, p1, p2, p3] = e.pts.map((p) => ({ x: p.x, y: p.y - 9 }));
        fx += `<path class="gedge ghost" opacity="${op}" d="M${fmtPt(p0)} C${fmtPt(p1)} ${fmtPt(p2)} ${fmtPt(p3)}"/>`;
      } else {
        fx += `<text class="glabel over" opacity="${op}" x="${(hub.cur.x + hub.el.offsetWidth + 16).toFixed(1)}" y="${(hub.cur.y - 10 - u * 14).toFixed(1)}">LLM の選択: ${esc(g.label)} ✕</text>`;
      }
      return true;
    });
    return fx;
  }
  goStage(to) {
    const from = this.cur;
    const back = to === "hub" && this.isStage(from) && from !== "respond";
    this.go(to, { back, state: to === "ask" ? "waiting" : LLM_STAGES.has(to) ? "llm" : "running" });
  }
  updatePages() {
    const chips = Object.values(this.pageChip);
    const ok = chips.filter((c) => c.classList.contains("done")).length, ng = chips.filter((c) => c.classList.contains("failed")).length;
    this.setText("pages", `${chips.length} ページ`, `読めた ${ok} · 読めなかった ${ng}`);
  }
  details(id) {
    const d = super.details(id);
    if (id === "pages") d.list = Object.keys(this.pageChip).map((u) => { const f = tree.N["p:" + u]?.final || u; return `${esc(host(f))} — ${link(f)}`; });
    if (id === "imgs") {
      d.list = Object.entries(this.thumb).map(([u, th]) => `${esc(th.dataset.done || "未確認")} — ${link(u)}`);
      if (this.unchecked) d.kv.push(["未確認", `${this.unchecked} 枚。${esc(this.uncheckedWhy)}`]);
    }
    if (id === "pages") d.note = "アイコンを押すと、そのページで確かめた事実と引用が出ます。";
    return d;
  }
  pageDetails(url) { const d = tree.info["p:" + url]; if (d) showDetails(d); }
  redirected(url, final) {
    const chip = this.pageChip[url]; if (!chip) return;
    const h = host(final);
    chip.title = `${h}（${host(url)} から転送）`; const ci = chip.querySelector(".ico"); if (ci) ci.outerHTML = icon(h);
  }
  on(e) {
    const f = this;
    switch (e.type) {
      case "run.start": f.node("start"); f.setState("start", "done"); f.setText("start", e.request, ctx.runId); f.cur = "start"; break;
      case "step.start": {
        const id = stageOf(e.node);
        f.labels[id] = labelOf(e.node);
        if (id === "ask" && f.cur === "ask") { f.setState("ask", "running"); break; }   // 答えで再開した
        f.goStage(id);
        if (id === "read") f.setText("read", "読む順番を決めている", "");
        if (id === "respond") f.setText("respond", "確かめた事実だけで報告を書いている", "");
        if (id === "conflicts") f.setText("conflicts", "別々のサイトの事実を比べている", "");
        break;
      }
      case "step.end": {
        const id = stageOf(e.node); if (!f.n[id]) break;
        f.seconds[id] += e.seconds || 0; f.count(id);
        if (e.error) { f.setState(id, "failed"); f.setText(id, undefined, e.error); }
        break;
      }
      case "feasibility": {
        const v = { yes: "調べられる", partial: "一部は調べられない", no: "調べられない", unsure: "調べてみる" }[e.verdict] || e.verdict;
        f.setText("feas", v, e.reason); f.push("feas", v); break;
      }
      case "decision": {
        const llm = ACTION[e.llm?.action] || e.llm?.action || "", act = ACTION[e.action] || e.action;
        const changed = e.guard && e.llm?.action !== e.action;
        f.setText("hub", act + (e.action === "search" && e.query ? `「${e.query}」` : ""), changed ? `LLM は「${llm}」→ プログラムが変更` : "LLM の選択どおり");
        f.push("hub", changed ? `${llm} → ${act}（変更）` : act);
        if (changed) {
          f.ghosts.push({ to: e.llm?.action && stageOf(e.llm.action), label: llm, t0: performance.now() });
          const to = stageOf(e.action);
          if (to && to !== "hub") f.edge("hub", to).over++;
        }
        break;
      }
      case "search.start": f.setText("search", e.query, SEARCH_KIND[e.kind] || e.kind); f.push("search", e.query); break;
      case "search.done": {
        const rel = e.results.filter((r) => r.relevant).length;
        f.setText("search", undefined, e.failed || `結果 ${e.results.length} 件 · 対象を含む ${rel} 件`);
        f.flow("search", "hub", Math.max(1, rel), null, "back");   // 検索結果が判断へ戻る
        break;
      }
      case "aspects": {
        const cov = f.node("cov");
        cov.el.querySelector(".covrows").innerHTML = e.aspects.map((a) =>
          `<div class="covrow" data-a="${esc(a)}"><span class="m"></span><span class="nm">${esc(a)}</span><span class="sq"></span></div>`).join("");
        e.aspects.forEach((a) => { f.covRow[a] = cov.el.querySelector(`.covrow[data-a="${CSS.escape(a)}"]`); });
        f.setState("cov", "done"); f.setText("cov", `観点 ${e.aspects.length} つ`, `${e.subject_type} · ${e.origin}が作成`);
        f.setText("read", `対象の種類: ${e.subject_type}`, "観点を作った");
        f.edge(f.n.pages ? "pages" : "read", "cov", "data");
        f.layout();
        break;
      }
      case "databases": f.push("read", `データベース: ${e.used.join(" → ") || "なし"}（記録 ${e.records.length} 件）`); break;
      case "page.planned": case "page.start": {
        let chip = f.pageChip[e.url];
        if (!chip) {
          const pages = f.node("pages"); f.setState("pages", "done"); f.edge("read", "pages", "data");
          if (f.e["read>cov"] && !f.e["pages>cov"]) { f.removeEdge("read>cov"); f.edge("pages", "cov", "data"); }
          chip = document.createElement("div"); chip.className = "pchip planned"; chip.title = e.host;
          chip.innerHTML = icon(e.host); chip.addEventListener("click", () => f.pageDetails(e.url));
          pages.el.querySelector(".chipgrid").appendChild(chip); f.pageChip[e.url] = chip;
          f.updatePages(); f.layout();
        }
        if (e.type === "page.start") { chip.className = "pchip running"; f.setText("read", e.host, e.db ? "データベースの記録を読んでいる" : "ページを取得している"); }
        break;
      }
      case "page.fetched": { const c = f.pageChip[e.url]; if (c && !e.ok) { c.className = "pchip failed"; f.push("read", `${host(e.url)}: ${e.reason}`); f.updatePages(); } break; }
      case "page.llm": { const c = f.pageChip[e.url]; if (c) c.className = "pchip llm"; f.setText("read", host(e.url), "LLM が引用付きで事実を抜き出している"); break; }
      case "page.done": {
        const c = f.pageChip[e.url];
        if (e.error) { if (c) c.className = "pchip failed"; f.push("read", `${host(e.url)}: ${e.error}`); f.updatePages(); break; }
        f.flow("read", "pages", 1, () => {   // 読み終えたページが「読んだページ」へ
          if (c) { c.className = "pchip done"; if (e.facts.length) c.insertAdjacentHTML("beforeend", `<span class="n">${e.facts.length}</span>`); }
          f.updatePages();
          // 照合できた事実が、1 件ずつ網羅表の観点へ流れ、届いたら観点の四角になる
          if (f.n.cov) f.flow("pages", "cov", e.facts.length, (i) => {
            const x = e.facts[i], row = x && f.covRow[x.aspect]; if (!row) return;
            const sq = document.createElement("i"); if (e.tier !== 1) sq.className = "t2"; sq.title = x.text; row.querySelector(".sq").appendChild(sq);
          });
        });
        f.push("read", `${host(e.url)}: 事実 ${e.facts.length} 件`);
        break;
      }
      case "coverage":
        e.rows.forEach((r) => { const row = f.covRow[r.aspect]; if (row) row.className = "covrow " + (r.status === "確認済み" ? "ok" : r.status === "出典不足" ? "weak" : ""); });
        f.setText("cov", undefined, `${e.rows.filter((r) => r.status === "確認済み").length} / ${e.rows.length} 観点を確認済み`);
        break;
      case "image.candidates": f.setText("images", `${e.ranked} 候補`, `${e.fetching} 件を取得して確かめる`); break;
      case "image.fetched": {
        if (!e.ok) break;
        f.node("imgs"); f.setState("imgs", "done"); f.edge("images", "imgs", "data");
        const th = document.createElement("div"); th.className = "th"; th.title = e.title || e.src;
        f.thumb[e.src] = th;
        // 取ってきた画像が粒になって「画像の候補」へ流れ、届いたところでサムネイルが現れる
        f.flow("images", "imgs", 1, () => {
          th.innerHTML = NEEDS_PERMISSION.test(e.license || "") ? "許可が<br>必要" : `<img src="${imageUrl(e.src)}" alt="" onerror="this.remove()">`;
          if (NEEDS_PERMISSION.test(e.license || "")) th.classList.add("lock");
          f.n.imgs.el.querySelector(".thumbs").appendChild(th); f.layout();
        });
        break;
      }
      case "image.llm": { const th = f.thumb[e.src]; if (th) th.classList.add("llm"); f.setText("images", undefined, "VLM が対象かどうか確かめている"); break; }
      case "image.done": {
        const th = f.thumb[e.src]; if (!th) break;
        th.classList.remove("llm"); th.dataset.done = e.level;
        const ok = e.level !== "不採用";
        // 判定が出たサムネイルは、一瞬縁取りが光る。不採用は点線の枠と「不採用」のラベル（画像は色付きのまま）
        th.classList.add(ok ? "ok" : "ng", "flash"); setTimeout(() => th.classList.remove("flash"), 900);
        if (NEEDS_PERMISSION.test(e.license || "")) { th.classList.add("lock"); th.innerHTML = "許可が<br>必要"; }
        else if (!ok) th.insertAdjacentHTML("beforeend", `<span class="un">不採用</span>`);
        th.title = `${e.level}: ${e.description || e.reason || ""}`;
        f.push("images", `${e.level}: ${e.description || e.reason || ""}`);
        break;
      }
      case "image.final": {
        // 取得はできたが、VLM で確かめずに打ち切った候補（確認済みが 2 枚そろった、または確かめる数の上限に達した）
        const ths = Object.entries(f.thumb), checked = ths.filter(([, th]) => th.dataset.done);
        const confirmed = checked.filter(([, th]) => th.dataset.done === "確認済み").length;
        const why = confirmed >= 2 ? "確認済みの画像が 2 枚そろったので、残りは確かめていない" : "確かめる数の上限（6 枚）に達したので、残りは確かめていない";
        ths.filter(([, th]) => !th.dataset.done).forEach(([, th]) => {
          th.classList.add("unchecked"); th.title = `未確認: ${why}`;
          if (!th.classList.contains("lock")) th.insertAdjacentHTML("beforeend", `<span class="un">未確認</span>`);
        });
        f.unchecked = ths.length - checked.length; f.uncheckedWhy = why;
        f.setText("images", `載せる ${e.shown.length} 枚`, `確かめた ${checked.length} 枚`);
        f.setText("imgs", `採用 ${e.shown.length} 枚`, `確かめた ${checked.length} · 未確認 ${f.unchecked}`);
        break;
      }
      case "conflicts":
        f.setText("conflicts", `食い違い ${e.conflicts.length} 件`, `比べた観点 ${e.aspects.length}`);
        e.conflicts.forEach((c) => f.push("conflicts", c.topic));
        if (e.conflicts.length) f.flow("conflicts", "hub", e.conflicts.length, null, "back");
        break;
      case "question": f.setState("ask", "waiting"); f.setText("ask", e.question.split("\n")[0], "利用者の答えを待っている"); break;
      case "answer": f.setState("ask", "running"); f.push("ask", `答え: ${e.answer}`); f.setText("ask", undefined, `答え: ${e.answer}`); break;
      case "report": f.setText("respond", `出典 ${(e.sources || []).length} 件`, "報告を書いた"); f.reportSources = (e.sources || []).length; break;
      case "run.end":
        f.goStage("end"); f.setState("end", "done"); f.setText("end", `${Math.round(e.t)} 秒`, "報告をチャットに出した");
        if (f.n.respond) f.flow("respond", "end", Math.max(1, f.reportSources || 1), null, "");
        break;
      case "run.error": if (f.cur) { f.setState(f.cur, "failed"); f.setText(f.cur, undefined, e.error); } break;
      case "run.cancelled": if (f.cur) { f.setState(f.cur, "failed"); f.setText(f.cur, undefined, "止めた"); } break;
    }
  }
}

// 木の側で転送が分かったら、流れの図のアイコンも切り替える（流れの図があるときだけ）
export function linkRedirects(getFlow) { redirectHooks.push((url, final) => getFlow()?.redirected?.(url, final)); }
