<!-- 画面の新しい版が届いたときの知らせ（右下）。「再読み込み」で新しい版にする。「あとで」は次に開いたときに効く -->
<script lang="ts">
  import { update } from "../stores/state.ts";

  let later = $state(false);
</script>

{#if $update.ready && !later}
  <div class="update" role="status">
    <span>画面の新しい版があります</span>
    <button type="button" class="primary" onclick={() => $update.apply()}>再読み込み</button>
    <button type="button" onclick={() => (later = true)}>あとで</button>
  </div>
{/if}

<style>
  .update {
    position: fixed; right: 16px; bottom: 16px; z-index: 95; display: flex; align-items: center; gap: 8px;
    background: var(--bg); color: var(--ink); border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px;
    box-shadow: 0 6px 20px rgb(0 0 0 / .18); font-size: 12.5px; max-width: calc(100vw - 32px);
  }
  button { border: 1px solid var(--line); background: var(--surface); color: var(--ink); border-radius: 7px; padding: 3px 9px; cursor: pointer; }
  button.primary { background: var(--ink); color: var(--bg); border-color: var(--ink); }
</style>
