<!-- 履歴の一覧（左の列）。行を押すと開き、「…」でメニュー（詳細・削除）を出す -->
<script lang="ts">
  import { agents, catalog, openRunId, runMenu, runs } from "../../stores/state.ts";
  import { statusLabel } from "../../panels/settings.ts";
  import { runModelTag } from "../../panels/models.ts";
  import type { RunSummary } from "../../types/api.ts";

  let { onOpen }: { onOpen: (r: RunSummary) => void } = $props();
  const defAgent = $derived(Object.values($agents).find((a) => a.default)?.id);
  const when = (id: string) => id.slice(0, 13).replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2}).*/, "$2/$3 $4:$5");
  function line(r: RunSummary, tag: string): string {
    const agent = $agents[r.agent] && r.agent !== defAgent ? " · " + $agents[r.agent].name : "";
    return `${when(r.id)} · ${statusLabel(r.status)}${agent}${tag ? " · " + tag : ""}`;
  }
  function toggleMenu(r: RunSummary, anchor: HTMLElement) {
    runMenu.set($runMenu?.anchor === anchor ? null : { run: r, anchor });
  }
</script>

{#if $runs.error}
  <p style="padding:8px;color:var(--muted)">一覧を読めません: {$runs.error}</p>
{:else if $runs.loaded}
  {#each $runs.list as r (r.id)}
    {@const tag = $catalog ? runModelTag(r.models) : ""}
    <div class="runrow" data-id={r.id}>
      <button type="button" class="run" class:on={r.id === $openRunId} data-id={r.id} data-agent={r.agent || ""} onclick={() => onOpen(r)}>
        <span class="r1">{r.request || "(依頼なし)"}</span><span class="r2">{line(r, tag)}</span>
      </button>
      <button type="button" class="more" data-id={r.id} aria-haspopup="menu" aria-expanded={$runMenu?.run.id === r.id}
        aria-label={`「${r.request || "依頼"}」の操作`} title="操作"
        onclick={(e) => { e.stopPropagation(); toggleMenu(r, e.currentTarget); }}>…</button>
    </div>
  {:else}
    <p class="r2" style="padding:8px;color:var(--muted)">まだ調査はありません</p>
  {/each}
{/if}
