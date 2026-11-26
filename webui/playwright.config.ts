// 画面を通しで動かす確認（Playwright）。`npm run e2e`。
// ゲートウェイ・エージェント・LLM は使わない: Vite の開発サーバーで画面を出し、/api の返事はテストの中で決める（e2e/mock.ts）。
// ブラウザは、この PC の Chrome を使う（channel: "chrome"）。Chrome のない環境では `npx playwright install chromium` の後、
// PW_BUNDLED=1 を付けて動かす
import { defineConfig } from "@playwright/test";

const PORT = 5173;   // 開発サーバー（npm run dev）が動いていれば、それを使う（Vite はプロジェクトごとに 1 つまで）

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  reporter: "list",
  use: {
    baseURL: `http://localhost:${PORT}/ui/`,
    channel: process.env.PW_BUNDLED ? undefined : "chrome",
    viewport: { width: 1500, height: 900 },
  },
  webServer: {
    command: `npx vite --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}/ui/`,
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
