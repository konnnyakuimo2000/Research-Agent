<!-- 履歴の行の「…」のメニュー。動いている調査は削除できない（終わってから） -->
<script lang="ts">
  import { tick } from "svelte";
  import { runMenu } from "../../stores/state.ts";
  import { statusLabel } from "../../panels/settings.ts";
  import type { RunSummary } from "../../types/api.ts";

  let { onDetail, onDelete }: {
    onDetail: (r: RunSummary, anchor: HTMLElement) => void;
    onDelete: (r: RunSummary, anchor: HTMLElement) => void;
  } = $props();
  const UNFINISHED = ["running", "waiting", "paused", "finishing"];
  let el: HTMLElement | undefined = $state();
  const m = $derived($runMenu);
  const busy = $derived(!!m && UNFINISHED.includes(m.run.status));
  const pos = $derived.by(() => {
    if (!m) return "";
    const rect = m.anchor.getBoundingClientRect();
    return `top:${Math.min(rect.bottom + 4, window.innerHeight - 140)}px;left:${Math.max(8, rect.right - 190)}px`;
  });
  $effect(() => { if (m) tick().then(() => el?.querySelector("button")?.focus()); });
  const close = () => runMenu.set(null);
  // 閉じるとストアが空になるので、依頼とボタンは閉じる前に取っておく
  function act(fn: (r: RunSummary, anchor: HTMLElement) => void) {
    if (!m) return;
    const { run, anchor } = m; close(); fn(run, anchor);
  }
  function outside(ev: MouseEvent) {
    if (m && el && !el.contains(ev.target as Node) && ev.target !== m.anchor) close();
  }
</script>

<svelte:document onclick={outside} />

{#if m}
  <div class="runmenu" id="runMenu" role="menu" style={pos} bind:this={el}>
    <button type="button" role="menuitem" data-act="detail" onclick={() => act(onDetail)}>詳細を見る</button>
    <button type="button" role="menuitem" data-act="delete" aria-disabled={busy ? "true" : undefined}
      onclick={() => { if (!busy) act(onDelete); }}>削除</button>
    {#if busy}<p class="mnote">終わってから削除できます（{statusLabel(m.run.status)}）</p>{/if}
  </div>
{/if}
