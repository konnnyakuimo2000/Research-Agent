// PWA（ビルドした画面）: アプリの情報・オフラインでも画面が開く・/api を覚えない
import { expect, test } from "@playwright/test";
import { DONE_RUN, mockApi } from "./mock.ts";

test("アプリの情報があり、Service Worker が画面を覚えてオフラインでも開く。/api は覚えない", async ({ page, context }) => {
  await mockApi(page);
  await page.goto("");
  await expect(page.locator("#runs .run")).toHaveCount(1);

  const manifest = await (await page.request.get("manifest.webmanifest")).json();
  expect(manifest).toMatchObject({ name: "Research-Agent", start_url: "/ui/", scope: "/ui/", display: "standalone" });
  expect(manifest.icons.map((i: { sizes: string }) => i.sizes)).toEqual(["192x192", "512x512", "512x512"]);

  // Service Worker が動き始め、この画面を受け持つまで待つ
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.reload();
  await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
  await expect(page.locator("#runs .run")).toHaveCount(1);

  // 覚えたものに /api がない（調査の一覧・記録は毎回ゲートウェイに聞く）
  const cached = await page.evaluate(async () => {
    const urls: string[] = [];
    for (const name of await caches.keys()) for (const req of await (await caches.open(name)).keys()) urls.push(req.url);
    return urls;
  });
  expect(cached.some((u) => u.includes("/ui/index.html"))).toBe(true);
  expect(cached.filter((u) => u.includes("/api/"))).toEqual([]);

  // オフライン: 画面は開き、一覧は読めないと出る（古い一覧を出さない）
  await context.setOffline(true);
  await page.unroute("**/api/**");
  await page.reload();
  await expect(page.locator(".brand")).toBeVisible();
  await expect(page.locator("#runs")).toContainText("一覧を読めません");
  await context.setOffline(false);
});

test("知らせを入れると、ライブの調査が終わったとき（画面が後ろ）に Service Worker が通知を出す", async ({ page, context }) => {
  await context.grantPermissions(["notifications"]);
  // 画面が後ろにある状態にする（ヘッドレスの Chrome では、ページはいつも前にあるため）
  await page.addInitScript(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.hasFocus = () => false;
  });
  await mockApi(page, [{ ...DONE_RUN, status: "running" }]);       // 動いている調査として開く（記録の最後に run.end）
  await page.goto("");
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.locator("#settingsBtn").click();
  await page.getByRole("tab", { name: "表示" }).click();
  await page.locator("#notifyToggle").check();
  await expect(page.locator("#setBody")).toContainText("画面が後ろにあるとき");
  await page.keyboard.press("Escape");
  await page.locator("#runs .run").first().click();
  await expect(page.locator("#chat .a .body h4")).toHaveText("答え");
  await expect.poll(() => page.evaluate(async () => {
    const reg = await navigator.serviceWorker.getRegistration("/ui/");
    return (await reg!.getNotifications()).map((n) => `${n.title} / ${n.body}`);
  }), { timeout: 15_000 }).toEqual(["報告ができました / 「東京タワーの高さ」"]);   // 全体のテストで PC が混んでいても待てるように
});
