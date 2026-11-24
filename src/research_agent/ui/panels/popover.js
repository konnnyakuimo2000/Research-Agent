// 入力欄から開く窓（モデル・設定）。入力欄の上に開き、上に場所がなければ下に開いて、高さを画面に収める
import { $ } from "../core/util.js";

export function placePop(pop) {
  const r = $("composer").getBoundingClientRect();
  const above = r.top - 16, below = window.innerHeight - r.bottom - 16, down = above < 320 && below > above;
  pop.classList.toggle("below", down);
  pop.style.maxHeight = Math.max(160, Math.min(560, down ? below : above)) + "px";
}
