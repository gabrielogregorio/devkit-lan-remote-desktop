/**
 * CONTRACT and PROPERTY tests for the client.
 *
 * app.test.js covers examples ("I pressed X, Y came out"). Here the question is
 * different: is *every* message the client produces, under any input, still
 * something the server knows how to receive?
 *
 * This matters because both ends are versioned together but evolve in
 * different files: the day app.js sends a field with NaN, or a `t` that
 * ws_input doesn't know, nobody notices until the controller "stops by itself".
 * The server-side mirror of this file is tests/test_fuzz_input.py.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadApp } from "./setup.js";

// ---- deterministic PRNG (failed once, fails always) -----------------------
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}
const SEEDS = [1, 42, 777, 20260813];

// hostile values a controller/driver may report
const CURSED = [
  undefined, null, NaN, Infinity, -Infinity, "0.5", "", true, false,
  {}, [], -5, 5, 1e308,
];

// Types the server (server/main.py ws_input + inject.Injector.handle) handles.
const ACCEPTED_TYPES = new Set(["m", "mr", "md", "mu", "w", "k", "g", "q", "vq", "aq", "mon"]);

let app;
beforeEach(async () => {
  app = await loadApp();
});

function sentMessages() {
  const ws = globalThis.__sockets.find((s) => s.url.endsWith("/ws/input"));
  return (ws ? ws.sent : []).map((s) => JSON.parse(s));
}

// ───────────────────────── message contract ─────────────────────────

describe("contract: every command sent is a command the server knows", () => {
  it("the types used in the app are exactly the documented ones", () => {
    app.connect();
    // exercises every message source that exists in the client
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
    document.getElementById("audioPreset").value = "medium";
    document.getElementById("audioPreset").dispatchEvent(new Event("change"));
    document.getElementById("monitor").value = "";
    document.getElementById("monitor").dispatchEvent(new Event("change"));

    const msgs = sentMessages();
    expect(msgs.length).toBeGreaterThan(5);
    for (const m of msgs) expect(ACCEPTED_TYPES.has(m.t)).toBe(true);
  });

  it("every message survives JSON without becoming null/undefined", () => {
    app.connect();
    const pad = {
      id: "P", mapping: "standard",
      axes: [0.25, -0.25, 0, 1],
      buttons: Array.from({ length: 17 }, (_, i) => ({ pressed: i === 3, value: i === 3 ? 1 : 0 })),
    };
    navigator.getGamepads = () => [pad];
    app.pollPads();
    for (const m of sentMessages()) {
      for (const [k, v] of Object.entries(m)) {
        expect(v, `field ${k} of t=${m.t}`).not.toBeNull();
        expect(v).not.toBeUndefined();
      }
    }
  });

  it("'vq' sends only the field that changed (the server fills in the rest)", () => {
    app.connect();
    document.getElementById("vfps").value = "30";
    document.getElementById("vfps").dispatchEvent(new Event("change"));
    const vq = sentMessages().find((m) => m.t === "vq");
    expect(Object.keys(vq).sort()).toEqual(["fps", "t"]);
    expect(typeof vq.fps).toBe("number");
  });

  it("'mon' sends null for the primary monitor (not an empty string)", () => {
    app.connect();
    const sel = document.getElementById("monitor");
    sel.innerHTML = "";
    const o = document.createElement("option");
    o.value = "";
    sel.appendChild(o);
    sel.value = "";
    sel.dispatchEvent(new Event("change"));
    const mon = sentMessages().find((m) => m.t === "mon");
    expect(mon.i).toBeNull();
  });
});

// ───────────────────────── gamepad: properties ─────────────────────────

function randomPad(rand, { standard } = {}) {
  const nAxes = Math.floor(rand() * 8);
  const nBtn = Math.floor(rand() * 20);
  const value = () =>
    rand() < 0.4 ? CURSED[Math.floor(rand() * CURSED.length)] : rand() * 2 - 1;
  return {
    id: "Pad " + Math.floor(rand() * 3),
    mapping: standard ?? (rand() < 0.5 ? "standard" : ""),
    axes: Array.from({ length: nAxes }, value),
    buttons: Array.from({ length: nBtn }, () =>
      rand() < 0.1 ? null : { pressed: rand() < 0.3, value: value() }),
  };
}

function checkFrame(r) {
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

describe("property: mapPad always produces a valid frame", () => {
  for (const seed of SEEDS) {
    it(`random controllers (seed ${seed}) never break the format`, () => {
      const rand = rng(seed);
      for (let i = 0; i < 300; i++) checkFrame(app.mapPad(randomPad(rand)));
    });
  }

  it("with a saved remap profile pointing at indices that do not exist", async () => {
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
      checkFrame(app.mapPad(randomPad(rand, { standard: "" })));
    }
  });

  it("a corrupted profile (wrong types) does not break the mapping", async () => {
    globalThis.fetch = vi.fn(async () => ({
      json: async () => ({ "Pad 0": { buttons: "nao-e-objeto", axes: 5, triggers: null } }),
    }));
    await app.loadGamepadMaps();
    const p = {
      id: "Pad 0", mapping: "",
      axes: [0.5, 0.5, 0, 0],
      buttons: [{ pressed: true, value: 1 }],
    };
    checkFrame(app.mapPad(p));
  });

  it("a pad without arrays (odd driver) becomes a neutral frame", () => {
    checkFrame(app.mapPad({ id: "X", mapping: "standard" }));
    checkFrame(app.mapPad({ id: "X", mapping: "standard", axes: null, buttons: null }));
  });

  it("a button with a missing value does not blow up (it was a TypeError in toFixed)", () => {
    const p = {
      id: "X", mapping: "standard",
      axes: [0, 0, 0, 0],
      buttons: [{ pressed: true }, {}, null, undefined],
    };
    const r = app.mapPad(p);
    checkFrame(r);
    expect(r.b[0]).toEqual({ p: true, v: 0 });
  });
});

describe("property: readSource always stays in 0..1", () => {
  for (const seed of SEEDS) {
    it(`random sources (seed ${seed})`, () => {
      const rand = rng(seed);
      for (let i = 0; i < 200; i++) {
        const p = randomPad(rand);
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

  it("a pad without buttons/axes (odd driver) returns released", () => {
    expect(app.readSource({}, { src: "button", i: 0 })).toBe(0);
    expect(app.readSource({}, { src: "axis", i: 0 })).toBe(0);
  });

  it("a degenerate calibration (min == max) is treated as released", () => {
    const p = { axes: [0.7], buttons: [] };
    expect(app.readSource(p, { src: "axis", i: 0, min: 0.5, max: 0.5 })).toBe(0);
  });

  it("an inverted axis (rest 0, bottom -1) is normalized the same way", () => {
    const p = { axes: [-1], buttons: [] };
    expect(app.readSource(p, { src: "axis", i: 0, min: 0, max: -1 })).toBe(1);
  });
});

describe("pollPads survives a hostile controller (the loop must not die)", () => {
  it("mapPad blowing up does not prevent the next frame", () => {
    const raf = vi.fn();
    globalThis.requestAnimationFrame = raf;
    navigator.getGamepads = () => {
      throw new Error("driver quebrado");
    };
    expect(() => app.pollPads()).not.toThrow();
    expect(raf).toHaveBeenCalled(); // rescheduled even after an error
  });

  it("does not resend when nothing changed (dedup by trigger byte)", () => {
    app.connect();
    const pad = {
      id: "P", mapping: "standard", axes: [0, 0, 0, 0],
      buttons: Array.from({ length: 17 }, () => ({ pressed: false, value: 0 })),
    };
    navigator.getGamepads = () => [pad];
    app.pollPads();
    const n = sentMessages().filter((m) => m.t === "g").length;
    app.pollPads();
    app.pollPads();
    expect(sentMessages().filter((m) => m.t === "g").length).toBe(n);
  });

  it("trigger noise below 1/255 does not become a new message", () => {
    app.connect();
    const btns = () => Array.from({ length: 17 }, () => ({ pressed: false, value: 0 }));
    const pad = { id: "P", mapping: "standard", axes: [0, 0, 0, 0], buttons: btns() };
    navigator.getGamepads = () => [pad];
    app.pollPads();
    const before = sentMessages().filter((m) => m.t === "g").length;
    pad.buttons[6] = { pressed: false, value: 0.001 }; // < 1/255
    app.pollPads();
    expect(sentMessages().filter((m) => m.t === "g").length).toBe(before);
    pad.buttons[6] = { pressed: true, value: 0.5 };    // real change
    app.pollPads();
    expect(sentMessages().filter((m) => m.t === "g").length).toBe(before + 1);
  });
});

// ───────────────────────── video/audio under garbage ─────────────────────────

describe("fuzz: random bytes instead of the stream", () => {
  for (const seed of SEEDS) {
    it(`onAU with garbage (seed ${seed}) does not throw`, () => {
      const rand = rng(seed);
      for (let i = 0; i < 120; i++) {
        const n = Math.floor(rand() * 60);
        const buf = new Uint8Array(n);
        for (let j = 0; j < n; j++) buf[j] = Math.floor(rand() * 256);
        expect(() => app.onAU(buf.buffer)).not.toThrow();
      }
    });
  }

  it("an empty AU and an AU with only a start code do not break", () => {
    expect(() => app.onAU(new Uint8Array([]).buffer)).not.toThrow();
    expect(() => app.onAU(new Uint8Array([0, 0, 1]).buffer)).not.toThrow();
  });

  it("onAudioPacket with random packets does not throw", () => {
    const rand = rng(5);
    for (let i = 0; i < 60; i++) {
      const n = Math.floor(rand() * 200);
      expect(() => app.onAudioPacket(new Uint8Array(n).buffer)).not.toThrow();
    }
  });

  it("findNALs on garbage always returns types 0..31", () => {
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

describe("loadConfig with bad server responses", () => {
  const responses = [
    {},
    { presets: null },
    { presets: ["high"], monitors: null },
    { presets: [], monitors: [], res_options: null, audio_presets: null },
    null,
  ];
  for (const [i, body] of responses.entries()) {
    it(`response #${i} does not break the UI`, async () => {
      globalThis.fetch = vi.fn(async () => ({ json: async () => body }));
      await expect(app.loadConfig()).resolves.toBeUndefined();
    });
  }

  it("a complete response fills the selectors", async () => {
    await app.loadConfig(); // uses the default fetch from setup.js
    expect(document.getElementById("preset").children.length).toBe(5);
    expect(document.getElementById("vres").children.length).toBe(6);
    expect(document.getElementById("monitor").children.length).toBe(1);
  });
});
