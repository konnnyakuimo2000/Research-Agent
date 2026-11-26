<!-- 設定のパネルの「モデル」: 全役割共通の選択・役割ごとの上書き・最終的な割り当て。外部に送るなら料金とともに知らせる -->
<script lang="ts">
  import { agents, catalog } from "../../stores/state.ts";
  import { editDraft, panel } from "../../panels/settings.ts";
  import { mTitle, modelOf, priceText, resolveModels, roleDefault, roleList, whyNot } from "../../panels/models.ts";
  import StatusDot from "./StatusDot.svelte";

  const draft = $derived($panel.draft!);
  const choice = $derived(draft.models);
  const roles = $derived.by(() => { void $agents; return roleList(draft.agent); });
  const rows = $derived.by(() => { void $catalog; return resolveModels(choice, draft.agent).rows; });
  const defaultIds = $derived.by(() => { void $catalog; return [...new Set(roles.map((r) => roleDefault(r.role)))]; });
  const external = $derived(rows.filter((r) => modelOf(r.id)?.external));
  const all = (id: string) => editDraft((d) => { d.models.all = id; });
  function setRole(role: string, id: string) {
    editDraft((d) => { if (id) d.models.roles[role] = id; else delete d.models.roles[role]; });
  }
</script>

{#if !$catalog}
  <p class="note">モデルの窓口につながらないため選べません。</p>
{:else if !roles.length}
  <p class="note">このエージェントはモデルを選べません。</p>
{:else}
  <h4>全役割共通</h4>
  <div class="opts">
    <button type="button" class="opt" class:on={!choice.all} data-all="" aria-pressed={!choice.all} onclick={() => all("")}>
      <span class="ot">既定</span>
      <span class="os">{defaultIds.length === 1 ? `役割の既定（${mTitle(defaultIds[0])}）` : "役割ごとの既定（窓口の設定どおり）"}</span>
    </button>
    {#each $catalog.models as m (m.id)}
      {@const why = whyNot(m, roles)}
      <button type="button" class="opt" class:on={choice.all === m.id} data-all={m.id} aria-pressed={choice.all === m.id} disabled={!!why}
        onclick={() => all(m.id)}>
        <span class="ot"><StatusDot id={m.id} />{m.title}{#if m.external}<span class="ext">外部</span>{/if}</span>
        <span class="os">{why ? `選べない: ${why}` : `${m.external ? "外部" : "研究室"} · ${priceText(m)}`}</span>
      </button>
    {/each}
  </div>
  <h4>役割ごとの上書き</h4>
  {#each roles as r (r.role)}
    <label class="rrow"><span>{r.title}{#if r.needs?.length}<small>要る能力: {r.needs.join("・")}</small>{/if}</span>
      <select data-role={r.role} onchange={(e) => setRole(r.role, (e.currentTarget as HTMLSelectElement).value)}>
        <option value="">上と同じ</option>
        {#each $catalog.models as m (m.id)}
          {@const why = whyNot(m, [r])}
          <option value={m.id} selected={choice.roles[r.role] === m.id} disabled={!!why}>{m.title}{m.external ? "（外部）" : ""}{why ? `（選べない: ${why}）` : ""}</option>
        {/each}
      </select>
    </label>
  {/each}
  <h4>最終的な割り当て</h4>
  <table class="usage assign"><thead><tr><th>役割</th><th>使うモデル</th><th>メモ</th></tr></thead><tbody>
    {#each rows as r (r.role)}
      <tr><td>{r.title}</td><td>{mTitle(r.id)}{#if modelOf(r.id)?.external}<span class="ext">外部</span>{/if}</td>
        <td>{r.why ? `${mTitle(r.asked)} は使えない（${r.why}）` : r.asked ? "選択" : "既定"}</td></tr>
    {/each}
  </tbody></table>
  <p class="since">外部のモデルは「API キー」から足せます。</p>
  {#if external.length}
    <p class="note warn">{[...new Set(external.map((r) => mTitle(r.id)))].join("・")} に依頼文と読んだページを送ります。料金: {[...new Set(external.map((r) => `${mTitle(r.id)} ${priceText(modelOf(r.id))}`))].join(" / ")}</p>
  {/if}
{/if}
