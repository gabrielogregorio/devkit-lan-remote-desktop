# TEST-PLAN-FRONTEND.md — testes do cliente `web/app.js`

> **Estado:** PARTE A (backend Python) concluída com 100% de cobertura.
> Este documento cobre apenas a **PARTE B** — testes do cliente web.
>
> **Meta:** Vitest (jsdom) com cobertura V8, meta ≥ 95% das linhas testáveis
> (algumas linhas de browser-only são legítimas com `/* v8 ignore */`),
> mais Playwright para validar WebCodecs/canvas em Chromium real.

---

## 0. Estado atual do `app.js`

O arquivo tem **551 linhas**, JS puro sem módulos (`"use strict"`, sem `export`).
Funções e variáveis vivem no escopo de módulo. Executa `boot()` no final.

### Mapa de funções/responsabilidades

| Grupo | Funções |
|---|---|
| WebCodecs vídeo | `hex2`, `findNALs`, `codecFromSPS`, `ensureDecoder`, `resetDecoder`, `onAU` |
| WebCodecs áudio | `startAudioContext`, `ensureAudioDecoder`, `resetAudioDecoder`, `playAudio`, `onAudioPacket` |
| Conexão | `connect`, `connectVideo`, `connectInput`, `connectAudio`, `setAudio`, `sendInput` |
| Mouse | `normFromEvent`, `flushMove` + listeners `mousemove/down/up/contextmenu/wheel` |
| Touch | listeners `touchstart/move/end` |
| Teclado | listeners `keydown/keyup` |
| Gamepad | `pollPads` + listener `gamepadconnected` |
| UI | `setStatus`, `charToCode`, `loadConfig` + listeners de botões/selects |
| Stats | `statsLoop` |
| Boot | `boot` |

### Variáveis de estado de módulo relevantes

```
videoWS, inputWS, decoder, configured, codecStr, tsCounter
frames, bytes, lastStat, lastFrameAt, decodeLatency
audioWS, audioCtx, audioDecoder, audioOn, playHead, audioTs, audioBytes, audioPkts
pendingMove, touchId, touchMoved, padSnap
```

---

## 1. Seam mínimo (única edição em `web/app.js`)

Substitua a linha final `boot();` por:

```js
// ---------- boot / test seam ----------
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    hex2, findNALs, codecFromSPS,
    ensureDecoder, resetDecoder, onAU,
    startAudioContext, ensureAudioDecoder, resetAudioDecoder,
    playAudio, onAudioPacket, connectAudio, setAudio,
    connect, connectVideo, connectInput, sendInput,
    flushMove, normFromEvent, pollPads,
    setStatus, charToCode, loadConfig, statsLoop, boot,
    __get: (k) => eval(k),  // lê variáveis de módulo no teste
  };
} else {
  boot();
}
```

> Em browser `module` não existe → `else { boot() }` roda como antes.
> Sob Vitest/Node (CommonJS), expõe as internals e **não** auto-executa.
> **Esta é a única edição permitida no cliente.**

---

## 2. Setup do ambiente

### 2.1 `package.json`

```json
{
  "name": "remote-link-web-tests",
  "private": true,
  "type": "commonjs",
  "scripts": {
    "test:web": "vitest run --coverage",
    "test:e2e": "playwright test"
  },
  "devDependencies": {
    "vitest": "^2.1.0",
    "@vitest/coverage-v8": "^2.1.0",
    "jsdom": "^25.0.0",
    "@playwright/test": "^1.48.0"
  }
}
```

```bash
npm install
npx playwright install chromium
```

### 2.2 `vitest.config.js`

```js
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["web/__tests__/**/*.test.js"],
    setupFiles: ["web/__tests__/setup.js"],
    coverage: {
      provider: "v8",
      include: ["web/app.js"],
      reporter: ["text", "html"],
      thresholds: { lines: 95, functions: 95, branches: 90, statements: 95 },
    },
  },
});
```

> Threshold de 95% (não 100%) porque: `requestPointerLock`, `requestFullscreen`,
> `exitFullscreen` e `webkitAudioContext` são linhas browser-only que o jsdom não
> implementa — marcadas com `/* v8 ignore next */` ou cobertas no Playwright.

---

## 3. `web/__tests__/setup.js` — harness DOM + stubs

```js
import { beforeEach, vi } from "vitest";

// IDs que app.js usa via $()
const IDS = [
  "screen", "overlay", "status", "stats", "connect", "lock", "full",
  "kbd", "menuToggle", "sound", "preset", "monitor", "audioPreset",
  "hiddenKbd", "bar", "audioGroup",
];

export function mountDOM() {
  document.body.innerHTML = "";
  for (const id of IDS) {
    const el =
      id === "screen" ? document.createElement("canvas") :
      ["preset", "monitor", "audioPreset"].includes(id) ? document.createElement("select") :
      id === "hiddenKbd" ? document.createElement("input") :
      document.createElement("div");
    el.id = id;
    document.body.appendChild(el);
  }
  const cv = document.getElementById("screen");
  cv.getContext = () => ({ drawImage: vi.fn() });
  cv.requestPointerLock = vi.fn();           // /* v8 ignore */
  cv.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1920, height: 1080 });
}

export function stubBrowser() {
  // WebSocket controlável
  globalThis.__sockets = [];
  globalThis.WebSocket = class {
    constructor(url) {
      this.url = url; this.readyState = 1; this.sent = [];
      globalThis.__sockets.push(this);
    }
    send(d) { this.sent.push(d); }
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
  };

  // VideoDecoder / AudioDecoder (WebCodecs)
  globalThis.VideoDecoder = class {
    constructor(cfg) { this.cfg = cfg; this.state = "unconfigured"; }
    configure(c) { this.state = "configured"; this.configured = c; }
    decode(chunk) { (this.decoded = this.decoded || []).push(chunk); }
    close() { this.state = "closed"; }
  };
  globalThis.AudioDecoder = class {
    constructor(cfg) { this.cfg = cfg; this.state = "unconfigured"; }
    configure(c) { this.state = "configured"; }
    decode(chunk) { (this.decoded = this.decoded || []).push(chunk); }
    close() { this.state = "closed"; }
  };
  globalThis.EncodedVideoChunk = class { constructor(o) { Object.assign(this, o); } };
  globalThis.EncodedAudioChunk = class { constructor(o) { Object.assign(this, o); } };

  // AudioContext
  globalThis.AudioContext = class {
    constructor() { this.state = "running"; this.currentTime = 0; this.destination = {}; }
    createBuffer(ch, n, sr) {
      return { duration: n / sr, copyToChannel: vi.fn() };
    }
    createBufferSource() { return { connect: vi.fn(), start: vi.fn(), buffer: null }; }
    resume() { this.state = "running"; }
    suspend() { this.state = "suspended"; }
  };

  // rAF: não reentra (evita loops infinitos)
  globalThis.requestAnimationFrame = vi.fn();
  globalThis.performance = { now: () => 0 };
  navigator.getGamepads = () => [];

  globalThis.fetch = vi.fn(async () => ({
    json: async () => ({
      presets: ["ultra", "high", "medium", "low", "potato"],
      current_preset: "high",
      monitors: [{ index: 0, name: "HDMI-0", primary: true, w: 1920, h: 1080 }],
      current_monitor: null,
      audio_available: false,
      audio_presets: ["alta", "media", "baixa", "voz"],
      current_audio_preset: "media",
    }),
  }));
}

// Carrega app.js limpo a cada teste (vi.resetModules garante estado fresco)
export async function loadApp() {
  globalThis.__sockets = [];
  vi.resetModules();
  return await import("../app.js");
}

beforeEach(() => {
  mountDOM();
  stubBrowser();
});
```

---

## 4. Testes Vitest — `web/__tests__/app.test.js`

Crie o arquivo com um `beforeEach` que carrega o app:

```js
import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadApp } from "./setup.js";

let app;
beforeEach(async () => { app = await loadApp(); });
```

> Use `app.__get("varName")` para ler estado de módulo.
> Use `app.functionName(...)` para chamar funções diretamente.

---

### 4.1 Funções puras

**`hex2`**
- `hex2(5) === "05"`, `hex2(255) === "ff"`, `hex2(0) === "00"`.

**`findNALs`**
- Buffer com 2 start codes `[0,0,1]` → retorna array com 2 entradas, cada uma com `type` (byte após) e `hdr` (índice do byte de tipo). Afirme `nals[0].type` e `nals[0].hdr`.
- Buffer sem start code → `[]`.
- Buffer que tem `[0,0,0,1]` (4-byte start code) → não deve falsa-detectar (só `[0,0,1]` é start code no parser).
- Cobre o `i += 2` (incremento do for loop ao encontrar um start code).

**`codecFromSPS`**
- Dado um AU sintético com SPS em posição conhecida (ex.: `au = new Uint8Array([0,0,1,0x67, 0x64, 0x00, 0x1f])`, `spsHdr = 3`), afirme que retorna `"avc1.640001f"` — use os bytes reais.
- Afirme formato `"avc1.xxxxxx"` (6 hex chars).

**`charToCode`**
- `"a"` → `"KeyA"`, `"Z"` → `"KeyZ"` (maiúsculas e minúsculas cobrem `/[a-zA-Z]/`).
- `"5"` → `"Digit5"`, `"0"` → `"Digit0"`.
- `" "` → `"Space"`.
- `"!"` → `null` (cobre o `return null` final).
- Caso de limite: `e` minúsculo → `"KeyE"` (cobre `ch.toUpperCase()`).

**`normFromEvent`**
- Evento com `clientX=960, clientY=540` e rect `{left:0, top:0, width:1920, height:1080}` → `{x:0.5, y:0.5}`.
- Clamp: `clientX = -100` → `x = 0`; `clientX = 9999` → `x = 1`.

---

### 4.2 WebCodecs vídeo

**`ensureDecoder`**
- Primeira chamada: cria `VideoDecoder`, estado `"unconfigured"` → após `configure`, `"configured"`.
- Segunda chamada com decoder já configurado (`state !== "closed"`): não recria (referência é a mesma).
- Com `decoder.state === "closed"`: recria.

**Callback `output` do VideoDecoder**
> Invocar o callback armazenado em `decoder.cfg.output` com um frame fake.

- Chama `drawImage` no ctx do canvas.
- Ajusta `canvas.width/height` quando diferentes de `frame.displayWidth/Height`.
- Chama `frame.close()`.
- Incrementa `frames` (`app.__get("frames")`).
- `decodeLatency`: na primeira chamada `lastFrameAt=0` → só seta `lastFrameAt`. Na segunda → calcula `decodeLatency` (cobre o `if (lastFrameAt)`).

**Callback `error` do VideoDecoder**
- Invocar `decoder.cfg.error(new Error("x"))` → chama `resetDecoder` (afirme `app.__get("decoder") === null`).

**`resetDecoder`**
- Com decoder configurado: chama `decoder.close()`, zera `decoder`, `configured`, `codecStr`.
- Com `decoder.close()` lançando (state "closed" + throw): `try/catch` silencia, decoder fica null.
- Com `decoder === null`: não levanta.

**`onAU`**
- AU sem SPS e `configured=false` → retorna sem chamar `decode` (decoder nunca criado).
- AU com SPS (NAL type 7) + IDR (NAL type 5): configura decoder, `configured=true`, chama `decode` com `type:"key"`.
- AU com apenas NAL type 1 após `configured=true`: chama `decode` com `type:"delta"`.
- `decode` lançando: cai no `catch`, chama `resetDecoder` (`decoder=null`).
- Acumula `bytes`: chame 2x e afirme `app.__get("bytes")` cresceu.

---

### 4.3 WebCodecs áudio

**`startAudioContext`**
- Primeira chamada: cria `AudioContext` global.
- Segunda chamada com `state==="running"`: não recria.
- Com `state==="suspended"`: chama `resume()`.
- Fallback `webkitAudioContext`: force `window.AudioContext = undefined`, defina `window.webkitAudioContext = AudioContext` (stub), afirme que cria. *(Marque com `/* v8 ignore next */` se o jsdom não permitir deletar AudioContext.)*

**`ensureAudioDecoder`**
- Cria `AudioDecoder` e chama `configure({codec:"opus",...})`.
- Segunda chamada sem fechar: não recria.
- Com `state==="closed"`: recria.

**Callback `error` do AudioDecoder**
- Invocar `audioDecoder.cfg.error(new Error("x"))` → chama `resetAudioDecoder`.

**`resetAudioDecoder`**
- Fecha decoder se aberto, zera `audioDecoder` e `playHead`.
- Com `close()` lançando: silencia.
- Com `audioDecoder === null`: não levanta.

**`playAudio`**
- Com `audioCtx === null`: chama `data.close()` e retorna (afirme `src.start` não chamado).
- Normal: cria buffer, itera canais, chama `copyTo` e `buf.copyToChannel`, `src.start(playHead)`, avança `playHead`.
- `copyTo` lançando na primeira chamada (com opts): cai no `catch`, usa `copyTo(tmp, {planeIndex})` sem format.
- Reancoragem atrasada: `playHead = 0`, `audioCtx.currentTime = 1.0` → `playHead < now + 0.005` → reancora (`playHead = now + JITTER`).
- Reancoragem rajada: `playHead = audioCtx.currentTime + JITTER + 0.5` → `> now + JITTER + 0.12` → reancora.

**`onAudioPacket`**
- Decodifica com `EncodedAudioChunk({type:"key", ...})` e incrementa `audioTs` em 20000.
- `decode` lançando → chama `resetAudioDecoder`.
- Acumula `audioBytes` e `audioPkts`.

---

### 4.4 Conexão e áudio

**`connectAudio`**
- Com `audioOn=false`: retorna sem criar WS (afirme `__sockets` não cresceu).
- Com `audioOn=true`: cria WS `/ws/audio`.
  - `ws.onmessage({data: arrbuf})` com `audioOn=true` → chama `onAudioPacket`.
  - `ws.onclose()` com `audioOn=true` → reconecta após setTimeout (afirme novo socket após `vi.runAllTimers()`).
  - `ws.onclose()` com `audioOn=false` → não reconecta.
  - `ws.onerror()` → chama `ws.close()`.
  - `ws.onerror()` com `ws.close()` lançando: cobre o `try/catch`.

**`setAudio(true)`**
- `audioGroup.hidden = false`.
- Texto do botão `"🔊 som"`, classe `"on"` adicionada.
- Chama `startAudioContext` (audioCtx criado).
- Cria WS de áudio (`connectAudio`).

**`setAudio(false)`**
- `audioGroup.hidden = true`.
- Texto `"🔇 som"`, classe `"on"` removida.
- Fecha `audioWS` se existe; cobre o `try/catch` do `audioWS.close()`.
- Chama `resetAudioDecoder`.
- Chama `audioCtx.suspend()` se `audioCtx` existe.
- Com `audioCtx === null`: não levanta.

---

### 4.5 Conexão vídeo/input

**`connect`**
- Remove classe `"hidden"` do overlay.
- Cria dois sockets (`__sockets.length === 2`): `/ws/video` e `/ws/input`.

**`connectVideo`**
- Cria WS `/ws/video` com `binaryType="arraybuffer"`.
- `onmessage` com string `{"t":"reset"}` → chama `resetDecoder`.
- `onmessage` com string JSON inválida → silencia (cobre `catch`).
- `onmessage` com string JSON válida mas `t !== "reset"` → retorna sem chamar resetDecoder (cobre o path "string mas não reset").
- `onmessage` com `ArrayBuffer` → chama `onAU`.
- `onclose` → chama `resetDecoder`, seta status, agenda reconnect via `setTimeout`.
- `onclose` com `visibilityState="visible"` → `connectVideo` é chamado após timer.
- `onclose` com `visibilityState="hidden"` → `connectVideo` NÃO é chamado (cobre o `if visible`).
- `onerror` → chama `videoWS.close()`.

**`connectInput`**
- Cria WS `/ws/input`.
- `onclose` → agenda `connectInput` via `setTimeout`.

**`sendInput`**
- Com `inputWS.readyState === 1`: chama `ws.send(JSON.stringify(obj))`.
- Com `inputWS === null`: não levanta.
- Com `inputWS.readyState !== 1`: não envia.

---

### 4.6 Mouse

**`normFromEvent`** — já em 4.1.

**Listener `mousemove`**
- Sem pointer lock: seta `pendingMove = {t:"m", x, y}`.
- Com `document.pointerLockElement === canvas`: envia `{t:"mr", dx, dy}` via `sendInput`.

**`flushMove`**
- Com `pendingMove` setado: chama `sendInput(pendingMove)` e zera `pendingMove`.
- Com `pendingMove === null`: não chama `sendInput`.
- Em ambos os casos chama `requestAnimationFrame(flushMove)`.

**Listener `mousedown`**
- `dispatchEvent(new MouseEvent("mousedown", {button:2}))` → `sendInput({t:"md",b:2})`.
- Chama `preventDefault`.

**Listener `mouseup`**
- Idem com `"mouseup"`.

**Listener `contextmenu`**
- Chama `preventDefault` (sem sendInput).

**Listener `wheel`**
- `dispatchEvent(new WheelEvent("wheel", {deltaX:5, deltaY:-3}))` → `sendInput({t:"w",x:1,y:-1})`.
- `deltaX=0, deltaY=0` → `x:0, y:0`.
- Chama `preventDefault`.

---

### 4.7 Touch

> Use `vi.useFakeTimers()` no `beforeEach` destes testes para controlar o `setTimeout(40)`.

**`touchstart`**
- 1 toque → envia `{t:"m",x,y}`, `touchMoved=false`.
- `e.touches.length !== 1` → retorna (nenhum sendInput).
- Chama `preventDefault`.

**`touchmove`**
- Toque com `identifier === touchId` → `touchMoved=true`, seta `pendingMove`.
- Toque com identifier diferente → nenhum efeito.
- Chama `preventDefault`.

**`touchend`**
- `touchMoved=false`: envia `{t:"md",b:0}`, depois `{t:"mu",b:0}` após `setTimeout(40)`.
- `touchMoved=true`: não envia botões.
- `touchId=null` após.
- Chama `preventDefault`.

---

### 4.8 Teclado

**Listener `keydown`**
- Com `inputWS.readyState === 1`: envia `{t:"k",code:"KeyA",down:true}`.
- `e.code === "F11"` → retorna sem enviar.
- `inputWS === null` → retorna.
- `readyState !== 1` → retorna.
- Chama `preventDefault` (quando envia).

**Listener `keyup`**
- Mesmo padrão com `down:false`.

---

### 4.9 Gamepad

**`pollPads`**
- `getGamepads()` retorna `[]` → nenhum `sendInput`, chama `requestAnimationFrame`.
- 1 pad com axes e buttons: envia `{t:"g",a:[...],b:[...]}`.
- Chamado 2x com estado idêntico: envia só 1x (deduplica via `padSnap`).
- Estado mudado: envia de novo.
- `p.axes[i]` com `0` (falsy) → usa `|| 0` (cobre o fallback).

**Listener `gamepadconnected`**
- `dispatchEvent(new Event("gamepadconnected"))` com `e.gamepad.id = "Xbox 360 Controller (XInput STANDARD GAMEPAD)"` → `setStatus` chamado com primeiros 28 chars.

---

### 4.10 UI — botões e selects

**`setStatus`**
- `setStatus("msg")` → `$("status").textContent === "msg"`.

**Botão `#connect`**
- Click → chama `connect` (afirme `__sockets.length === 2`).

**Botão `#lock`** *(v8 ignore)*
- Click → chama `canvas.requestPointerLock()`. *(Marque linha com `/* v8 ignore next */`)*

**Botão `#full`** *(v8 ignore)*
- `document.fullscreenElement === null` → chama `requestFullscreen`. *(v8 ignore next)*
- `document.fullscreenElement !== null` → chama `exitFullscreen`. *(v8 ignore next)*

**Botão `#kbd`**
- Click → chama `$("hiddenKbd").focus()`.

**Botão `#menuToggle`**
- Click → toggle da classe `"hidden"` no `$("bar")`.

**Botão `#sound`**
- Click → chama `setAudio(!audioOn)`.

**Select `#preset` change**
- `$("preset").dispatchEvent(new Event("change"))` com `value="low"` → `sendInput({t:"q",preset:"low"})`.

**Select `#monitor` change**
- `value = "1"` → `sendInput({t:"mon",i:1})`.
- `value = ""` → `sendInput({t:"mon",i:null})` (cobre o ternário `v===""?null:Number(v)`).

**Select `#audioPreset` change**
- `value = "alta"` → `sendInput({t:"aq",preset:"alta"})`.

**Input `#hiddenKbd`**
- `e.data = "a"` → envia `keydown + keyup` via `sendInput`.
- `e.data = "!"` → `charToCode` retorna null → nenhum sendInput.
- `e.data = null` → `if (ch)` falso → nenhum sendInput.
- Zera `e.target.value = ""` sempre.

---

### 4.11 `loadConfig`

**Normal**
- `fetch` retornando config com 5 presets, 1 monitor, áudio disponível com `AudioDecoder` presente:
  - `#preset` tem 5 options, `selected` no current.
  - `#monitor` tem 1 option.
  - `#sound.disabled === false`.
  - `#audioPreset` tem as opções de áudio, `selected` no current.

**Áudio indisponível no servidor** (`audio_available=false`)
- `sbtn.disabled = true`.
- `sbtn.title` contém `"sem áudio no servidor"`.

**Áudio disponível mas sem `AudioDecoder`** (`audio_available=true` mas `delete globalThis.AudioDecoder`)
- `sbtn.disabled = true`.
- `sbtn.title` contém `"AudioDecoder"`.

**`fetch` lançando**
- Afirme que `console.warn` foi chamado (cobre o `catch`).
- Nenhum erro propagado.

---

### 4.12 `statsLoop`

**`dt >= 1`** (performance.now cresceu ≥ 1000ms)
- Configure `performance.now` para retornar `lastStat + 1500`.
- Force `frames=10, bytes=500000, audioOn=true, audioBytes=1000`.
- Chame `statsLoop()` manualmente.
- `$("stats").textContent` contém fps, Mbps e `"🔊"` (branch audioOn).
- Contadores zerados.

**`dt < 1`**
- `performance.now` retorna `lastStat + 100`.
- `$("stats").textContent` não muda.

Ambos chamam `requestAnimationFrame`.

---

### 4.13 `boot`

**Sem WebCodecs** (`delete globalThis.VideoDecoder` antes de `loadApp()`)
- Chamar `app.boot()` → `$("status").textContent` contém `"Sem WebCodecs"`.
- `$("connect").disabled === true`.
- `loadConfig` NÃO chamado (fetch não chamado).
- `requestAnimationFrame` NÃO chamado.

**Normal** (VideoDecoder presente)
- `$("status").textContent` contém `"pronto"`.
- `fetch` chamado (loadConfig).
- `requestAnimationFrame` chamado (statsLoop + pollPads + flushMove).

---

### 4.14 Linhas marcadas `/* v8 ignore next */`

Adicione `/* v8 ignore next */` antes de cada uma no `app.js` (além do seam final):

| Linha original | Razão |
|---|---|
| `canvas.requestPointerLock()` | API não implementada no jsdom |
| `document.documentElement.requestFullscreen()` | Idem |
| `document.exitFullscreen()` | Idem |
| `new (...window.webkitAudioContext)(...)` | Só Safari; jsdom não tem |

> Com jsdom 25, `requestFullscreen` e `requestPointerLock` podem ser stubbados no
> setup — prefira isso a ignorar. Use `/* v8 ignore */` apenas como último recurso.

---

## 5. Gate Vitest

```bash
npm run test:web
# Espera: web/app.js lines >= 95%, functions >= 95%, branches >= 90%
```

---

## 6. Playwright — `web/e2e/smoke.spec.js`

### 6.1 `playwright.config.js`

```js
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "web/e2e",
  timeout: 30000,
  use: {
    baseURL: "https://127.0.0.1:18443",
    ignoreHTTPSErrors: true,
    headless: true,
  },
  webServer: {
    command: "RL_GUARDIAN=off RL_PORT=18443 RL_CERT=certs/cert.pem RL_KEY=certs/key.pem ./.venv/bin/python -m server.main",
    url: "https://127.0.0.1:18443",
    ignoreHTTPSErrors: true,
    reuseExistingServer: true,
    timeout: 20000,
  },
});
```

### 6.2 `web/e2e/smoke.spec.js`

```js
import { test, expect } from "@playwright/test";

test("carrega UI, popula presets e monitores", async ({ page }) => {
  await page.goto("/");
  // status muda de vazio para "pronto"
  await expect(page.locator("#status")).not.toHaveText("");
  // select de preset foi populado pelo loadConfig
  await expect(page.locator("#preset option")).not.toHaveCount(0);
  // connect está habilitado (WebCodecs disponível no Chromium)
  await expect(page.locator("#connect")).not.toBeDisabled();
});

test("conecta e o canvas recebe frames reais (WebCodecs)", async ({ page }) => {
  test.skip(!process.env.DISPLAY, "precisa de X11 para ffmpeg capturar tela");
  await page.goto("/");
  await page.click("#connect");
  // canvas ganha dimensões quando o primeiro keyframe é decodificado
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
  // seleciona um preset diferente
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
  // aguarda loadConfig popular o estado de áudio
  await page.waitForTimeout(500);
  const disabled = await btn.isDisabled();
  if (!disabled) {
    await btn.click();
    await expect(page.locator("#audioGroup")).toBeVisible();
    await btn.click();
    await expect(page.locator("#audioGroup")).toBeHidden();
  }
});
```

---

## 7. Ordem de implementação

1. **Seam** no `web/app.js` (linha `boot();`) + `/* v8 ignore */` nas 4 linhas browser-only.
2. `package.json` + `vitest.config.js` + `npm install`.
3. `web/__tests__/setup.js`.
4. `web/__tests__/app.test.js` — funções puras (4.1) → video (4.2) → áudio (4.3) → conexão (4.4–4.5) → eventos (4.6–4.9) → UI (4.10–4.12) → boot (4.13).
5. Rodar `npm run test:web` após cada grupo para verificar cobertura incremental.
6. `playwright.config.js` + `web/e2e/smoke.spec.js`.
7. Rodar `npm run test:e2e` no host com X11/GPU.

---

## 8. `.gitignore` — adicionar

```
node_modules/
coverage/
playwright-report/
test-results/
```
