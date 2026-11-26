<!-- 設定の欄（エージェントの名乗りの settings_schema から）。値を変えたら onchange(欄の名前, 値) -->
<script lang="ts">
  import { fieldList } from "../../core/schema-form.ts";
  import type { SettingsSchema, SettingsValues } from "../../types/api.ts";

  let { schema, values, onchange }: {
    schema: SettingsSchema; values: SettingsValues; onchange: (name: string, value: unknown) => void;
  } = $props();
  const fields = $derived(fieldList(schema, values));
  const target = (e: Event) => e.currentTarget as HTMLInputElement;
</script>

<div class="fields">
  {#each fields as f (f.name)}
    <div class="frow" class:bool={f.kind === "bool"}>
      <label for={f.id}>{f.title}{#if !f.isDefault}<i class="chg" title={`既定（${JSON.stringify(f.def)}）から変えた`}></i>{/if}</label>
      {#if f.kind === "bool"}
        <input type="checkbox" id={f.id} data-name={f.name} checked={!!f.value} onchange={(e) => onchange(f.name, target(e).checked)}>
      {:else if f.kind === "num"}
        <input type="number" id={f.id} data-name={f.name} value={f.value} min={f.min} max={f.max} step={f.step}
          onchange={(e) => onchange(f.name, target(e).value)}>
      {:else if f.kind === "select"}
        <select id={f.id} data-name={f.name} onchange={(e) => onchange(f.name, target(e).value)}>
          {#each f.options as o (o)}<option selected={o === f.value}>{o}</option>{/each}
        </select>
      {:else if f.kind === "multi"}
        <span class="multi" data-name={f.name}>
          {#each f.options as o (o)}
            <label><input type="checkbox" value={o} checked={f.value.includes(o)}
              onchange={(e) => onchange(f.name, target(e).checked ? [...f.value, o] : f.value.filter((x: string) => x !== o))}>{o}</label>
          {/each}
        </span>
      {:else}
        <input type="text" id={f.id} data-name={f.name} value={f.value} onchange={(e) => onchange(f.name, target(e).value)}>
      {/if}
      {#if f.hint}<small>{f.hint}</small>{/if}
    </div>
  {/each}
</div>
