// 画面のビルド。できたファイルはゲートウェイが /ui で配る場所（src/research_agent/ui/）に出す（git の外）。
// 開発中は `npm run dev`（http://localhost:5173/ui/）。/api はゲートウェイ（8100）へ渡す。
// 部品のテストは `npm test`（Vitest・jsdom。src/**/*.test.ts）。pytest も tests/test_webui_vitest.py から呼ぶ
//
// PWA（.claude/webui-framework-and-app-plan.md 第 1 段）: アプリの情報（manifest.webmanifest）と Service Worker（sw.js）を作る。
// - Service Worker が覚えるのは、ビルドした画面のファイルだけ。/api とイベントの流れ（SSE）は覚えず、毎回ゲートウェイに聞く
//   （古い調査の状態を出さないため）
// - 新しい版が届いても勝手に読み直さない（調査の途中で画面が変わらないように）。画面に「再読み込み」を出す（components/UpdateNotice）
// - Service Worker は HTTPS か localhost でしか動かない（研究室の LAN からは Caddy の HTTPS の入口を通す。docs/setup.md）
//
// デスクトップアプリ（--mode desktop。webui/src-tauri が使う）: 画面をアプリの中の / に置き（dist-desktop/）、
// Service Worker は作らない（アプリが画面を持つため）。API はつなぐ先（core/api.ts）へ
import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { VitePWA } from "vite-plugin-pwa";

const THEME = "#171717";

export default defineConfig(({ mode }) => ({
  base: mode === "desktop" ? "/" : "/ui/",
  plugins: [
    svelte(),
    VitePWA({
      disable: mode === "desktop",          // 作らない（app.ts の registerSW は何もしない形のまま使える）
      registerType: "prompt",
      injectRegister: false,              // 登録は app.ts（新しい版の知らせを画面に出すため）
      includeAssets: ["logo.svg", "icons/apple-touch-icon.png", "sw-notify.js"],
      manifest: {
        name: "Research-Agent",
        short_name: "Research-Agent",
        description: "公開の Web とデータベースを調べ、出典付きの報告を書く",
        lang: "ja",
        id: "/ui/",
        start_url: "/ui/",
        scope: "/ui/",
        display: "standalone",
        background_color: THEME,
        theme_color: THEME,
        icons: [
          { src: "icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
      workbox: {
        globPatterns: ["**/*.{js,css,html,svg,png,webmanifest}"],
        navigateFallback: "/ui/index.html",
        navigateFallbackDenylist: [/^\/api\//],
        cleanupOutdatedCaches: true,
        importScripts: ["sw-notify.js"],     // 調査の知らせを押したら画面に戻る（panels/notify.ts）
      },
    }),
  ],
  // テストでは Svelte のブラウザ用（mount できる方）を使う
  resolve: process.env.VITEST ? { conditions: ["browser"] } : undefined,
  build: { outDir: mode === "desktop" ? "dist-desktop" : "../src/research_agent/ui", emptyOutDir: true, target: "es2022" },
  server: { proxy: { "/api": { target: process.env.GATEWAY_URL || "http://localhost:8100", changeOrigin: true } } },
  test: { environment: "jsdom", include: ["src/**/*.test.ts"] },
}));
