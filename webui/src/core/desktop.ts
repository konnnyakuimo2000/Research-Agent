// デスクトップアプリ（Tauri。webui/src-tauri）で動いているか。`npm run desktop:build` などで、--mode desktop で作った画面だけ true。
// ブラウザ（ゲートウェイの /ui）では false のまま、今までどおり同じ出どころの /api を呼ぶ
export const DESKTOP: boolean = (import.meta as { env?: { MODE?: string } }).env?.MODE === "desktop";

/** アプリの本体（Rust）の命令を呼ぶ（鍵の保管など）。デスクトップアプリのときだけ */
export async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke: call } = await import("@tauri-apps/api/core");
  return call<T>(cmd, args);
}
