<!-- 設定のパネルの「API キー」: 外部の Web API の鍵を入れる。鍵の形から提供元を判別し、その提供元のモデル一覧で確かめてから、
     選んだモデルを窓口に足す（modelgw/keys.py）。鍵はモデルの窓口だけが持ち、画面には末尾 4 文字だけが返る。
     入れた鍵は、足すか「やめる」までこの部品の中だけに持つ（ブラウザには保存しない） -->
<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../../core/api.ts";
  import { loadModels } from "../../panels/models.ts";

  interface KeyModel { model: string; title: string; capabilities: string[] | null; price?: Record<string, number>; context_tokens?: number }
  interface Pick { title: string; capabilities: string[]; price: Record<string, number>; context_tokens: number }
  const SHOW = 60;                     // 一覧に出すモデルの数（多い提供元は絞り込みで探す）
  const capLabel: Record<string, string> = { vision: "画像を読める", json_schema: "JSON スキーマに沿える" };

  let list = $state<any[] | null>(null), loadError = $state("");
  let checked = $state<{ provider_title: string; models: KeyModel[] } | null>(null);
  let keyInput = $state(""), pendingKey = "", picks = $state<Record<string, Pick>>({}), filter = $state("");
  let busy = $state(""), message = $state(""), problem = $state("");

  const shown = $derived(checked ? checked.models.filter((m) => !filter || (m.model + " " + m.title).toLowerCase().includes(filter.toLowerCase())) : []);
  const chosen = $derived(Object.keys(picks));

  async function loadKeys() {
    try { list = (await api("/api/models/keys")).keys || []; loadError = ""; }
    catch (err) { list = []; loadError = (err as Error).message; }
  }
  onMount(loadKeys);
  function reset() { checked = null; pendingKey = ""; picks = {}; filter = ""; busy = ""; keyInput = ""; }

  async function check() {
    const key = keyInput.trim();
    if (!key) { problem = "鍵を入れてください"; return; }
    busy = "check"; problem = ""; message = "";
    try { checked = await api("/api/models/keys/check", { key }); pendingKey = key; picks = {}; }
    catch (err) { problem = (err as Error).message; checked = null; }
    busy = "";
  }
  function pick(m: KeyModel, on: boolean) {
    if (on) picks[m.model] = { title: m.title, capabilities: [...(m.capabilities || [])], price: { ...(m.price || {}) }, context_tokens: m.context_tokens || 0 };
    else delete picks[m.model];
  }
  function cap(model: string, c: string, on: boolean) {
    const p = picks[model];
    p.capabilities = on ? [...new Set([...p.capabilities, c])] : p.capabilities.filter((x) => x !== c);
  }
  function price(model: string, field: string, value: string) {
    const p = picks[model];
    if (value === "") delete p.price[field]; else p.price[field] = +value;
  }
  async function add() {
    const models = Object.entries(picks).map(([model, p]) => ({ model, ...p }));
    const missing = models.filter((m) => m.price.input_per_mtok == null || m.price.output_per_mtok == null);
    if (missing.length) { problem = `値段を入れてください: ${missing.map((m) => m.title).join("・")}（無料なら 0）`; return; }
    busy = "add"; problem = "";
    try {
      const entry = await api("/api/models/keys", { key: pendingKey, models: $state.snapshot(models) });
      message = `${entry.provider_title} のモデルを ${entry.models.length} 件足しました`;
      reset(); await loadModels(); await loadKeys();
    } catch (err) { problem = (err as Error).message; busy = ""; }
  }
  async function remove(k: any) {
    if (!window.confirm(`${k.provider_title} の鍵（${k.key}）とモデル ${k.models.length} 件を外しますか？`)) return;
    try { await api(`/api/models/keys/${encodeURIComponent(k.id)}`, undefined, "DELETE"); message = `${k.provider_title} の鍵を外しました`; problem = ""; }
    catch (err) { problem = (err as Error).message; }
    await loadModels(); await loadKeys();
  }
  const num = (v: unknown) => (v == null || v === "" ? "" : String(v));
  const value = (e: Event) => (e.currentTarget as HTMLInputElement);
</script>

<p class="note">鍵の形から提供元（Anthropic・OpenAI・Gemini・OpenRouter）を判別して確かめます。鍵は窓口だけが持ちます。</p>
<h4>入れてある鍵</h4>
{#if list === null}
  <p class="note">読み込み中…</p>
{:else if loadError}
  <p class="ngline"><i class="dot ng"></i>{loadError}</p>
{:else if list.length}
  <table class="usage"><thead><tr><th>提供元</th><th>鍵</th><th>足したモデル</th><th></th></tr></thead><tbody>
    {#each list as k (k.id)}
      <tr><td>{k.provider_title}</td><td class="mono">{k.key}</td><td>{#each k.models as m, i (i)}{#if i}<br>{/if}{m.title}{/each}</td>
        <td><button type="button" class="linkbtn" data-remove={k.id} onclick={() => remove(k)}>外す</button></td></tr>
    {/each}
  </tbody></table>
{:else}
  <p class="note">まだありません。</p>
{/if}

<h4>鍵を足す</h4>
<div class="keyrow">
  <input type="password" id="keyInput" autocomplete="off" spellcheck="false" placeholder="sk-ant-… / sk-… / AIza… / sk-or-…" aria-label="API キー"
    disabled={!!checked} bind:value={keyInput}>
  {#if !checked}
    <button type="button" id="keyCheck" disabled={!!busy} onclick={check}>{busy === "check" ? "確かめています…" : "判別して確かめる"}</button>
  {/if}
</div>
{#if problem}<p class="notice warn" role="alert">{problem}</p>{/if}
{#if message}<p class="notice" role="status">{message}</p>{/if}
{#if checked}
  <p class="spend"><b>{checked.provider_title}</b> の鍵です。足すモデルを選んでください（{checked.models.length} 件）。</p>
  {#if checked.models.length > 12}
    <input type="search" id="keyFilter" placeholder="モデル名で絞り込む" aria-label="モデル名で絞り込む" bind:value={filter}>
  {/if}
  <div class="keymodels">
    {#each shown.slice(0, SHOW) as m (m.model)}
      {@const p = picks[m.model]}
      <div class="kmodel" class:on={!!p}>
        <label class="kpick"><input type="checkbox" data-pick={m.model} checked={!!p} onchange={(e) => pick(m, value(e).checked)}>
          <span>{m.title}{#if m.title !== m.model} <small class="mono">{m.model}</small>{/if}</span></label>
        {#if p}
          <div class="kdetail">
            {#if m.capabilities === null}<small class="warn">能力を選んでください</small>{/if}
            <span class="kcaps">
              {#each Object.entries(capLabel) as [c, label] (c)}
                <label><input type="checkbox" data-cap={c} data-model={m.model} checked={p.capabilities.includes(c)}
                  onchange={(e) => cap(m.model, c, value(e).checked)}>{label}</label>
              {/each}
            </span>
            <span class="kprice">100 万トークン 入力 $<input type="number" min="0" step="any" data-price="input_per_mtok" data-model={m.model}
                value={num(p.price.input_per_mtok)} aria-label={`${m.title} の入力の値段`} onchange={(e) => price(m.model, "input_per_mtok", value(e).value)}>
              出力 $<input type="number" min="0" step="any" data-price="output_per_mtok" data-model={m.model}
                value={num(p.price.output_per_mtok)} aria-label={`${m.title} の出力の値段`} onchange={(e) => price(m.model, "output_per_mtok", value(e).value)}></span>
            {#if !m.price}<small>値段を入れてください（無料なら 0）</small>{/if}
          </div>
        {/if}
      </div>
    {/each}
    {#if shown.length > SHOW}<p class="since">ほかに {shown.length - SHOW} 件（絞り込んでください）</p>{/if}
  </div>
  <div class="fbtns">
    <button type="button" class="primary" id="keyAdd" disabled={!chosen.length || !!busy} onclick={add}>{busy === "add" ? "足しています…" : `選んだ ${chosen.length} 件を足す`}</button>
    <button type="button" id="keyCancel" onclick={() => { reset(); message = ""; problem = ""; }}>やめる</button>
  </div>
{/if}
