<!-- モデルの接続の印（窓口の一覧 API が答えたか） -->
<script lang="ts">
  import { modelStatus } from "../../stores/state.ts";

  let { id }: { id: string } = $props();
  const s = $derived($modelStatus.data?.models?.[id]);
  const title = $derived(s?.ok ? `一覧 API が ${s.seconds ?? "?"} 秒で応答` : s?.error || "状態を確かめていない");
</script>

<i class="dot" class:ok={s?.ok} class:ng={s && s.ok === false} {title}></i>
