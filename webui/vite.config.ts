// 画面のビルド。できたファイルはゲートウェイが /ui で配る場所（src/research_agent/ui/）に出す（git の外）。
// 開発中は `npm run dev`（http://localhost:5173/ui/）。/api はゲートウェイ（8100）へ渡す。
// 部品のテストは `npm test`（Vitest・jsdom。src/**/*.test.ts）。pytest も tests/test_webui_vitest.py から呼ぶ
import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  base: "/ui/",
  plugins: [svelte()],
  // テストでは Svelte のブラウザ用（mount できる方）を使う
  resolve: process.env.VITEST ? { conditions: ["browser"] } : undefined,
  build: { outDir: "../src/research_agent/ui", emptyOutDir: true, target: "es2022" },
  server: { proxy: { "/api": { target: process.env.GATEWAY_URL || "http://localhost:8100", changeOrigin: true } } },
  test: { environment: "jsdom", include: ["src/**/*.test.ts"] },
});
