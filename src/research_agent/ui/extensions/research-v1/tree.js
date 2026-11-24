// research/v1 の木の描き手: 判断ごとの枝、網羅表（依頼 → 網羅表 → 観点）、検索・ページ・データベース・画像・食い違い。
// ページで確かめた事実は、観点のノードへ飛んで溜まる
import { esc, host, tier, icon, card, toggleBtn, link, sameUrl, imageUrl } from "../../core/util.js";
import { ctx, labelOf } from "../../core/ctx.js";
import { tree } from "../../views/tree.js";
import { now } from "../../panels/chat.js";

export const ACTION = { search: "検索する", read_pages: "ページを読む", find_images: "画像を探す", check_conflicts: "食い違いを調べる",
  respond: "回答する", ask_user: "聞き返す" };
const WORK = { search: "検索", read_pages: "ページを読む", find_images: "画像", check_conflicts: "食い違い", respond: "報告",
  ask_user: "聞き返し", report_infeasible: "調べられないことの報告" };
export const SEARCH_KIND = { free: "LLM が選んだ言葉", aspect: "観点を狙った検索", site: "サイトを狙った検索", conflict: "食い違いの確かめ直し" };
export const NEEDS_PERMISSION = /all rights reserved|著作権あり|無断転載/i;
const workName = (node) => WORK[node] || labelOf(node);

// この拡張の状態（依頼を開き直すたびに捨てる）
export const S = { round: null, rounds: 0, dbNode: {}, sources: [] };
export function resetTree() { Object.assign(S, { round: null, rounds: 0, dbNode: {}, sources: [] }); }

const T = tree, N = () => tree.N, info = () => tree.info;
// ---- 網羅表のノード ----
const aspectId = (name) => "a:" + name;
function buildCoverage(e) {
  T.add("cov", "req", 1, card("網羅表", `観点 ${e.aspects.length} つ`, `${e.subject_type} · ${e.origin}が作成`) + toggleBtn,
    { title: "網羅表", kv: [["対象の種類", esc(e.subject_type)], ["観点を作ったもの", esc(e.origin)]],
      note: "観点ごとに、確かめた事実の数と状態（未発見 · 出典不足 · 確認済み）をプログラムが集計する。" }, "done", true);
  e.aspects.forEach((a) => T.add(aspectId(a), "cov", 2,
    `<div class="k"><span>観点</span><span class="mono cnt">0</span></div><div class="t">${esc(a)}</div><div class="s">未発見</div><div class="facts"></div>`,
    { title: `観点: ${a}`, kv: [["状態", "未発見"]], facts: [] }, "aspect"));
}
function flyFact(pageId, f, t, delay) {
  const target = N()[aspectId(f.aspect)];
  const land = () => {
    if (!target) return;
    const sq = document.createElement("i"); if (t !== 1) sq.className = "t2"; sq.title = f.text;
    target.el.querySelector(".facts").appendChild(sq);
    target.el.querySelector(".cnt").textContent = target.el.querySelectorAll(".facts i").length;
    // 状態（確認済みなど）は、ページを読み終えたときにプログラムが集計する（coverage のイベント）
    if (!target.el.classList.contains("ok") && !target.el.classList.contains("weak")) T.setSub(target.id, "集計を待っている");
    info()[target.id].facts.push(f);
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
function coverage(rows) {
  rows.forEach((r) => {
    const id = aspectId(r.aspect), n = N()[id]; if (!n) return;
    n.el.classList.toggle("ok", r.status === "確認済み"); n.el.classList.toggle("weak", r.status === "出典不足");
    T.setSub(id, r.status === "確認済み" ? "✓ 確認済み" : r.status + (r.independent > 1 ? `（${r.independent} サイト）` : ""));
    n.el.querySelector(".cnt").textContent = r.facts;
    info()[id].kv = [["状態", esc(r.status)], ["事実", `${r.facts} 件`], ["独立した出典のサイト", `${r.independent}`],
      ["最も高い Tier", r.best_tier ? `T${r.best_tier}` : ""]];
  });
  if (N().cov) T.setSub("cov", `${rows.filter((r) => r.status === "確認済み").length} / ${rows.length} 観点を確認済み`);
}

// 転送されたページ: 本文の発行元（転送先）をノードに覚えさせ、サイト名・アイコンを切り替える。
// 報告の出典は転送先の URL なので、出典番号から飛ぶときはこれで探す
export const redirectHooks = [];
function redirected(id, e) {
  const final = e.final_url, n = N()[id];
  if (!final || sameUrl(final, e.url) || n.final) return;
  n.final = final;
  const h = host(final);
  const k = n.el.querySelector(".k span"); if (k) k.textContent = `ページ · ${host(e.url)} から転送`;
  const ico = n.el.querySelector(".ico"); if (ico) ico.outerHTML = icon(h);
  info()[id].title = h; info()[id].kv.push(["転送先", link(final)]);
  redirectHooks.forEach((f) => f(e.url, final));
}
// 出典の URL（転送先のこともある）から、ページのノードを探す
export function pageIdFor(url) {
  return T.order.find((k) => k.startsWith("p:") && (sameUrl(k.slice(2), url) || (N()[k].final && sameUrl(N()[k].final, url))));
}
function pageNode(e, state) {
  const id = "p:" + e.url;
  if (N()[id]) { if (state !== "planned") T.setState(id, state); return; }
  const parent = e.db && S.dbNode[T.work] ? S.dbNode[T.work] : T.work;
  if (!N()[parent]) return;
  T.add(id, parent, N()[parent].depth + 1, card(e.db ? "記録 · " + e.db : "ページ", e.host, state === "planned" ? "読む順番を待っている" : "取得している", tier(e.tier), icon(e.host)) +
    `<div class="steps"><span></span><span></span><span></span></div>`,
    { title: e.host, kv: [["URL", link(e.url)], ["Tier", `${e.tier}`]], note: "本文に一字一句ある引用の事実だけを残す（引用の照合）。" }, state);
}

// ---- イベント → 木 ----
export const treeRenderers = {
  "step.start": (e) => {
    if (e.node === "decide") {
      S.rounds++; const id = "d" + e.step; S.round = id;
      T.add(id, "req", 1, `<div class="row"><span class="num">判断 ${S.rounds}</span><span class="choice">LLM が考えている</span><span class="st"></span></div>`,
        { title: `判断 ${S.rounds}` }, "round llm");
      now(`判断 ${S.rounds} · 次に何をするかを LLM が考えている`);
      return;
    }
    if (e.node === "check_feasibility") {
      T.add("w" + e.step, "req", 1, `<div class="row"><span class="num">判定</span><span class="choice">調べられるか</span><span class="st"></span></div>`,
        { title: "公開情報で調べられるか" }, "round llm feas");
      now("依頼が公開情報で調べられるかを、LLM が判定している");
      return;
    }
    const id = "w" + e.step;
    if (N()[id]) { T.setState(id, "running"); return; }   // 聞き返しの答えで再開した
    T.work = id;
    const name = workName(e.node);
    T.add(id, S.round || "req", 2, card(name, name, "始めた") + toggleBtn,
      { title: name }, e.node === "respond" || e.node === "check_conflicts" ? "llm" : "running");
    if (e.node === "respond") now("確かめた事実と出典番号から、LLM が報告を書いている");
    if (e.node === "check_conflicts") now("観点ごとに、別々のサイトの事実が食い違っていないか確かめている");
  },
  "step.end": (e) => {
    const id = e.node === "decide" ? "d" + e.step : "w" + e.step;
    if (e.error) { T.setState(id, "failed"); T.setSub(id, e.error); }
    else if (!N()[id]?.el.classList.contains("failed")) T.setState(id, "done");
    if (info()[id]) info()[id].kv = [...(info()[id].kv || []), ["所要時間", `${e.seconds} 秒${e.llm_wait ? `（うち LLM サーバーの待ち ${e.llm_wait} 秒）` : ""}`]];
  },
  "status": () => {},     // 状態の一行は、木ではなくノードの中身で分かる
  "feasibility": (e) => {
    const id = T.order.find((k) => N()[k].el.classList.contains("feas"));
    if (!id) return;
    N()[id].el.querySelector(".choice").textContent = { yes: "調べられる", partial: "一部は調べられない", no: "調べられない", unsure: "調べてみる" }[e.verdict] || e.verdict;
    info()[id].kv = [["判定", esc(e.verdict)], ["理由", esc(e.reason)], ["調べられない部分", esc((e.infeasible_parts || []).join("、"))]];
    T.layout();
  },
  "decision": (e) => {
    const r = S.round;
    if (!r || !N()[r]) return;
    const llm = ACTION[e.llm?.action] || e.llm?.action || "", act = ACTION[e.action] || e.action;
    const q = e.action === "search" && e.query ? `「${e.query}」` : "";
    const changed = e.guard && e.llm?.action !== e.action || (e.guard && e.llm?.query !== e.query);
    N()[r].el.querySelector(".choice").innerHTML = changed ? `<span class="was">${esc(llm)}</span> → ${esc(act + q)}` : esc(act + q);
    if (changed) N()[r].el.querySelector(".row").insertAdjacentHTML("beforeend", `<span class="tag">変更</span>`);
    const plan = e.plan ? `点数 ${e.plan.score}（${Object.entries(e.plan.parts || {}).map(([k, v]) => `${esc(k)} ${(+v).toFixed(2)}`).join(" × ")}）` : "";
    info()[r].kv = [["LLM の選択", esc(llm + (e.llm?.query ? `「${e.llm.query}」` : ""))], ["LLM の理由", esc(e.llm?.reason)],
      ["実行した行動", esc(act + q)], ["プログラムの変更", esc(e.guard)], ["検索の点数", plan],
      ["他の候補", (e.plan?.alternatives || []).map((a) => `${esc(a.query)}（${a.score}）`).join("<br>")]];
    info()[r].note = changed ? "LLM の選択を、網羅表と止める条件を見たプログラムが変えた（guard）。" : "";
    T.layout();
  },
  "search.start": (e) => {
    const w = T.work; if (!N()[w]) return;
    N()[w].el.querySelector(".k span").textContent = "検索 · " + (SEARCH_KIND[e.kind] || e.kind);
    T.setTitle(w, e.query); T.setSub(w, "SearXNG と Wikidata に問い合わせている");
    info()[w] = { title: "検索", kv: [["検索の言葉", esc(e.query)], ["種類", esc(SEARCH_KIND[e.kind] || e.kind)], ["狙った観点", esc(e.aspect)], ["狙ったサイト", esc(e.site)]] };
    now(`検索している: ${e.query}`);
  },
  "search.done": (e) => {
    const w = T.work; if (!N()[w]) return;
    const rel = e.results.filter((r) => r.relevant).length;
    T.setSub(w, e.failed ? e.failed : `結果 ${e.results.length} 件 · 対象を含む ${rel} 件${e.target ? " · 対象: " + e.target.label : ""}`);
    if (e.failed) T.setState(w, "failed");
    info()[w].kv.push(["結果", `${e.results.length} 件（対象の名前を含む ${rel} 件）`], ["応答しなかったエンジン", esc((e.unresponsive || []).join("、"))],
      ["やり直し", e.retried ? "0 件だったので時間をおいて 1 回やり直した" : ""], ["公式サイト", esc((e.official || []).join("、"))],
      ["拾った表記ゆれ", esc((e.variants || []).join("、"))]);
    if (e.candidates?.length) {
      T.add(w + "-cand", w, 3, card("同名の候補 · Wikidata", e.target ? e.target.label : `${e.candidates.length} 件`, e.target ? "対象が決まった" : "決まっていない", "", icon("wikidata.org")) +
        `<div class="chips2">${e.candidates.slice(0, 4).map((c) => `<span>${esc(c.label)}</span>`).join("")}</div>`,
        { title: "同名の候補", list: e.candidates.map((c) => `${esc(c.id)} ${esc(c.label)}（${esc(c.description)}）`) }, "done");
    }
    if (e.results.length) {
      const hosts = [...new Set(e.results.map((r) => r.host))].slice(0, 10);
      T.add(w + "-res", w, 3, card("検索結果", `${e.results.length} 件 · 対象を含む ${rel} 件`, "Tier の高い順に読む") + `<div class="icons">${hosts.map(icon).join("")}</div>`,
        { title: `検索結果（${e.results.length} 件）`, list: e.results.map((r) => `${r.relevant ? "" : "（対象の名前なし）"}${esc(r.title)} — ${link(r.url)} · T${r.tier}`) }, "done");
    }
    if (e.summary_url && !N().sum) {
      T.add("sum", w, 3, card("概要", host(e.summary_url), "対象の概要（報告の冒頭に使う）", tier(2), icon(host(e.summary_url))),
        { title: "概要", kv: [["URL", link(e.summary_url)]] }, "done");
      N().sum.url = e.summary_url;
    }
  },
  "aspects": (e) => {
    buildCoverage(e);
    const w = T.work;
    if (N()[w]) { T.setSub(w, `${e.subject_type} · 観点 ${e.aspects.length} つ`); info()[w].kv = [["対象の種類", esc(e.subject_type)], ["観点", esc(e.aspects.join("、"))]]; }
    now("対象の種類を判定し、網羅表の観点を作った");
  },
  "databases": (e) => {
    const w = T.work; if (!N()[w]) return;
    const id = w + "-db"; S.dbNode[w] = id;
    T.add(id, w, 3, card("分野別データベース · L3", e.used.join(" → ") || "この種類に使えるものがない", `記録 ${e.records.length} 件（${e.seconds} 秒）${e.failures.length ? " · 失敗あり" : ""}`,
      "", e.records[0] ? icon(host(e.records[0].url)) : "") + toggleBtn,
      { title: "分野別データベース（L3）", kv: [["引いたもの", esc(e.used.join("、"))], ["使えなかったもの", esc(e.failures.join("、"))]],
        list: e.records.map((r) => `${esc(r.database)}: ${link(r.url)}`) }, e.failures.length && !e.records.length ? "failed" : "done");
  },
  "page.planned": (e) => pageNode(e, "planned"),
  "page.start": (e) => { pageNode(e, "running"); T.step("p:" + e.url, 0, "work"); now(`${e.host} を${e.db ? "（データベースの記録として）" : ""}読んでいる`); },
  "page.fetched": (e) => {
    const id = "p:" + e.url; if (!N()[id]) return;
    redirected(id, e);
    if (!e.ok) { T.setState(id, "failed"); T.step(id, 0, "ng"); T.setSub(id, e.reason); info()[id].kv.push(["結果", esc(e.reason)]); return; }
    T.step(id, 0, "on"); if (e.title) T.setTitle(id, e.title); T.setSub(id, `取得した · ${e.chars.toLocaleString()} 文字`);
    info()[id].title = e.title || info()[id].title;
  },
  "page.llm": (e) => {
    const id = "p:" + e.url; if (!N()[id]) return;
    T.setState(id, "llm"); T.step(id, 1, "work"); T.setSub(id, "LLM が引用付きで事実を抜き出している");
    now(`${host(e.url)} の本文から、LLM が引用付きで事実を抜き出している`);
  },
  "page.done": (e) => {
    const id = "p:" + e.url; if (!N()[id]) return;
    redirected(id, e);
    if (e.error) { T.setState(id, "failed"); T.step(id, 1, "ng"); T.setSub(id, e.error); info()[id].kv.push(["結果", esc(e.error)]); return; }
    T.setState(id, "done"); T.step(id, 1, "on"); T.step(id, 2, "on");
    const moved = e.tier !== e.base_tier ? ` · T${e.base_tier}→T${e.tier}` : "";
    T.setSub(id, `事実 ${e.facts.length} 件 · 捨てた ${e.dropped.length} 件${moved}`);
    if (moved) N()[id].el.querySelector(".tier").outerHTML = tier(e.tier);
    Object.assign(info()[id], { facts: e.facts, dropped: e.dropped });
    info()[id].kv.push(["Tier の調整", e.tier_reason ? esc(`T${e.base_tier}→T${e.tier}（${e.tier_reason}）`) : ""],
      ["見分ける特徴", esc((e.features || []).join("、"))], ["別の呼び方", esc((e.new_terms || []).join("、"))],
      ["同名の別の対象", esc(e.other_entity)]);
    e.facts.forEach((f, i) => flyFact(id, f, e.tier, i * 160));
  },
  "page.reprint": (e) => {
    const id = pageIdFor(e.url), orig = pageIdFor(e.original);
    if (!id) return;
    T.setSub(id, `ほぼ同じ文章: ${host(e.original)}（独立した出典に数えない）`);
    if (orig) N()[id].extra.push(orig);
    info()[id].kv.push(["転載", `${link(e.original)} とほぼ同じ文章`]);
  },
  "coverage": (e) => coverage(e.rows),
  "image.candidates": (e) => {
    const w = T.work; if (!N()[w]) return;
    T.setSub(w, `${e.ranked} 候補 · ${e.fetching} 件を取得`);
    info()[w] = { title: "画像の探索と確認", kv: [["候補", esc(Object.entries(e.counts).map(([k, v]) => `${k} ${v}`).join(" · "))]] };
    now("画像の候補を集め、1 枚ずつ確かめている");
  },
  "image.fetched": (e) => {
    const w = T.work; if (!N()[w]) return;
    if (!e.ok) {
      const g = w + "-imgfail";
      if (!N()[g]) T.add(g, w, 3, card("取れなかった画像", "0 件", "取得できない・小さすぎる・画像でない"), { title: "取れなかった画像", list: [] }, "failed");
      info()[g].list.push(`${esc(e.stage)} · ${esc(e.reason)} · ${link(e.src)}`); T.setTitle(g, `${info()[g].list.length} 件`);
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
  "image.llm": (e) => { const id = "i:" + e.src; if (!N()[id]) return; T.setState(id, "llm"); T.step(id, 1, "work"); T.setSub(id, "VLM が対象かどうか確かめている"); },
  "image.done": (e) => {
    const id = "i:" + e.src; if (!N()[id]) return;
    const ng = e.level === "不採用";
    T.setState(id, ng ? "failed" : "done"); T.step(id, 1, ng ? "ng" : "on"); T.step(id, 2, ng ? "ng" : "on");
    if (ng) N()[id].el.classList.add("dropped");
    T.setSub(id, `${e.level} · ${e.reason || ""}`);
    const license = e.license || N()[id].license || "";
    const th = N()[id].el.querySelector(".thumb");
    if (!ng && !NEEDS_PERMISSION.test(license)) {
      const src = imageUrl(e.src);
      th.innerHTML = `<img src="${src}" alt="${esc(e.description)}">`; info()[id].photo = src; info()[id].alt = e.description;
    } else if (ng && !NEEDS_PERMISSION.test(license)) {
      th.innerHTML = `<img src="${imageUrl(e.src)}" alt="" onerror="this.remove()"><span class="un">不採用</span>`;
    } else th.innerHTML = "許可が<br>必要";
    info()[id].kv.push(["判定", esc(e.level)], ["理由", esc(e.reason)], ["写っているもの", esc(e.description)],
      ["文脈 V-1 / 見た目 V-3", e.context ? esc(`${e.context} / ${e.visual}`) : ""], ["画像の中の文字", esc(e.visible_text)],
      ["画像で確かめた特徴", esc((e.features_seen || []).join("、"))], ["ライセンス", esc(license)]);
  },
  "image.final": (e) => {
    const w = T.work;
    T.order.filter((k) => k.startsWith("i:") && N()[k].parent === w && N()[k].el.classList.contains("running")).forEach((k) => {
      T.setState(k, "planned"); T.setSub(k, "未確認（確認済みがそろった、または確かめる数の上限）"); T.step(k, 1, "ng"); T.step(k, 2, "ng");
      const lic = N()[k].license || "", src = k.slice(2);
      N()[k].el.querySelector(".thumb").innerHTML = NEEDS_PERMISSION.test(lic) ? "未確認"
        : `<img src="${imageUrl(src)}" alt="" onerror="this.remove()"><span class="un">未確認</span>`;
    });
    (e.withheld || []).forEach((x) => { const id = "i:" + x.src; if (!N()[id]) return; N()[id].el.classList.add("dropped"); T.setSub(id, "確認済み · 許可が必要なので載せない"); info()[id].kv.push(["報告", esc(x.why)]); });
    if (N()[w]) T.setSub(w, `確かめた ${T.order.filter((k) => k.startsWith("i:") && N()[k].parent === w).length} · 載せる ${e.shown.length}`);
  },
  "conflicts": (e) => {
    const w = T.work; if (!N()[w]) return;
    T.setSub(w, `比べた観点 ${e.aspects.length} · 食い違い ${e.conflicts.length} 件`);
    e.conflicts.forEach((c, i) => T.add(`${w}-c${i}`, w, 3, card("食い違い · " + c.aspect, c.topic, c.facts.map((f) => f.text).join(" / ")),
      { title: `食い違い: ${c.topic}`, list: c.facts.map((f) => `${esc(f.text)} — ${link(f.source)}`) }, "done"));
  },
  "report": (e) => {
    S.sources = e.sources || [];
    const w = T.work;
    if (N()[w]) { T.setSub(w, `出典 ${S.sources.length} 件`); info()[w].list = S.sources.map((s, i) => `[${i + 1}] ${esc(s.title)} — ${link(s.url)} · T${s.tier}`); }
  },
};
