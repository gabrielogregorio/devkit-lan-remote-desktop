/**
 * E2E do contrato HTTP contra o servidor DE VERDADE (o playwright.config.js
 * sobe o server/main.py). Aqui não tem mock nenhum: se o formato de
 * /api/monitors mudar sem o cliente saber, estes testes caem.
 */
import { test, expect } from "@playwright/test";

test("/api/monitors devolve tudo que a UI precisa pra montar os controles", async ({ request }) => {
  const cfg = await (await request.get("/api/monitors")).json();

  expect(Array.isArray(cfg.presets)).toBe(true);
  expect(cfg.presets).toContain("high");
  expect(cfg.presets).toContain(cfg.current_preset);

  // controles de vídeo independentes
  expect(Object.keys(cfg.video).sort()).toEqual(["bitrate", "fps", "res"]);
  expect(cfg.res_options.length).toBeGreaterThan(0);
  expect(cfg.fps_options).toContain(cfg.video.fps);
  expect(cfg.bitrate_options).toContain(cfg.video.bitrate);
  expect(cfg.res_options).toContain(cfg.video.res);

  // áudio
  expect(cfg.audio_presets).toContain(cfg.current_audio_preset);
  expect(typeof cfg.audio_available).toBe("boolean");
  expect(cfg.audio_manual).toBe(false);

  expect(["nvenc", "vaapi", "x264"]).toContain(cfg.encoder);
  expect(Array.isArray(cfg.monitors)).toBe(true);
});

test("perfil de gamepad: salva, lê e apaga", async ({ request }) => {
  const id = `e2e-pad-${Date.now()}`;
  const perfil = { buttons: { a: 1 }, axes: { lx: 0 }, triggers: {} };

  expect((await request.post("/api/gamepad-map", { data: { id, map: perfil } })).ok()).toBe(true);
  const todos = await (await request.get("/api/gamepad-map")).json();
  expect(todos[id]).toEqual(perfil);

  const del = await (await request.delete("/api/gamepad-map", { data: { id } })).json();
  expect(del).toEqual({ ok: true, removed: true });
  expect((await (await request.get("/api/gamepad-map")).json())[id]).toBeUndefined();
});

test("perfil de gamepad inválido é recusado com 400 (e não derruba o servidor)", async ({ request }) => {
  expect((await request.post("/api/gamepad-map", { data: { id: "", map: {} } })).status()).toBe(400);
  expect((await request.post("/api/gamepad-map", { data: { id: "x", map: "nao-e-dict" } })).status()).toBe(400);
  // JSON válido mas que não é objeto (já foi 500 aqui)
  expect((await request.post("/api/gamepad-map", { data: "sou-uma-string" })).status()).toBe(400);
  expect((await request.post("/api/gamepad-map", { data: [1, 2] })).status()).toBe(400);
  expect((await request.delete("/api/gamepad-map", { data: [1, 2] })).status()).toBe(400);
  // JSON quebrado de verdade
  expect((await request.post("/api/gamepad-map", {
    headers: { "Content-Type": "application/json" }, data: Buffer.from("{{{"),
  })).status()).toBe(400);
  // continua no ar
  expect((await request.get("/api/monitors")).ok()).toBe(true);
});

test("a página de diagnóstico de gamepad é servida", async ({ page }) => {
  await page.goto("/gamepad-debug.html");
  await expect(page.locator("h1")).toHaveText(/Diagn/);
  await expect(page.locator("#out")).toBeVisible();
});

test("lixo no /ws/input não derruba o canal", async ({ page }) => {
  await page.goto("/");
  const resultado = await page.evaluate(async () => {
    const ws = new WebSocket(`wss://${location.host}/ws/input`);
    await new Promise((r) => (ws.onopen = r));
    for (const lixo of ['nao é json', "[]", "5", '{"t":"vq","res":"abc"}',
                        '{"t":"q","preset":["low"]}', '{"t":"mon","i":"x"}']) {
      ws.send(lixo);
    }
    ws.send(JSON.stringify({ t: "k", code: "ShiftLeft", down: false }));
    await new Promise((r) => setTimeout(r, 500));
    return ws.readyState;                       // 1 = OPEN
  });
  expect(resultado).toBe(1);
});
