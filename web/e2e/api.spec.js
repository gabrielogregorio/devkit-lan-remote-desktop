/**
 * E2E of the HTTP contract against the REAL server (playwright.config.js starts
 * server/main.py). There is no mock here: if the /api/monitors format changes
 * without the client knowing, these tests fail.
 */
import { test, expect } from "@playwright/test";

test("/api/monitors returns everything the UI needs to build the controls", async ({ request }) => {
  const cfg = await (await request.get("/api/monitors")).json();

  expect(Array.isArray(cfg.presets)).toBe(true);
  expect(cfg.presets).toContain("high");
  expect(cfg.presets).toContain(cfg.current_preset);

  // independent video controls
  expect(Object.keys(cfg.video).sort()).toEqual(["bitrate", "fps", "res"]);
  expect(cfg.res_options.length).toBeGreaterThan(0);
  expect(cfg.fps_options).toContain(cfg.video.fps);
  expect(cfg.bitrate_options).toContain(cfg.video.bitrate);
  expect(cfg.res_options).toContain(cfg.video.res);

  // audio
  expect(cfg.audio_presets).toContain(cfg.current_audio_preset);
  expect(typeof cfg.audio_available).toBe("boolean");
  expect(cfg.audio_manual).toBe(false);

  expect(["nvenc", "vaapi", "x264"]).toContain(cfg.encoder);
  expect(Array.isArray(cfg.monitors)).toBe(true);
});

test("gamepad profile: save, read and delete", async ({ request }) => {
  const id = `e2e-pad-${Date.now()}`;
  const profile = { buttons: { a: 1 }, axes: { lx: 0 }, triggers: {} };

  expect((await request.post("/api/gamepad-map", { data: { id, map: profile } })).ok()).toBe(true);
  const all = await (await request.get("/api/gamepad-map")).json();
  expect(all[id]).toEqual(profile);

  const del = await (await request.delete("/api/gamepad-map", { data: { id } })).json();
  expect(del).toEqual({ ok: true, removed: true });
  expect((await (await request.get("/api/gamepad-map")).json())[id]).toBeUndefined();
});

test("an invalid gamepad profile is refused with 400 (and does not take the server down)", async ({ request }) => {
  expect((await request.post("/api/gamepad-map", { data: { id: "", map: {} } })).status()).toBe(400);
  expect((await request.post("/api/gamepad-map", { data: { id: "x", map: "not-a-dict" } })).status()).toBe(400);
  // valid JSON that is not an object (this used to be a 500)
  expect((await request.post("/api/gamepad-map", { data: "i-am-a-string" })).status()).toBe(400);
  expect((await request.post("/api/gamepad-map", { data: [1, 2] })).status()).toBe(400);
  expect((await request.delete("/api/gamepad-map", { data: [1, 2] })).status()).toBe(400);
  // truly broken JSON
  expect((await request.post("/api/gamepad-map", {
    headers: { "Content-Type": "application/json" }, data: Buffer.from("{{{"),
  })).status()).toBe(400);
  // still up
  expect((await request.get("/api/monitors")).ok()).toBe(true);
});

test("the gamepad diagnostics page is served", async ({ page }) => {
  await page.goto("/gamepad-debug.html");
  await expect(page.locator("h1")).toHaveText("Gamepad diagnostics");
  await expect(page.locator("#out")).toBeVisible();
});

test("the diagnostics page follows ?lang=pt-BR", async ({ page }) => {
  await page.goto("/gamepad-debug.html?lang=pt-BR");
  await expect(page.locator("h1")).toHaveText("Diagnóstico de gamepad");
  await expect(page.locator("html")).toHaveAttribute("lang", "pt-BR");
});

test("garbage on /ws/input does not take the channel down", async ({ page }) => {
  await page.goto("/");
  const result = await page.evaluate(async () => {
    const ws = new WebSocket(`wss://${location.host}/ws/input`);
    await new Promise((r) => (ws.onopen = r));
    for (const junk of ['not json', "[]", "5", '{"t":"vq","res":"abc"}',
                        '{"t":"q","preset":["low"]}', '{"t":"mon","i":"x"}']) {
      ws.send(junk);
    }
    ws.send(JSON.stringify({ t: "k", code: "ShiftLeft", down: false }));
    await new Promise((r) => setTimeout(r, 500));
    return ws.readyState;                       // 1 = OPEN
  });
  expect(result).toBe(1);
});
