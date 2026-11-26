// 画面を通しで動かす: 終わった調査の再生・設定を適用して送る・狭い画面
import { expect, test } from "@playwright/test";
import { DONE_RUN, mockApi } from "./mock.ts";

test("終わった調査を開くと、報告・流れの図・木・履歴の印が出る", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await mockApi(page);
  await page.goto(`?run=${DONE_RUN.id}&speed=0`);
  const chat = page.locator("#chat");
  await expect(chat.locator(".u")).toHaveText("東京タワーの高さ");
  await expect(chat.locator(".a .body h4")).toHaveText("答え");
  await expect(chat.locator(".cite")).toBeEnabled();                     // 出典を見せる拡張がある
  await expect(page.locator("#runMeta")).toContainText("画像も探す: いいえ");
  await expect(page.locator("#runs .run.on")).toHaveAttribute("data-id", DONE_RUN.id);
  await expect(page.locator(".fnode")).not.toHaveCount(0);
  // 出典番号を押すと、そのページの詳細が出る
  await chat.locator(".cite").first().click();
  await expect(page.locator("#drawer")).toHaveClass(/open/);
  await expect(page.locator("#dBody")).toContainText("ja.wikipedia.org");
  // 木に切り替えても、ノードがある
  await page.getByRole("button", { name: "Tree" }).click();
  await expect(page.locator("#canvas .node")).not.toHaveCount(0);
  // 3 列の並び（部品の入れ物が列を取らない）
  const cols = await page.locator(".app").evaluate((el) => getComputedStyle(el).gridTemplateColumns.split(" ").length);
  expect(cols).toBe(3);
  expect(errors).toEqual([]);
});

test("設定で「すばやく」を適用して送ると、その 1 件だけに設定が付く", async ({ page }) => {
  const posted = await mockApi(page, []);
  await page.goto("");
  await expect(page.locator("#headSummary")).toContainText("標準");
  await page.locator("#settingsBtn").click();
  await expect(page.locator("#setTitle")).toBeFocused();
  await page.getByRole("button", { name: "すばやく" }).click();
  await page.getByRole("button", { name: "この依頼に適用" }).click();
  await expect(page.locator("#setPanel")).toHaveCount(0);
  await expect(page.locator("#input")).toBeFocused();
  await expect(page.locator("#headSummary")).toContainText("すばやく");
  await expect(page.locator("#headSummary")).toContainText("この依頼だけ");
  await page.locator("#input").fill("富士山の高さ");
  await page.locator("#input").press("Enter");
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0]).toMatchObject({ request: "富士山の高さ", agent: "research", settings: { max_seconds: 420, max_pages: 6 } });
  await expect(page.locator("#headSummary")).not.toContainText("この依頼だけ");   // 次の依頼には持ち越さない
});

test("狭い画面: 作業記録に切り替えられ、履歴の引き出しから詳細を開ける", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await mockApi(page);
  await page.goto(`?run=${DONE_RUN.id}&speed=0`);
  await expect(page.locator("#chat .a .body h4")).toHaveText("答え");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);   // 横にはみ出さない
  await page.getByRole("tab", { name: "作業記録" }).click();
  await expect(page.locator(".app")).toHaveClass(/show-tele/);
  await page.getByRole("tab", { name: "チャット" }).click();
  await page.locator("#runsBtn").click();
  await page.locator("#runs .more").first().click();
  await page.getByRole("menuitem", { name: "詳細を見る" }).click();
  await expect(page.locator("#setTitle")).toHaveText("履歴の詳細");
  await expect(page.locator("#setBody")).toContainText("東京タワーの高さ");
});
