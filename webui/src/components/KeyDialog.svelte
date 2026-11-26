<!-- ゲートウェイの鍵の欄。ゲートウェイが鍵を求めた（401）ときに出す。研究室で共有するとき、管理する人から鍵を聞いて入れる -->
<script lang="ts">
  import { needKey, saveKey } from "../core/api.ts";

  let key = $state("");
  let input: HTMLInputElement | undefined = $state();
  $effect(() => { if ($needKey) input?.focus(); });
  function submit(e: SubmitEvent) { e.preventDefault(); if (key.trim()) saveKey(key); }
</script>

{#if $needKey}
  <div class="keyscrim"></div>
  <div class="keydialog" role="dialog" aria-modal="true" aria-labelledby="keyTitle"><form onsubmit={submit}>
    <h2 id="keyTitle">ゲートウェイの鍵</h2>
    <p>この画面を使うには鍵が要ります。管理している人から聞いて入れてください（このブラウザに覚えます）。</p>
    <input type="password" id="gatewayKey" autocomplete="off" spellcheck="false" aria-label="ゲートウェイの鍵" bind:value={key} bind:this={input}>
    <div class="btns">
      <button type="button" onclick={() => needKey.set(false)}>閉じる</button>
      <button type="submit" class="primary" disabled={!key.trim()}>保存して開き直す</button>
    </div>
  </form></div>
{/if}

<style>
  .keyscrim { position: fixed; inset: 0; z-index: 96; background: rgb(0 0 0 / .3); }
  .keydialog {
    position: fixed; z-index: 97; left: 50%; top: 30%; transform: translate(-50%, -30%); width: min(380px, calc(100vw - 32px));
    background: var(--bg); color: var(--ink); border: 1px solid var(--line); border-radius: 12px; padding: 16px;
    box-shadow: 0 10px 30px rgb(0 0 0 / .25);
  }
  form { display: grid; gap: 10px; }
  h2 { margin: 0; font-size: 15px; }
  p { margin: 0; font-size: 12.5px; color: var(--muted); }
  input { font: inherit; padding: 7px 9px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--ink); }
  .btns { display: flex; justify-content: flex-end; gap: 8px; }
  button { border: 1px solid var(--line); background: var(--surface); color: var(--ink); border-radius: 8px; padding: 5px 12px; cursor: pointer; }
  button.primary { background: var(--ink); color: var(--bg); border-color: var(--ink); }
  button:disabled { opacity: .5; cursor: default; }
</style>
