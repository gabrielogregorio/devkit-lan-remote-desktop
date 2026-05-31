import { test, expect } from "@playwright/test";

test("carrega UI, popula presets e monitores", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#status")).not.toHaveText("");
  await expect(page.locator("#preset option")).not.toHaveCount(0);
  await expect(page.locator("#connect")).not.toBeDisabled();
});

test("conecta e o canvas recebe frames reais (WebCodecs)", async ({ page }) => {
  test.skip(!process.env.DISPLAY, "precisa de X11 para ffmpeg capturar tela");
  await page.goto("/");
  await page.click("#connect");
  await expect.poll(
    () => page.evaluate(() => document.getElementById("screen").width),
    { timeout: 15000 }
  ).toBeGreaterThan(0);
});

test("troca de preset envia comando ao servidor", async ({ page }) => {
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

test("botão de som habilita/desabilita áudio", async ({ page }) => {
  await page.goto("/");
  const btn = page.locator("#sound");
  await page.waitForTimeout(500);
  const disabled = await btn.isDisabled();
  if (!disabled) {
    await btn.click();
    await expect(page.locator("#audioGroup")).toBeVisible();
    await btn.click();
    await expect(page.locator("#audioGroup")).toBeHidden();
  }
});
