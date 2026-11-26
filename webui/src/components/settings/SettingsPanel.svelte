<!-- 設定のパネル（状態と操作は panels/settings.ts）。開いたら見出しへ、閉じたら開いたボタンへフォーカスを移す。Esc で閉じる -->
<script lang="ts">
  import { agents } from "../../stores/state.ts";
  import { TABS, applyOnce, closePanel, panel, resetDraft, saveDefault, setTab, useRun } from "../../panels/settings.ts";
  import type { Tab } from "../../panels/settings.ts";
  import { invalidChoices } from "../../panels/models.ts";
  import ResearchTab from "./ResearchTab.svelte";
  import ModelsTab from "./ModelsTab.svelte";
  import KeysTab from "./KeysTab.svelte";
  import StatusTab from "./StatusTab.svelte";
  import DisplayTab from "./DisplayTab.svelte";
  import RunDetail from "./RunDetail.svelte";

  let title: HTMLElement | undefined = $state();
  const open = $derived($panel.open);
  const tab = $derived($panel.tab);
  const run = $derived($panel.run);
  const tabs = $derived(run ? [...TABS, ["run", "履歴の詳細"] as [Tab, string]] : TABS);
  const known = $derived(!!run && !!$agents[run.agent]);
  const bad = $derived(run && known ? invalidChoices({ all: "", roles: run.models || {} }, run.agent) : []);

  $effect(() => { if (open && title) title.focus(); });     // 開いたときだけ（タブを変えても欄のフォーカスは動かさない）
  function onKey(ev: KeyboardEvent) {
    if (ev.key === "Escape" && $panel.open) { ev.stopPropagation(); closePanel(); }
  }
</script>

<svelte:document onkeydown={onKey} />

{#if open}
  <div class="panel-scrim" id="panelScrim" onclick={closePanel} aria-hidden="true"></div>
  <div class="setpanel" id="setPanel" role="dialog" aria-modal="true" aria-labelledby="setTitle">
    <header>
      <h2 id="setTitle" tabindex="-1" bind:this={title}>{tab === "run" ? "履歴の詳細" : "設定"}</h2>
      <button type="button" id="setClose" aria-label="設定を閉じる" onclick={closePanel}>✕</button>
    </header>
    <div class="settabs" id="setTabs" role="tablist" aria-label="設定の種類">
      {#each tabs as [id, label] (id)}
        <button type="button" role="tab" data-tab={id} aria-selected={tab === id} onclick={() => setTab(id)}>{label}</button>
      {/each}
    </div>
    <div class="pbody" id="setBody">
      {#if tab === "research"}<ResearchTab />
      {:else if tab === "models"}<ModelsTab />
      {:else if tab === "keys"}<KeysTab />
      {:else if tab === "status"}<StatusTab />
      {:else if tab === "display"}<DisplayTab />
      {:else if tab === "run" && run}<RunDetail {run} />
      {/if}
    </div>
    {#if tab === "research" || tab === "models"}
      <footer class="pfoot" id="setFoot">
        {#if $panel.notice}<p class="notice" role="status">{$panel.notice}</p>{/if}
        <div class="fbtns">
          <button type="button" class="primary" id="applyOnce" onclick={applyOnce}>この依頼に適用</button>
          <button type="button" id="saveDefault" onclick={saveDefault}>このブラウザの既定値に保存</button>
          <button type="button" id="resetStd" onclick={resetDraft}>標準に戻す</button>
        </div>
        <p class="since">適用: 次の 1 件だけ · 保存: 毎回</p>
      </footer>
    {:else if tab === "run" && run}
      <footer class="pfoot" id="setFoot">
        {#if bad.length}<p class="notice warn">今は使えない役割があります:{#each bad as b (b)}<br>{b}{/each}</p>{/if}
        <div class="fbtns"><button type="button" class="primary" id="useRun" disabled={!known} onclick={() => useRun(run)}>この設定を使う</button></div>
        <p class="since">{known ? "新しい依頼に読み込みます" : "このエージェントは今つながっていません"}</p>
      </footer>
    {/if}
  </div>
{/if}
