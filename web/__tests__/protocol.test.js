/**
 * Testes de CONTRATO e de PROPRIEDADE do cliente.
 *
 * O app.test.js cobre exemplos ("apertei X, saiu Y"). Aqui a pergunta é outra:
 * *toda* mensagem que o cliente produz, sob qualquer entrada, continua sendo
 * algo que o servidor sabe receber?
 *
 * Isso importa porque as duas pontas são versionadas juntas mas evoluem em
 * arquivos diferentes: o dia em que app.js mandar um campo com NaN, ou um `t`
 * que o ws_input não conhece, ninguém percebe até o controle "parar sozinho".
 * O espelho deste arquivo no servidor é tests/test_fuzz_input.py.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadApp } from "./setup.js";

// ---- PRNG determinístico (falhou uma vez, falha sempre) -------------------
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}
const SEEDS = [1, 42, 777, 20260813];

// valores hostis que um controle/driver pode entregar
const CURSED = [
  undefined, null, NaN, Infinity, -Infinity, "0.5", "", true, false,
  {}, [], -5, 5, 1e308,
];

// Tipos que o servidor (server/main.py ws_input + inject.Injector.handle) trata.
const TIPOS_ACEITOS = new Set(["m", "mr", "md", "mu", "w", "k", "g", "q", "vq", "aq", "mon"]);

let app;
beforeEach(async () => {
  app = await loadApp();
});

function enviados() {
  const ws = globalThis.__sockets.find((s) => s.url.endsWith("/ws/input"));
  return (ws ? ws.sent : []).map((s) => JSON.parse(s));
}

// ───────────────────────── contrato das mensagens ─────────────────────────

describe("contrato: todo comando enviado é um comando que o servidor conhece", () => {
  it("os tipos usados no app são exatamente os documentados", () => {
    app.connect();
    // exercita todas as origens de mensagem que existem no cliente
    app.sendInput({ t: "m", x: 0.5, y: 0.5 });
    app.sendChord(app.COMBOS.altf4);
    document.getElementById("preset").value = "low";
    document.getElementById("preset").dispatchEvent(new Event("change"));
    document.getElementById("vres").value = "720";
    document.getElementById("vres").dispatchEvent(new Event("change"));
    document.getElementById("vfps").value = "60";
    document.getElementById("vfps").dispatchEvent(new Event("change"));
    document.getElementById("vbitrate").value = "7M";
    document.getElementById("vbitrate").dispatchEvent(new Event("change"));
    document.getElementById("audioPreset").value = "media";
    document.getElementById("audioPreset").dispatchEvent(new Event("change"));
    document.getElementById("monitor").value = "";
    document.getElementById("monitor").dispatchEvent(new Event("change"));

    const msgs = enviados();
    expect(msgs.length).toBeGreaterThan(5);
    for (const m of msgs) expect(TIPOS_ACEITOS.has(m.t)).toBe(true);
  });

  it("toda mensagem sobrevive ao JSON sem virar null/undefined", () => {
    app.connect();
    const pad = {
      id: "P", mapping: "standard",
      axes: [0.25, -0.25, 0, 1],
      buttons: Array.from({ length: 17 }, (_, i) => ({ pressed: i === 3, value: i === 3 ? 1 : 0 })),
    };
    navigator.getGamepads = () => [pad];
    app.pollPads();
    for (const m of enviados()) {
      for (const [k, v] of Object.entries(m)) {
        expect(v, `campo ${k} de t=${m.t}`).not.toBeNull();
        expect(v).not.toBeUndefined();
      }
    }
  });

  it("'vq' manda só o campo que mudou (o servidor preenche o resto)", () => {
    app.connect();
    document.getElementById("vfps").value = "30";
    document.getElementById("vfps").dispatchEvent(new Event("change"));
    const vq = enviados().find((m) => m.t === "vq");
    expect(Object.keys(vq).sort()).toEqual(["fps", "t"]);
    expect(typeof vq.fps).toBe("number");
  });

  it("'mon' manda null para o monitor primário (não string vazia)", () => {
    app.connect();
    const sel = document.getElementById("monitor");
    sel.innerHTML = "";
    const o = document.createElement("option");
    o.value = "";
    sel.appendChild(o);
    sel.value = "";
    sel.dispatchEvent(new Event("change"));
    const mon = enviados().find((m) => m.t === "mon");
    expect(mon.i).toBeNull();
  });
});

// ───────────────────────── gamepad: propriedades ─────────────────────────

function padAleatorio(rand, { standard } = {}) {
  const nAxes = Math.floor(rand() * 8);
  const nBtn = Math.floor(rand() * 20);
  const valor = () =>
    rand() < 0.4 ? CURSED[Math.floor(rand() * CURSED.length)] : rand() * 2 - 1;
  return {
    id: "Pad " + Math.floor(rand() * 3),
    mapping: standard ?? (rand() < 0.5 ? "standard" : ""),
    axes: Array.from({ length: nAxes }, valor),
    buttons: Array.from({ length: nBtn }, () =>
      rand() < 0.1 ? null : { pressed: rand() < 0.3, value: valor() }),
  };
}

function checaFrame(r) {
  expect(r.a).toHaveLength(4);
  for (const v of r.a) {
    expect(typeof v).toBe("number");
    expect(Number.isFinite(v)).toBe(true);
  }
  for (const b of r.b) {
    expect(typeof b.p).toBe("boolean");
    expect(Number.isFinite(b.v)).toBe(true);
    expect(b.v).toBeGreaterThanOrEqual(0);
    expect(b.v).toBeLessThanOrEqual(1);
  }
  for (const t of [r.lt, r.rt]) {
    expect(Number.isFinite(t)).toBe(true);
    expect(t).toBeGreaterThanOrEqual(0);
    expect(t).toBeLessThanOrEqual(1);
  }
}

describe("propriedade: mapPad sempre produz um frame válido", () => {
  for (const seed of SEEDS) {
    it(`controles aleatórios (semente ${seed}) nunca quebram o formato`, () => {
      const rand = rng(seed);
      for (let i = 0; i < 300; i++) checaFrame(app.mapPad(padAleatorio(rand)));
    });
  }

  it("com perfil de remap salvo apontando pra índices inexistentes", async () => {
    globalThis.fetch = vi.fn(async () => ({
      json: async () => ({
        "Pad 0": {
          buttons: { a: 99, b: -1, x: null },
          axes: { lx: 42, ly: 0 },
          triggers: { lt: { src: "axis", i: 77 }, rt: { src: "button", i: 55 } },
        },
      }),
    }));
    await app.loadGamepadMaps();
    const rand = rng(9);
    for (let i = 0; i < 200; i++) {
      checaFrame(app.mapPad(padAleatorio(rand, { standard: "" })));
    }
  });

  it("perfil corrompido (tipos errados) não derruba o mapeamento", async () => {
    globalThis.fetch = vi.fn(async () => ({
      json: async () => ({ "Pad 0": { buttons: "nao-e-objeto", axes: 5, triggers: null } }),
    }));
    await app.loadGamepadMaps();
    const p = {
      id: "Pad 0", mapping: "",
      axes: [0.5, 0.5, 0, 0],
      buttons: [{ pressed: true, value: 1 }],
    };
    checaFrame(app.mapPad(p));
  });

  it("pad sem arrays (driver estranho) vira frame neutro", () => {
    checaFrame(app.mapPad({ id: "X", mapping: "standard" }));
    checaFrame(app.mapPad({ id: "X", mapping: "standard", axes: null, buttons: null }));
  });

  it("botão com value ausente não explode (era TypeError em toFixed)", () => {
    const p = {
      id: "X", mapping: "standard",
      axes: [0, 0, 0, 0],
      buttons: [{ pressed: true }, {}, null, undefined],
    };
    const r = app.mapPad(p);
    checaFrame(r);
    expect(r.b[0]).toEqual({ p: true, v: 0 });
  });
});

describe("propriedade: readSource fica sempre em 0..1", () => {
  for (const seed of SEEDS) {
    it(`fontes aleatórias (semente ${seed})`, () => {
      const rand = rng(seed);
      for (let i = 0; i < 200; i++) {
        const p = padAleatorio(rand);
        const src = rand() < 0.5
          ? { src: "button", i: Math.floor(rand() * 25) }
          : {
            src: "axis", i: Math.floor(rand() * 10),
            min: rand() < 0.3 ? undefined : rand() * 2 - 1,
            max: rand() < 0.3 ? undefined : rand() * 2 - 1,
          };
        const v = app.readSource(p, src);
        expect(Number.isFinite(v)).toBe(true);
        expect(v).toBeGreaterThanOrEqual(0);
        expect(v).toBeLessThanOrEqual(1);
      }
    });
  }

  it("pad sem buttons/axes (driver estranho) devolve solto", () => {
    expect(app.readSource({}, { src: "button", i: 0 })).toBe(0);
    expect(app.readSource({}, { src: "axis", i: 0 })).toBe(0);
  });

  it("calibração degenerada (min == max) trata como solto", () => {
    const p = { axes: [0.7], buttons: [] };
    expect(app.readSource(p, { src: "axis", i: 0, min: 0.5, max: 0.5 })).toBe(0);
  });

  it("eixo invertido (repouso 0, fundo -1) é normalizado igual", () => {
    const p = { axes: [-1], buttons: [] };
    expect(app.readSource(p, { src: "axis", i: 0, min: 0, max: -1 })).toBe(1);
  });
});

describe("pollPads sobrevive a controle hostil (o loop não pode morrer)", () => {
  it("mapPad explodindo não impede o próximo quadro", () => {
    const raf = vi.fn();
    globalThis.requestAnimationFrame = raf;
    navigator.getGamepads = () => {
      throw new Error("driver quebrado");
    };
    expect(() => app.pollPads()).not.toThrow();
    expect(raf).toHaveBeenCalled(); // reagendou mesmo com erro
  });

  it("não reenvia quando nada mudou (dedup por byte do gatilho)", () => {
    app.connect();
    const pad = {
      id: "P", mapping: "standard", axes: [0, 0, 0, 0],
      buttons: Array.from({ length: 17 }, () => ({ pressed: false, value: 0 })),
    };
    navigator.getGamepads = () => [pad];
    app.pollPads();
    const n = enviados().filter((m) => m.t === "g").length;
    app.pollPads();
    app.pollPads();
    expect(enviados().filter((m) => m.t === "g").length).toBe(n);
  });

  it("ruído menor que 1/255 no gatilho não vira mensagem nova", () => {
    app.connect();
    const btns = () => Array.from({ length: 17 }, () => ({ pressed: false, value: 0 }));
    const pad = { id: "P", mapping: "standard", axes: [0, 0, 0, 0], buttons: btns() };
    navigator.getGamepads = () => [pad];
    app.pollPads();
    const antes = enviados().filter((m) => m.t === "g").length;
    pad.buttons[6] = { pressed: false, value: 0.001 }; // < 1/255
    app.pollPads();
    expect(enviados().filter((m) => m.t === "g").length).toBe(antes);
    pad.buttons[6] = { pressed: true, value: 0.5 };    // mudança real
    app.pollPads();
    expect(enviados().filter((m) => m.t === "g").length).toBe(antes + 1);
  });
});

// ───────────────────────── vídeo/áudio sob lixo ─────────────────────────

describe("fuzz: bytes aleatórios no lugar do stream", () => {
  for (const seed of SEEDS) {
    it(`onAU com lixo (semente ${seed}) não levanta`, () => {
      const rand = rng(seed);
      for (let i = 0; i < 120; i++) {
        const n = Math.floor(rand() * 60);
        const buf = new Uint8Array(n);
        for (let j = 0; j < n; j++) buf[j] = Math.floor(rand() * 256);
        expect(() => app.onAU(buf.buffer)).not.toThrow();
      }
    });
  }

  it("AU vazia e AU só com start code não quebram", () => {
    expect(() => app.onAU(new Uint8Array([]).buffer)).not.toThrow();
    expect(() => app.onAU(new Uint8Array([0, 0, 1]).buffer)).not.toThrow();
  });

  it("onAudioPacket com pacotes aleatórios não levanta", () => {
    const rand = rng(5);
    for (let i = 0; i < 60; i++) {
      const n = Math.floor(rand() * 200);
      expect(() => app.onAudioPacket(new Uint8Array(n).buffer)).not.toThrow();
    }
  });

  it("findNALs em lixo devolve sempre tipos 0..31", () => {
    const rand = rng(3);
    for (let i = 0; i < 200; i++) {
      const n = Math.floor(rand() * 80);
      const buf = new Uint8Array(n);
      for (let j = 0; j < n; j++) buf[j] = Math.floor(rand() * 256);
      for (const nal of app.findNALs(buf)) {
        expect(nal.type).toBeGreaterThanOrEqual(0);
        expect(nal.type).toBeLessThanOrEqual(31);
      }
    }
  });
});

describe("loadConfig com respostas ruins do servidor", () => {
  const respostas = [
    {},
    { presets: null },
    { presets: ["high"], monitors: null },
    { presets: [], monitors: [], res_options: null, audio_presets: null },
    null,
  ];
  for (const [i, body] of respostas.entries()) {
    it(`resposta #${i} não quebra a UI`, async () => {
      globalThis.fetch = vi.fn(async () => ({ json: async () => body }));
      await expect(app.loadConfig()).resolves.toBeUndefined();
    });
  }

  it("resposta completa preenche os seletores", async () => {
    await app.loadConfig(); // usa o fetch padrão do setup.js
    expect(document.getElementById("preset").children.length).toBe(5);
    expect(document.getElementById("vres").children.length).toBe(6);
    expect(document.getElementById("monitor").children.length).toBe(1);
  });
});
