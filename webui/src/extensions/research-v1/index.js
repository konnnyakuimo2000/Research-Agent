// research/v1 の画面の部品（docs/telemetry_enhancement_plan.md 4.0・8.1）。
// 調査エージェントが名乗りの extensions に research/v1 を入れているときだけ、画面がこれを読み込む。
// - 木: 判断ごとの枝・網羅表・ページ・画像・食い違いの描き手（共通の描き手に代わる）
// - 流れの図: 判断を中心に作業が並ぶ並べ方（共通の GraphFlow に代わる）
// - 報告の出典番号: 押すと、そのページのノードと、確かめた事実・引用を出す
import { ctx } from "../../core/ctx.js";
import { sameUrl } from "../../core/util.js";
import { registerExtension } from "../../core/events.js";
import { jumpTo } from "../../core/camera.js";
import { tree } from "../../views/tree.js";
import { now } from "../../panels/chat.js";
import { showDetails } from "../../panels/details.js";
import { S, pageIdFor, resetTree, treeRenderers } from "./tree.js";
import { ResearchFlow, linkRedirects } from "./flow.js";

linkRedirects(() => ctx.flow);

registerExtension("research/v1", {
  renderers: {
    tree: treeRenderers,
    chat: { "run.start": () => now("調査を始めた") },
  },
  layouts: { flow: ResearchFlow },
  observe: (e) => S.summary.on(e),
  reset: resetTree,
  cite(n) {
    const s = S.sources[n - 1]; if (!s) return;
    const id = pageIdFor(s.url) || (tree.N.sum && sameUrl(tree.N.sum.url, s.url) ? "sum" : null);
    if (!id) return;
    tree.reveal(id); tree.select(id);
    if (ctx.view === "flow") showDetails(tree.info[id]); else jumpTo(tree.box(id));
  },
});
