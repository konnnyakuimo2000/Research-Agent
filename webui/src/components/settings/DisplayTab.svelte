<!-- 設定のパネルの「表示」: ペインの幅を既定に戻す・調査の知らせ（OS の通知） -->
<script lang="ts">
  import { panel, setNotice } from "../../panels/settings.ts";
  import { resetWidths, widthsText } from "../../panels/layout.ts";
  import { notifyState, setNotify } from "../../panels/notify.ts";

  let text = $state(widthsText());
  function reset() { resetWidths(); text = widthsText(); setNotice("幅を既定に戻した"); }
  // 許可を断られた・閉じられたときは、切り替えを戻す（状態が変わらないと、部品は欄を描き直さないため）
  async function toggle(e: Event) {
    const box = e.currentTarget as HTMLInputElement;
    box.checked = (await setNotify(box.checked)) === "on";
  }
  const NOTE: Record<string, string> = {
    on: "画面が後ろにあるとき、開いている調査が答えを待つ・終わる・止まると知らせます。",
    off: "",
    denied: "ブラウザで通知が止められています。サイトの設定で許可してください。",
    unsupported: "この接続では使えません（HTTPS か localhost で開いてください）。",
  };
</script>

{#if $panel.notice}<p class="notice" role="status">{$panel.notice}</p>{/if}
<h4>ペインの幅</h4>
<p class="note">境界のドラッグか矢印キーで幅を変えられます。</p>
<p class="since">{text}</p>
<div class="fbtns"><button type="button" id="resetWidths" onclick={reset}>幅を既定に戻す</button></div>

<h4>調査の知らせ</h4>
<label class="frow bool"><span>答え待ち・完了・失敗を通知する</span>
  <input type="checkbox" id="notifyToggle" checked={$notifyState === "on"} disabled={$notifyState === "unsupported"}
    onchange={toggle}>
</label>
{#if NOTE[$notifyState]}<p class="since">{NOTE[$notifyState]}</p>{/if}
