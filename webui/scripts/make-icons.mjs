// アプリのアイコン（PNG）を public/icons/icon.svg から作る。`node scripts/make-icons.mjs`（アイコンを変えたときだけ）。
// この PC の Chrome で描いて保存する（Playwright。channel: "chrome"）
import { chromium } from "@playwright/test";
import { readFileSync } from "node:fs";

const svg = readFileSync(new URL("../public/icons/icon.svg", import.meta.url), "utf-8");
const SIZES = [["icon-192.png", 192], ["icon-512.png", 512], ["apple-touch-icon.png", 180]];

const browser = await chromium.launch({ channel: process.env.PW_BUNDLED ? undefined : "chrome" });
for (const [name, size] of SIZES) {
  const page = await browser.newPage({ viewport: { width: size, height: size } });
  await page.setContent(`<html><body style="margin:0">${svg.replace("<svg ", `<svg width="${size}" height="${size}" `)}</body></html>`);
  await page.screenshot({ path: new URL(`../public/icons/${name}`, import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1") });
  await page.close();
  console.log(name, size);
}
await browser.close();
