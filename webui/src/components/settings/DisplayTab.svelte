<!-- 設定のパネルの「表示」: ペインの幅を既定に戻す -->
<script lang="ts">
  import { panel, setNotice } from "../../panels/settings.ts";
  import { resetWidths, widthsText } from "../../panels/layout.js";

  let text = $state(widthsText());
  function reset() { resetWidths(); text = widthsText(); setNotice("幅を既定に戻した"); }
</script>

{#if $panel.notice}<p class="notice" role="status">{$panel.notice}</p>{/if}
<p class="note">境界のドラッグか矢印キーで幅を変えられます。</p>
<p class="since">{text}</p>
<div class="fbtns"><button type="button" id="resetWidths" onclick={reset}>幅を既定に戻す</button></div>
