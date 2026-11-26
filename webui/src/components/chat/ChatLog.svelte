<!-- チャットの吹き出し（状態と変え方は panels/chat.ts）。頭には開いている調査のモデル・設定・費用を出す（押すと履歴の詳細） -->
<script lang="ts">
  import { tick } from "svelte";
  import { messages, runMeta, scrollRequest, hooks } from "../../panels/chat.ts";
  import { active } from "../../core/events.ts";
  import ReportActions from "./ReportActions.svelte";

  let { onMeta }: { onMeta: (button: HTMLElement) => void } = $props();
  let end: HTMLElement | undefined = $state();

  // 下まで送る（入れ物は #chat。この部品はその中身）
  $effect(() => {
    void $scrollRequest;
    tick().then(() => { const box = end?.parentElement; if (box) box.scrollTop = box.scrollHeight; });
  });

  // 出典番号: 押したら、拡張の部品（cite）がその出典を図の中で見せる。拡張がなければ押せない
  function cites(node: HTMLElement, _html?: string) {
    const apply = () => {
      const ext = active().find((x) => x.cite);
      node.querySelectorAll<HTMLButtonElement>(".cite").forEach((b) => { b.disabled = !ext; });
    };
    apply();
    return { update: apply };
  }
  function cite(ev: MouseEvent) {
    const b = (ev.target as Element).closest<HTMLElement>(".cite");
    if (b) active().find((x) => x.cite)?.cite?.(+b.dataset.src!);
  }
</script>

{#if $runMeta}
  <button type="button" class="runmeta" id="runMeta" title="この調査の設定を見る" onclick={(e) => onMeta(e.currentTarget)}>
    {#if $runMeta.models}<span id="runModels" title={$runMeta.modelsTitle || undefined}>{$runMeta.models}</span>{/if}
    {#if $runMeta.settings}<span>{$runMeta.settings}</span>{/if}
    <span class="cost mono" id="runCost">{$runMeta.cost}</span><span class="more">詳細</span>
  </button>
{/if}
{#each $messages as m (m.id)}
  {#if m.who === "u"}
    <div class="u">{m.text}</div>
  {:else}
    <div class="a">
      <div class="avatar">R</div>
      <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions (出典番号はボタン。押した場所をここで受ける) -->
      <div class="body" use:cites={m.html} onclick={cite}>
        {#if m.status != null && !m.html}
          <div class="status"><span class="dots"><i></i><i></i><i></i></span><span class="txt">{m.status}</span></div>
        {/if}
        {#if m.html}{@html m.html}{/if}
        {#if m.question}
          <div class="question">{m.question.text}{#if m.question.opts.length}<div class="opts">
            {#each m.question.opts as o (o.label)}
              <button type="button" data-a={o.a} disabled={m.question.readOnly} title={m.question.readOnly ? "履歴の再生中は答えられません" : undefined}
                onclick={() => hooks.sendAnswer(o.a)}>{o.label}</button>
            {/each}
          </div>{/if}</div>
        {/if}
        {#if m.done && m.report}<ReportActions report={m.report} />{/if}
        {#if m.saved}<p class="q saved">報告と成果物を保存した</p>{/if}
      </div>
    </div>
  {/if}
{/each}
<span bind:this={end} hidden></span>
