// 画面の部品が共有する状態（開いている依頼・エージェントの名乗り・再生の様子）
export const ctx = {
  AGENTS: {},        // id → 名乗りの要約（/api/agents）
  agentId: "",       // 新しい依頼を送るエージェント
  runId: null,       // 開いている依頼
  runAgent: "",      // 開いている依頼のエージェント
  instant: false,    // 動きを付けずにすぐ描く（再生の「すぐ」）
  replay: false,     // 終わった依頼の記録を再生している
  view: "flow",      // 流れの図（flow）か木（tree）か
  awakeUntil: 0,     // この時刻（performance.now()）までは毎フレーム描く（wake）
  frames: { drawn: 0, idle: 0 },   // 描いたフレームと、休んだフレームの数（確かめるため）
};

// 図を描き直す必要があることを知らせる。何も起きていない画面では、毎フレームの線の計算と SVG の書き直しを休む。
// 起こすきっかけ: 新しいイベント・再生・拡大縮小と移動・画面の大きさ・表示の切り替え。
// 秒数は、ノードが膨らむ・線が伸びる・事実が飛ぶ・点や粒が流れる動きが終わるまでの長さ
export function wake(seconds = 4) {
  ctx.awakeUntil = Math.max(ctx.awakeUntil, performance.now() + seconds * 1000);
}

export const manifest = (id = ctx.runAgent) => ctx.AGENTS[id] || {};
// 名乗りの段（levels）: 1 段（作業・段階）があれば流れの図と木を出す。まだ名乗りを受け取っていなければ出す
export const hasFlow = (id) => (ctx.AGENTS[id]?.levels || [0, 1]).includes(1);
export const caps = (id) => ctx.AGENTS[id]?.capabilities || ["pause", "questions"];
// 名乗りの graph にあるノードの名前（なければノードの id）
export function labelOf(node, id = ctx.runAgent) {
  return (ctx.AGENTS[id]?.graph?.nodes || []).find((n) => n.id === node)?.label || node;
}
