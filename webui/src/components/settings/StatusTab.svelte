<!-- 設定のパネルの「接続と使用量」: エージェント（とそれが使うモデル・検索）、モデルの窓口、開いている調査の費用 -->
<script lang="ts">
  import { onMount } from "svelte";
  import { api, gatewayBase, needConnect } from "../../core/api.ts";
  import { DESKTOP } from "../../core/desktop.ts";
  import { ctx } from "../../core/ctx.ts";
  import { agentStatus, agents, catalog, modelStatus } from "../../stores/state.ts";
  import { refreshStatus } from "../../panels/settings.ts";
  import { fmtNum, fmtUsd, isPriced } from "../../panels/models.ts";
  import StatusDot from "./StatusDot.svelte";

  const time = (at: number) => new Date(at).toLocaleTimeString("ja-JP");
  const st = $derived($agentStatus.data);
  const name = $derived($agents[ctx.agentId]?.name || ctx.agentId);
  const checks = $derived(Object.values(st?.checks || {}));

  const gw = $derived($modelStatus.data);
  const usage = $derived(gw?.models || {});
  const spend = $derived(gw?.spend || {});
  const limits = $derived(spend.limits || {});
  const models = $derived($catalog?.models || Object.keys(usage).map((id) => ({ id, title: id })));
  const unmeasured = $derived(Object.values(usage).reduce((n: number, s: any) => n + (s.unmeasured || 0), 0) as number);

  // 開いている調査の費用（窓口が数えたもの）
  const runId = ctx.runId;
  let cost = $state("読み込み中…");
  async function loadCost() {
    if (!runId) return;
    try {
      const u = await api(`/api/runs/${encodeURIComponent(runId)}/usage`);
      cost = `${fmtUsd(u.usd || 0)}${u.unmeasured ? ` · 未計測 ${u.unmeasured} 件` : ""}${u.limit_usd ? ` · 上限 ${fmtUsd(u.limit_usd)}` : ""}`;
    } catch (err) { cost = `読めません（${(err as Error).message}）`; }
  }
  onMount(() => { refreshStatus(); loadCost(); });
</script>

{#if DESKTOP}
  <h4>つなぐ先</h4>
  <p class="spend mono">{gatewayBase() || "まだ決めていない"}</p>
  <div class="fbtns"><button type="button" id="changeGateway" onclick={() => needConnect.set(true)}>つなぐ先を変える</button></div>
{/if}
<h4>エージェント</h4>
{#if !st}
  <p class="note">まだ確かめていません。</p>
{:else}
  <p class="since">{time($agentStatus.at)} に確認</p>
  {#if !st.ok}
    <p class="ngline"><i class="dot ng"></i>{name}につながらない（{st.error || "応答なし"}）</p>
  {:else}
    <table class="usage"><thead><tr><th>確かめたこと</th><th>結果</th></tr></thead><tbody>
      <tr><td>{name}（エージェント）</td><td><i class="dot ok"></i>応答あり</td></tr>
      {#each checks as c (c.title)}
        <tr><td>{c.title}</td><td>
          {#if c.ok}<i class="dot ok"></i>{c.seconds ?? "?"} 秒で応答
          {:else}<i class="dot ng"></i>{c.error || "応答なし"}{c.hint ? ` · ${c.hint}` : ""}{/if}
        </td></tr>
      {/each}
      {#if st.model}<tr><td>エージェントの設定のモデル</td><td class="mono">{st.model}</td></tr>{/if}
    </tbody></table>
  {/if}
{/if}

<h4>モデルの窓口</h4>
{#if !gw}
  <p class="ngline"><i class="dot ng"></i>モデルの窓口につながらない</p>
{:else}
  <p class="since">{time($modelStatus.at)} に確認（一覧 API の応答）</p>
  <table class="usage"><thead><tr><th>モデル</th><th>接続</th><th>呼び出し</th><th>トークン（入力・出力）</th><th>費用（USD）</th></tr></thead><tbody>
    {#each models as m (m.id)}
      {@const s = usage[m.id] || {}}
      <tr><td>{m.title}</td><td><StatusDot id={m.id} />{s.ok ? `${s.seconds ?? "?"} 秒` : s.error || "応答なし"}</td>
        <td class="mono">{fmtNum(s.calls)}{s.errors ? `（失敗 ${fmtNum(s.errors)}）` : ""}{s.refused ? `（上限で断った ${fmtNum(s.refused)}）` : ""}{s.unmeasured ? `（未計測 ${fmtNum(s.unmeasured)}）` : ""}</td>
        <td class="mono">{fmtNum(s.prompt_tokens)}・{fmtNum(s.completion_tokens)}</td><td class="mono">{isPriced(m) ? fmtUsd(s.cost_usd || 0) : "—"}</td></tr>
    {/each}
  </tbody></table>
  <p class="since">窓口の起動からの合計</p>
  <p class="spend">今日（{spend.day || ""}）の推計費用 <b class="mono">{fmtUsd(spend.today_usd || 0)}</b>
    {#if unmeasured || spend.unmeasured_today}<br><span class="warn">未計測 {fmtNum(spend.unmeasured_today || unmeasured)} 件（費用に含まない）</span>{/if}</p>
  <p class="spend">{limits.daily_usd || limits.run_usd
    ? `上限: ${limits.daily_usd ? `1 日 ${fmtUsd(limits.daily_usd)}` : ""}${limits.daily_usd && limits.run_usd ? "・" : ""}${limits.run_usd ? `1 調査 ${fmtUsd(limits.run_usd)}` : ""}（上限到達後に有料呼び出しを停止）`
    : "費用の上限なし"}</p>
{/if}

{#if runId}
  <h4>開いている調査の費用</h4><p class="spend" id="runCostLine">{cost}</p>
{/if}
<div class="fbtns"><button type="button" id="recheck" onclick={refreshStatus}>確かめ直す</button></div>
