// research/v1 の記録の解釈（再レビュー: テレメトリーの表記）。流れの図・木・詳細の欄は、件数・単位・言葉をここから取る。
//
// 画面が表すのは「1 件の依頼について、エージェントが記録した作業と、その時点で集まった根拠」。
// 言葉は、実際に測っていることに合わせる:
// - 「事実を照合した」ではなく「原文に引用が見つかった記述」（引用は正規化した本文に部分一致で見つかった。数値や否定の
//   意味まで確かめたわけではない）
// - 「確認済み」ではなく「出典条件を満たした観点」（Tier 1 の出典、または別サイト 2 件以上で 1 件以上が Tier 1〜2。
//   同じ観点に食い違う記述があっても満たしうるので、食い違い候補を並べて出す）
// - モデルの提案と、プログラムが採用した行動を分けて出す（検索語だけを補正したものは「検索語を補正」）
// - 画像は、同じ画像 URL を単位に「取得成功」「画像モデルで判定完了」「参考画像」「報告に掲載」を数える

import type { UIEvent } from "../../types/api.ts";

export const WORDS = {
  quoteFound: (n: number) => `原文に引用が見つかった記述 ${n} 件`,
  quoteDropped: (n: number) => `引用が見つからず除いた記述 ${n} 件`,
  coverage: (ok: number, total: number) => `出典条件を満たした観点 ${ok}/${total}`,
  conflicts: (n: number) => `食い違い候補 ${n} 件`,
  report: "集めた記述と出典から報告を作成",
  processing: "処理中",
  askingModel: "モデルに依頼中",
  elapsed: (s: number | null | undefined) => s == null ? "開始からの経過: 未計測" : `開始からの経過 ${Math.round(s)} 秒`,
  stageTotal: (s: number | null | undefined) => s == null ? "工程の累計: 未計測" : `工程の累計 ${Math.round(s)} 秒`,
  retryWait: (s: number) => `うち再試行関連 ${Math.round(s)} 秒`,
};

// 網羅表の観点の状態（エージェントの判定の名前 → 画面の言葉）
export const COVERAGE: Record<string, string> = {
  "確認済み": "出典条件を満たす",
  "出典不足": "出典条件に届かない",
  "未発見": "今回の取得範囲に採用記述なし",
};
export const coverageWord = (status: string): string => COVERAGE[status] || status;

// 画像の判定（エージェントの採用区分 → 画面の言葉）
export const IMAGE: Record<string, string> = {
  "確認済み": "対象画像の採用条件を満たす",
  "おそらく正しい": "採用条件の一部を満たす",
  "不採用": "不採用",
  "参考画像": "参考画像（対象そのものではない）",
};
export const imageWord = (level: string): string => IMAGE[level] || level;

const ACTION: Record<string, string> = { search: "検索", read_pages: "ページを読む", find_images: "画像を探す", check_conflicts: "食い違いを調べる",
  respond: "報告", ask_user: "聞き返す" };
const actionText = (a: string, q: string) => (ACTION[a] || a || "") + (a === "search" && q ? `「${q}」` : "");

// 判断: モデルの提案と、採用した行動。両方の画面で同じ差分を使う
export interface Decision { kind: "same" | "action" | "query" | "note"; label: string; proposal: string; adopted: string; reason: string; why: string }
export function decisionDiff(e: UIEvent): Decision {
  const pa = e.llm?.action || "", pq = e.llm?.query || "", aa = e.action || "", aq = e.query || "";
  let kind: Decision["kind"] = "same";
  if (e.guard && pa !== aa) kind = "action";
  else if (e.guard && aa === "search" && pq !== aq) kind = "query";
  else if (e.guard) kind = "note";
  const label = { same: "モデルの提案どおり", action: "行動を変更", query: "検索語を補正", note: "提案どおり（注記あり）" }[kind];
  return { kind, label, proposal: actionText(pa, pq), adopted: actionText(aa, aq), reason: e.guard || "", why: e.llm?.reason || "" };
}

// 1 件の依頼の集計。イベントを順に受け取る（流れの図・木・詳細が同じものを読む）
export interface PageState { db: string; stage: string; quoteFound: number; dropped: number; reason: string }
export interface ImageState { fetched: boolean; asked: boolean; judged: boolean; reference: boolean; level: string; shown: boolean }
export interface CoverageRow { aspect: string; status: string; [key: string]: any }
export interface Conflict { aspect: string; [key: string]: any }

export class RunSummary {
  pages: Map<string, PageState>;
  images: Map<string, ImageState>;
  decisions: Decision[];
  coverage: CoverageRow[];
  conflicts: Conflict[];
  stageSeconds: Record<string, number | null>;
  retrySeconds: number;
  elapsed: number | null;
  constructor() {
    this.pages = new Map();       // URL → { db, stage, quoteFound, dropped, reason }
    this.images = new Map();      // 画像 URL → { fetched, asked, judged, reference, level, shown }
    this.decisions = [];
    this.coverage = [];
    this.conflicts = [];
    this.stageSeconds = {};       // ノード → 工程の累計秒（記録がないときは null のまま）
    this.retrySeconds = 0;
    this.elapsed = null;
  }
  on(e: UIEvent): void {
    const page = (u: string): PageState => (this.pages.has(u) || this.pages.set(u, { db: "", stage: "予定", quoteFound: 0, dropped: 0, reason: "" }), this.pages.get(u)!);
    const image = (u: string): ImageState => (this.images.has(u) || this.images.set(u, { fetched: false, asked: false, judged: false, reference: false, level: "", shown: false }), this.images.get(u)!);
    switch (e.type) {
      case "page.planned": page(e.url).db = e.db || ""; break;
      case "page.start": Object.assign(page(e.url), { db: e.db || "", stage: e.db ? "DB記録を読込中" : "本文を取得中" }); break;
      case "page.fetched": {
        const p = page(e.url);
        if (e.ok) p.stage = p.db ? "DB記録を読込済み" : "本文取得済み";
        else Object.assign(p, { stage: "本文を取得できない", reason: e.reason || "" });
        break;
      }
      case "page.done": {
        const p = page(e.url);
        if (e.error) Object.assign(p, { stage: `${p.db ? "DB記録読込済み" : "本文取得済み"}・抽出失敗`, reason: e.error });
        else Object.assign(p, { stage: "抽出済み", quoteFound: (e.facts || []).length, dropped: (e.dropped || []).length });
        break;
      }
      case "image.fetched": if (e.ok) { const i = image(e.src); i.fetched = true; if (e.related) i.reference = true; } break;
      case "image.llm": image(e.src).asked = true; break;
      case "image.done": {
        const i = image(e.src);
        i.level = e.level;
        if (e.level === "参考画像") i.reference = true;   // 参考画像は画像モデルを通さない（判定の数に入れない）
        else if (i.asked) i.judged = true;          // 画像モデルの判定は、開始（image.llm）と結果の組で数える
        break;
      }
      case "image.final": (e.shown || []).forEach((s: string) => { image(s).shown = true; }); break;
      case "decision": this.decisions.push(decisionDiff(e)); break;
      case "coverage": this.coverage = e.rows || []; break;
      case "conflicts": this.conflicts = e.conflicts || []; break;
      case "step.end":
        if (e.seconds == null) { this.stageSeconds[e.node] ??= null; break; }
        this.stageSeconds[e.node] = (this.stageSeconds[e.node] || 0) + e.seconds;
        this.retrySeconds += e.llm_wait || 0;
        break;
      case "run.end": this.elapsed = e.t ?? null; break;
    }
  }
  pageCounts() {
    const ps = [...this.pages.values()];
    return {
      planned: ps.length,
      fetched: ps.filter((p) => /取得済み|読込済み|抽出/.test(p.stage)).length,
      extracted: ps.filter((p) => p.stage === "抽出済み").length,
      failed: ps.filter((p) => /できない|失敗/.test(p.stage)).length,
      quoteFound: ps.reduce((n, p) => n + p.quoteFound, 0),
    };
  }
  pageText() {
    const c = this.pageCounts();
    return `対象資料 ${c.planned} · 本文取得 ${c.fetched} · 抽出 ${c.extracted} · 引用一致 ${c.quoteFound} 件`;
  }
  imageCounts() {
    const is = [...this.images.values()];
    return {
      fetched: is.filter((i) => i.fetched).length,
      judged: is.filter((i) => i.judged).length,
      reference: is.filter((i) => i.reference).length,
      shown: is.filter((i) => i.shown).length,
    };
  }
  imageText() {
    const c = this.imageCounts();
    return `取得成功 ${c.fetched} · 画像モデルで判定 ${c.judged}${c.reference ? ` · 参考画像 ${c.reference}` : ""} · 報告に掲載 ${c.shown}`;
  }
  coverageText() {
    const ok = this.coverage.filter((r) => r.status === "確認済み").length;
    return WORDS.coverage(ok, this.coverage.length) + (this.conflicts.length ? ` · ${WORDS.conflicts(this.conflicts.length)}` : "");
  }
  conflictAspects() { return new Set(this.conflicts.map((c) => c.aspect)); }
}
