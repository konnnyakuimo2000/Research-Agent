<!-- デスクトップアプリのつなぐ先（ゲートウェイの住所と鍵）。初めて開いたとき・鍵が違うとき・設定から変えるときに出す。
     確かめてから覚え、画面を開き直す。鍵は Windows の資格情報マネージャーに置く -->
<script lang="ts">
  import { connect, gatewayBase, needConnect } from "../core/api.ts";

  let url = $state(gatewayBase() || "http://localhost:8100");
  let key = $state("");
  let busy = $state(false), problem = $state("");
  let first: HTMLInputElement | undefined = $state();
  $effect(() => { if ($needConnect) first?.focus(); });

  async function submit(e: SubmitEvent) {
    e.preventDefault();
    busy = true; problem = "";
    try { await connect(url, key); location.reload(); }
    catch (err) { problem = (err as Error).message; busy = false; }
  }
</script>

{#if $needConnect}
  <div class="scrim"></div>
  <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="connectTitle"><form onsubmit={submit}>
    <h2 id="connectTitle">つなぐ先</h2>
    <p>Research-Agent のゲートウェイの住所と鍵を入れてください。</p>
    <label><span>住所</span>
      <input type="url" id="gatewayUrl" placeholder="http://localhost:8100 / https://133.2.x.x" autocomplete="off" spellcheck="false"
        bind:value={url} bind:this={first}></label>
    <small>この PC の Docker: http://localhost:8100 · 研究室の入口: https://（その PC の住所）</small>
    <label><span>鍵</span>
      <input type="password" id="connectKey" autocomplete="off" spellcheck="false" placeholder="AGENT_API_KEY（なければ空）" bind:value={key}></label>
    {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
    <div class="btns">
      {#if gatewayBase()}<button type="button" onclick={() => needConnect.set(false)}>やめる</button>{/if}
      <button type="submit" class="primary" disabled={busy || !url.trim()}>{busy ? "確かめています…" : "確かめてつなぐ"}</button>
    </div>
  </form></div>
{/if}

<style>
  .scrim { position: fixed; inset: 0; z-index: 98; background: rgb(0 0 0 / .35); }
  .dialog {
    position: fixed; z-index: 99; left: 50%; top: 28%; transform: translate(-50%, -28%); width: min(420px, calc(100vw - 32px));
    background: var(--bg); color: var(--ink); border: 1px solid var(--line); border-radius: 12px; padding: 16px;
    box-shadow: 0 10px 30px rgb(0 0 0 / .25);
  }
  form { display: grid; gap: 9px; }
  h2 { margin: 0; font-size: 15px; }
  p, small { margin: 0; font-size: 12px; color: var(--muted); }
  label { display: grid; gap: 3px; font-size: 12.5px; }
  input { font: inherit; padding: 7px 9px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--ink); }
  .problem { color: var(--ink); border-left: 2px solid var(--ink); padding-left: 8px; }
  .btns { display: flex; justify-content: flex-end; gap: 8px; }
  button { border: 1px solid var(--line); background: var(--surface); color: var(--ink); border-radius: 8px; padding: 5px 12px; cursor: pointer; }
  button.primary { background: var(--ink); color: var(--bg); border-color: var(--ink); }
  button:disabled { opacity: .5; cursor: default; }
</style>
