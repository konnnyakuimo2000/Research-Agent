<!-- 最近の依頼（調査の一覧の新しい順。同じ依頼は 1 つにまとめ、3 つまで）。押すと入力欄に入る（送るのは利用者） -->
<script lang="ts">
  import { runs } from "../../stores/state.ts";

  let { onPick }: { onPick: (text: string) => void } = $props();
  const recent = $derived.by(() => {
    const seen = new Set<string>(), out: string[] = [];
    for (const r of $runs.list) {
      const q = (r.request || "").trim();
      if (q && !seen.has(q)) { seen.add(q); out.push(q); }
      if (out.length >= 3) break;
    }
    return out;
  });
</script>

{#if recent.length}
  <span class="lbl">最近の依頼</span>
  {#each recent as q (q)}<button type="button" class="ex" title={q} onclick={() => onPick(q)}>{q}</button>{/each}
{/if}
