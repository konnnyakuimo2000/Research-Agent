<!-- 設定のパネルの「調査」: 対象エージェント・プリセット・設定の欄 -->
<script lang="ts">
  import { agents } from "../../stores/state.ts";
  import { chooseAgent, choosePreset, panel, presetName, presetsOf, schemaOf, setField } from "../../panels/settings.ts";
  import { defaults, hasFields } from "../../core/schema-form.ts";
  import SchemaFields from "./SchemaFields.svelte";

  const draft = $derived($panel.draft!);
  const list = $derived(Object.values($agents));
  const sc = $derived.by(() => { void $agents; return schemaOf(draft.agent); });
  const presets = $derived.by(() => { void $agents; return Object.entries(presetsOf(draft.agent)); });
  const name = $derived.by(() => { void $agents; return presetName(draft.agent, draft.settings.values); });
  const tip = (values: Record<string, unknown>) =>
    Object.entries(values).map(([k, v]) => `${sc.properties?.[k]?.title || k}: ${v}`).join("\n");
</script>

{#if list.length > 1}
  <label class="rrow"><span>対象エージェント</span>
    <select id="pAgent" onchange={(e) => chooseAgent((e.currentTarget as HTMLSelectElement).value)}>
      {#each list as a (a.id)}
        <option value={a.id} selected={a.id === draft.agent} disabled={!a.ok}>{a.name}{a.ok ? "" : "（つながらない）"}</option>
      {/each}
    </select>
  </label>
{/if}
{#if !hasFields(sc)}
  <p class="note">変えられる設定はありません。</p>
{:else}
  {#if presets.length}
    <div class="presets" role="group" aria-label="プリセット">
      <button type="button" class="pre" class:on={name === "標準"} data-p="" aria-pressed={name === "標準"} onclick={() => choosePreset("")}>標準</button>
      {#each presets as [id, p] (id)}
        <button type="button" class="pre" class:on={name === (p.title || id)} data-p={id} aria-pressed={name === (p.title || id)}
          title={tip(p.values || p)} onclick={() => choosePreset(id)}>{p.title || id}</button>
      {/each}
      {#if name === "カスタム"}<span class="pre on custom" aria-label="今の設定はカスタム">カスタム</span>{/if}
    </div>
  {/if}
  <SchemaFields schema={sc} values={{ ...defaults(sc), ...draft.settings.values }} onchange={setField} />
{/if}
