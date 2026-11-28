// 画面を通しで動かす確認（Playwright）。`npm run e2e`。
// ゲートウェイ・エージェント・LLM は使わない: /api の返事はテストの中で決める（e2e/mock.ts）。
// - app: Vite の開発サーバー（5173。npm run dev が動いていれば、それを使う。Vite はプロジェクトごとに 1 つまで）
// - pwa: ビルドした画面（vite preview の 4173）。Service Worker はビルドしたときだけ作られるため
// ブラウザは、この PC の Chrome を使う（channel: "chrome"）。Chrome のない環境では `npx playwright install chromium` の後、
// PW_BUNDLED=1 を付けて動かす
import { defineConfig } from "@playwright/test";

const DEV = 5173, PREVIEW = 4173;

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  reporter: "list",
  // 1 つずつ動かす（並べて動かすと、通知の確認がときどき通知を受け取れなかった。原因は突き止めていない。1 つずつでも 30 秒ほど）
  workers: 1,
  use: {
    channel: process.env.PW_BUNDLED ? undefined : "chrome",
    viewport: { width: 1500, height: 900 },
  },
  projects: [
    { name: "app", testIgnore: /pwa\.spec/, use: { baseURL: `http://localhost:${DEV}/ui/` } },
    { name: "pwa", testMatch: /pwa\.spec/, use: { baseURL: `http://localhost:${PREVIEW}/ui/` } },
  ],
  webServer: [
    { command: `npx vite --port ${DEV} --strictPort`, url: `http://localhost:${DEV}/ui/`, reuseExistingServer: true, timeout: 60_000 },
    { command: `npx vite build && npx vite preview --port ${PREVIEW} --strictPort`, url: `http://localhost:${PREVIEW}/ui/`,
      reuseExistingServer: false, timeout: 120_000 },
  ],
});
