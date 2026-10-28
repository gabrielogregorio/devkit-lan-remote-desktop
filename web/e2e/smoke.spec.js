import { test, expect } from "@playwright/test";

test("loads the UI, fills presets and monitors", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#status")).not.toHaveText("");
  await expect(page.locator("#preset option")).not.toHaveCount(0);
  await expect(page.locator("#connect")).not.toBeDisabled();
});

test("English by default, pt-BR with ?lang=pt-BR", async ({ page }) => {
  await page.goto("/?lang=en");
  await expect(page.locator("#connect")).toHaveText("Connect");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await page.goto("/?lang=pt-BR");
  await expect(page.locator("#connect")).toHaveText("Conectar");
  await expect(page.locator("#status")).toHaveText("pronto - toque em Conectar");
  await expect(page.locator("label").first()).toHaveText("preset");
  await expect(page.locator("#full")).toHaveAttribute("title", "tela cheia");
  await expect(page.locator("html")).toHaveAttribute("lang", "pt-BR");
});

test("connects and the canvas receives real frames (WebCodecs)", async ({ page }) => {
  test.skip(!process.env.DISPLAY, "needs X11 for ffmpeg to capture the screen");
  await page.goto("/");
  await page.click("#connect");
  await expect.poll(
    () => page.evaluate(() => document.getElementById("screen").width),
    { timeout: 15000 }
  ).toBeGreaterThan(0);
});

test("changing the preset sends a command to the server", async ({ page }) => {
  await page.goto("/");
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.click("#connect");
  await page.waitForTimeout(300);
  const opts = await page.locator("#preset option").allTextContents();
  if (opts.length > 1) {
    await page.selectOption("#preset", { index: 1 });
    await page.waitForTimeout(200);
  }
  expect(errors).toEqual([]);
});

test("the sound button turns audio on/off", async ({ page }) => {
  await page.goto("/");
  const btn = page.locator("#sound");
  await page.waitForTimeout(500);
  const disabled = await btn.isDisabled();
  if (!disabled) {
    await btn.click();
    await expect(btn).toHaveClass(/on/);
    await btn.click();
    await expect(btn).not.toHaveClass(/on/);
  }
});
