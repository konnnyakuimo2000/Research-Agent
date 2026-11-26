<!-- 報告の下の操作（registerAction("report", …)。拡張も足せる）。押した結果（「コピーした」）を少しの間出す -->
<script lang="ts">
  import { ctx } from "../../core/ctx.ts";
  import { actionsFor } from "../../core/events.ts";

  let { report }: { report: string } = $props();
  const list = $derived(actionsFor("report").filter((a) => !a.when || a.when(report)));
  let shown: Record<number, string> = $state({});
  async function run(i: number, a: (typeof list)[number]) {
    try {
      const done = await a.run(report, ctx);
      if (done) { shown[i] = done; setTimeout(() => { delete shown[i]; }, 1600); }
    } catch (err) { shown[i] = "できませんでした"; }
  }
</script>

{#if list.length}
  <div class="ractions">
    {#each list as a, i (a.label)}<button type="button" onclick={() => run(i, a)}>{shown[i] || a.label}</button>{/each}
  </div>
{/if}
