<!-- 履歴の詳細: 開始時に効いた設定と、役割ごとのモデル。実際に応答したモデルと費用は、窓口の記録があれば -->
<script lang="ts">
  import { api, withKey } from "../../core/api.ts";
  import { agents } from "../../stores/state.ts";
  import { presetName, runSettingRows, statusLabel } from "../../panels/settings.ts";
  import { answeredMeta, fmtUsd, mTitle, modelOf, roleList } from "../../panels/models.ts";
  import type { RunSummary } from "../../types/api.ts";

  let { run }: { run: RunSummary } = $props();
  const when = $derived(run.created ? new Date(run.created * 1000).toLocaleString("ja-JP") : "");
  const rows = $derived.by(() => { void $agents; return runSettingRows(run); });
  const roles = $derived.by(() => { void $agents; return Object.fromEntries(roleList(run.agent).map((x) => [x.role, x.title])); });
  const preset = $derived.by(() => { void $agents; return run.effective_settings ? presetName(run.agent, run.effective_settings) : ""; });
  const models = $derived(Object.entries(run.models || {}));
  const log = $derived(withKey(`/api/runs/${encodeURIComponent(run.id)}/llm-log`));

  let answered = $state({ text: "読み込み中…", title: "" });
  $effect(() => {
    const id = run.id, agent = run.agent;
    answered = { text: "読み込み中…", title: "" };
    api(`/api/runs/${encodeURIComponent(id)}/usage`).then((u) => {
      if (id !== run.id) return;
      const a = answeredMeta(u.answered, agent);
      let text = a ? a.text.replace(/^応答したモデル: /, "") : "記録なし";
      if (u.usd || u.unmeasured) text += ` · 費用 ${fmtUsd(u.usd || 0)}${u.unmeasured ? `（未計測 ${u.unmeasured} 件）` : ""}`;
      answered = { text, title: a?.title || "" };
    }).catch((err) => { answered = { text: `読めません（${err.message}）`, title: "" }; });
  });
</script>

<dl class="kv"><dt>依頼</dt><dd>{run.request || ""}</dd><dt>エージェント</dt><dd>{$agents[run.agent]?.name || run.agent}</dd>
  <dt>状態</dt><dd>{statusLabel(run.status)}</dd><dt>始めた時刻</dt><dd>{when}</dd><dt>ID</dt><dd class="mono">{run.id}</dd></dl>
<h4>開始時に適用した調査の設定</h4>
{#if !run.effective_settings}<p class="note">開始時の既定値は記録なし（変えた項目のみ）</p>{/if}
{#if rows.length}
  <table class="usage"><thead><tr><th>項目</th><th>値</th><th></th></tr></thead><tbody>
    {#each rows as r (r.title)}<tr><td>{r.title}</td><td>{r.value}</td><td>{r.mark}</td></tr>{/each}
  </tbody></table>
{:else}
  <p class="note">設定の記録はありません。</p>
{/if}
{#if preset}<p class="since">プリセット: {preset}</p>{/if}
<h4>役割ごとのモデル（開始時の割り当て）</h4>
{#if models.length}
  <table class="usage"><tbody>
    {#each models as [role, id] (role)}<tr><td>{roles[role] || role}</td><td>{mTitle(id)}{modelOf(id) ? "" : "（今は一覧にない）"}</td></tr>{/each}
  </tbody></table>
{:else}
  <p class="note">記録なし</p>
{/if}
<h4>実際に応答したモデル</h4><p class="spend" id="runAnswered" title={answered.title || undefined}>{answered.text}</p>
<h4>LLM とのやり取り</h4>
<p class="fbtns"><a class="linkbtn" href={log} target="_blank" rel="noopener">テキストで開く</a>
  <a class="linkbtn" href={`${log}${log.includes("?") ? "&" : "?"}download=1`} download>保存</a></p>
<p class="since">送った全文と応答（記録を始めた後の調査だけ）</p>
