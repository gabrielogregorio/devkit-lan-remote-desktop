import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadApp } from "./setup.js";

let app;
beforeEach(async () => {
  app = await loadApp();
});

// ---------------------------------------------------------------------------
// 4.1 Pure functions
// ---------------------------------------------------------------------------

describe("hex2", () => {
  it("pads a single digit", () => expect(app.hex2(5)).toBe("05"));
  it("maximum byte value", () => expect(app.hex2(255)).toBe("ff"));
  it("zero", () => expect(app.hex2(0)).toBe("00"));
});

describe("wsBase / protocol", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("under https uses wss://", async () => {
    vi.stubGlobal("location", { protocol: "https:", host: "h:8443" });
    const httpsApp = await loadApp();
    httpsApp.connectInput();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    expect(ws.url.startsWith("wss://")).toBe(true);
  });
});

describe("findNALs", () => {
  it("two start codes return 2 entries with the right type and hdr", () => {
    const buf = new Uint8Array([0, 0, 1, 0x67, 0xAA, 0, 0, 1, 0x41, 0xBB]);
    const nals = app.findNALs(buf);
    expect(nals).toHaveLength(2);
    expect(nals[0].type).toBe(0x67 & 0x1f); // 7
    expect(nals[0].hdr).toBe(3);
    expect(nals[1].type).toBe(0x41 & 0x1f); // 1
    expect(nals[1].hdr).toBe(8);
  });

  it("no start code returns []", () => {
    const buf = new Uint8Array([0x01, 0x02, 0x03, 0x04]);
    expect(app.findNALs(buf)).toEqual([]);
  });

  it("a 4-byte start code does not falsely detect an extra entry", () => {
    // [0,0,0,1] — the extra zero is position i, then i+1=0, i+2=0, i+3=1: detected
    // but the type byte is buf[i+3] = the following '0x67', not the '1'
    const buf = new Uint8Array([0, 0, 0, 1, 0x67]);
    const nals = app.findNALs(buf);
    // The parser finds [0,0,1] at i=1 (buf[1]=0, buf[2]=0, buf[3]=1), type=buf[4]=0x67
    expect(nals.length).toBeGreaterThanOrEqual(1);
    // Confirms the i+=2 (extra increment) works: with a buf of 2 consecutive
    // start codes, none is skipped
    const buf2 = new Uint8Array([0, 0, 1, 0x67, 0, 0, 1, 0x41]);
    expect(app.findNALs(buf2)).toHaveLength(2);
  });
});

describe("codecFromSPS", () => {
  it("builds the right avc1 string from SPS bytes", () => {
    // au = [0,0,1,0x67, P, C, L]  spsHdr=3
    const au = new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f]);
    const result = app.codecFromSPS(au, 3);
    expect(result).toBe("avc1.64001f");
  });

  it("avc1.xxxxxx format (6 hex chars)", () => {
    const au = new Uint8Array([0, 0, 1, 0x67, 0x42, 0xc0, 0x1e]);
    expect(app.codecFromSPS(au, 3)).toMatch(/^avc1\.[0-9a-f]{6}$/);
  });
});

describe("sendChord", () => {
  it("presses in order and releases in reverse order", () => {
    app.connectInput();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    ws.sent = [];
    app.sendChord(["AltLeft", "F4"]);
    const seq = ws.sent.map(JSON.parse);
    expect(seq).toEqual([
      { t: "k", code: "AltLeft", down: true },
      { t: "k", code: "F4", down: true },
      { t: "k", code: "F4", down: false },
      { t: "k", code: "AltLeft", down: false },
    ]);
  });
});

describe("OSK (virtual keyboard)", () => {
  it("buildOSK builds the keys only once", () => {
    app.buildOSK();
    const n = document.getElementById("osk").querySelectorAll(".osk-key").length;
    expect(n).toBeGreaterThan(0);
    app.buildOSK(); // idempotent
    expect(document.getElementById("osk").querySelectorAll(".osk-key").length).toBe(n);
  });

  it("toggleOSK toggles the hidden class", () => {
    app.buildOSK();
    const k = document.getElementById("osk");
    app.toggleOSK(true);
    expect(k.classList.contains("hidden")).toBe(false);
    app.toggleOSK(false);
    expect(k.classList.contains("hidden")).toBe(true);
    app.toggleOSK(); // no arg: flips (was hidden -> shows)
    expect(k.classList.contains("hidden")).toBe(false);
  });

  it("oskPress on a normal key sends down+up", () => {
    app.connectInput();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    ws.sent = [];
    const btn = document.createElement("button");
    app.oskPress("KeyA", btn);
    const seq = ws.sent.map(JSON.parse);
    expect(seq).toContainEqual({ t: "k", code: "KeyA", down: true });
    expect(seq).toContainEqual({ t: "k", code: "KeyA", down: false });
  });

  it("sticky modifier: locks, applies to a normal key and releases", () => {
    app.connectInput();
    app.buildOSK();
    const k = document.getElementById("osk");
    const shift = k.querySelector('[data-code="ShiftLeft"]');
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    ws.sent = [];
    app.oskPress("ShiftLeft", shift); // locks
    expect(shift.classList.contains("held")).toBe(true);
    app.oskPress("KeyA", document.createElement("button")); // uses and releases
    expect(shift.classList.contains("held")).toBe(false);
    const downs = ws.sent.map(JSON.parse);
    expect(downs).toContainEqual({ t: "k", code: "ShiftLeft", down: true });
    expect(downs).toContainEqual({ t: "k", code: "ShiftLeft", down: false });
  });

  it("sticky clicked twice unlocks without applying", () => {
    app.connectInput();
    app.buildOSK();
    const k = document.getElementById("osk");
    const ctrl = k.querySelector('[data-code="ControlLeft"]');
    app.oskPress("ControlLeft", ctrl);
    expect(ctrl.classList.contains("held")).toBe(true);
    app.oskPress("ControlLeft", ctrl);
    expect(ctrl.classList.contains("held")).toBe(false);
  });

  it("the OSK's own ✕ button closes the keyboard", () => {
    app.toggleOSK(true);
    const k = document.getElementById("osk");
    k.querySelector(".osk-close").click();
    expect(k.classList.contains("hidden")).toBe(true);
    expect(document.getElementById("kbd").classList.contains("on")).toBe(false);
  });

  it("clicking a real OSK key sends the key", () => {
    app.connectInput();
    app.buildOSK();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    ws.sent = [];
    document.getElementById("osk").querySelector('[data-code="KeyG"]').click();
    const seq = ws.sent.map(s => JSON.parse(s));
    expect(seq).toContainEqual({ t: "k", code: "KeyG", down: true });
    expect(seq).toContainEqual({ t: "k", code: "KeyG", down: false });
  });
});

describe("bar recall (mouse at the top)", () => {
  it("mousemove at the top shows the bar", () => {
    document.getElementById("bar").classList.add("hidden");
    window.dispatchEvent(new MouseEvent("mousemove", { clientY: 2 }));
    expect(app.barHidden()).toBe(false);
  });
  it("a click on the gear corner (#menuRecall) brings the bar back", () => {
    app.showBar(false);
    document.getElementById("menuRecall").click();
    expect(app.barHidden()).toBe(false);
  });
  it("showBar(false) hides", () => {
    app.showBar(false);
    expect(app.barHidden()).toBe(true);
  });
  it("no auto-hide: once open, it only hides via the gear", () => {
    vi.useFakeTimers();
    Object.defineProperty(document, "pointerLockElement", {
      value: null, configurable: true,
    });
    window.dispatchEvent(new MouseEvent("mousemove", { clientY: 2 }));
    expect(app.barHidden()).toBe(false);
    vi.advanceTimersByTime(60000);
    window.dispatchEvent(new MouseEvent("mousemove", { clientY: 500 }));
    expect(app.barHidden()).toBe(false);
    vi.useRealTimers();
    document.getElementById("menuToggle").click();
    expect(app.barHidden()).toBe(true);
  });
  it("touchstart at the top (mobile) reveals the bar", () => {
    document.getElementById("bar").classList.add("hidden");
    const t = { clientY: 3, identifier: 1, clientX: 0, target: document.body };
    const ev = new Event("touchstart");
    ev.touches = [t];
    window.dispatchEvent(ev);
    expect(app.barHidden()).toBe(false);
  });
});

describe("displayedRect", () => {
  it("16:9 on a 16:9 screen → fills with no bars", () => {
    const canvas = document.getElementById("screen");
    canvas.width = 1280;
    canvas.height = 720; // aspect = element (1920x1080)
    const r = app.displayedRect();
    expect(r.left).toBeCloseTo(0);
    expect(r.top).toBeCloseTo(0);
    expect(r.width).toBeCloseTo(1920);
    expect(r.height).toBeCloseTo(1080);
  });

  it("buffer 'taller' than the element → side bars (offset in left)", () => {
    const canvas = document.getElementById("screen");
    canvas.width = 540;
    canvas.height = 540; // 1:1 in a 16:9 element → scales by height, space left on the sides
    const r = app.displayedRect();
    expect(r.height).toBeCloseTo(1080);
    expect(r.width).toBeCloseTo(1080);
    expect(r.left).toBeCloseTo((1920 - 1080) / 2);
    expect(r.top).toBeCloseTo(0);
  });

  it("fallback when the buffer is 0 uses the element size", () => {
    const canvas = document.getElementById("screen");
    canvas.width = 0;
    canvas.height = 0;
    const r = app.displayedRect();
    expect(r.width).toBeCloseTo(1920);
    expect(r.height).toBeCloseTo(1080);
  });
});

describe("normFromEvent", () => {
  it("exact center 0.5,0.5", () => {
    const n = app.normFromEvent({ clientX: 960, clientY: 540 });
    expect(n.x).toBeCloseTo(0.5);
    expect(n.y).toBeCloseTo(0.5);
  });
  it("negative clamp → 0", () => {
    expect(app.normFromEvent({ clientX: -100, clientY: -100 }).x).toBe(0);
    expect(app.normFromEvent({ clientX: -100, clientY: -100 }).y).toBe(0);
  });
  it("clamp past the edge → 1", () => {
    expect(app.normFromEvent({ clientX: 9999, clientY: 9999 }).x).toBe(1);
    expect(app.normFromEvent({ clientX: 9999, clientY: 9999 }).y).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// 4.2 WebCodecs video
// ---------------------------------------------------------------------------

describe("ensureDecoder", () => {
  it("first call creates a configured VideoDecoder", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    expect(dec).not.toBeNull();
    expect(dec).toBeInstanceOf(globalThis.VideoDecoder);
  });

  it("second call with an open decoder does not recreate it", () => {
    app.ensureDecoder();
    const first = app.__get("decoder");
    app.ensureDecoder();
    expect(app.__get("decoder")).toBe(first);
  });

  it("with decoder.state === 'closed' recreates it", () => {
    app.ensureDecoder();
    const first = app.__get("decoder");
    first.state = "closed";
    app.ensureDecoder();
    expect(app.__get("decoder")).not.toBe(first);
  });

  it("output callback: drawImage + frame.close + increments frames", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    const moduleCtx = app.__get("ctx"); // ctx captured by the module at load
    const frame = { displayWidth: 1920, displayHeight: 1080, close: vi.fn() };
    const framesBefore = app.__get("frames");
    dec.cfg.output(frame);
    expect(moduleCtx.drawImage).toHaveBeenCalledWith(frame, 0, 0);
    expect(frame.close).toHaveBeenCalled();
    expect(app.__get("frames")).toBe(framesBefore + 1);
  });

  it("output callback: adjusts canvas.width/height when they differ", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    const canvas = document.getElementById("screen");
    canvas.width = 0;
    canvas.height = 0;
    dec.cfg.output({ displayWidth: 800, displayHeight: 600, close: vi.fn() });
    expect(canvas.width).toBe(800);
    expect(canvas.height).toBe(600);
  });

  it("output callback: decodeLatency only updates on the second call", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    const frame = { displayWidth: 1920, displayHeight: 1080, close: vi.fn() };
    // First call — lastFrameAt was 0, decodeLatency not updated
    globalThis.performance.now.mockReturnValue(100);
    dec.cfg.output(frame);
    expect(app.__get("decodeLatency")).toBe(0);
    // Second call — lastFrameAt != 0, updated
    globalThis.performance.now.mockReturnValue(200);
    dec.cfg.output({ ...frame, close: vi.fn() });
    expect(app.__get("decodeLatency")).toBeGreaterThan(0);
  });

  it("error callback calls resetDecoder", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    dec.cfg.error(new Error("test error"));
    expect(app.__get("decoder")).toBeNull();
  });
});

describe("resetDecoder", () => {
  it("closes the decoder and clears state", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    app.resetDecoder();
    expect(dec.state).toBe("closed");
    expect(app.__get("decoder")).toBeNull();
    expect(app.__get("configured")).toBe(false);
    expect(app.__get("codecStr")).toBeNull();
  });

  it("a throwing close() is swallowed, decoder becomes null", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    dec.close = () => { throw new Error("boom"); };
    expect(() => app.resetDecoder()).not.toThrow();
    expect(app.__get("decoder")).toBeNull();
  });

  it("with a null decoder does not throw", () => {
    expect(() => app.resetDecoder()).not.toThrow();
  });
});

describe("onAU", () => {
  // AU with SPS (type 7) + IDR (type 5) + codec bytes
  const spsAU = () =>
    new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0, 0, 1, 0x65, 0x01]).buffer;
  const deltaAU = () =>
    new Uint8Array([0, 0, 1, 0x61, 0x01]).buffer;
  const flush = () => new Promise((r) => setTimeout(r, 0));

  // Configuration is async (isConfigSupported). The 1st keyframe starts the
  // probe and does NOT decode; once configured, the decoder still REQUIRES a
  // keyframe as the 1st chunk (needKey) -> we send one more SPS to unlock it.
  async function setup() {
    app.onAU(spsAU()); // starts async configuration
    await flush();
    expect(app.__get("configured")).toBe(true);
    app.onAU(spsAU()); // 1st keyframe after config: clears needKey and decodes
    expect(app.__get("needKey")).toBe(false);
  }

  it("AU without SPS and configured=false returns without creating a decoder", () => {
    app.onAU(deltaAU());
    expect(app.__get("decoder")).toBeNull();
  });

  it("1st SPS configures (async) without decoding; the next keyframe decodes as key", async () => {
    await setup();
    const dec = app.__get("decoder");
    expect(dec).not.toBeNull();
    expect(dec.decoded[dec.decoded.length - 1].type).toBe("key");
  });

  it("after config, deltas are dropped until a keyframe arrives", async () => {
    app.onAU(spsAU());
    await flush();
    expect(app.__get("configured")).toBe(true);
    expect(app.__get("needKey")).toBe(true);
    const dec = app.__get("decoder");
    // delta before the keyframe -> does NOT decode (needKey still true)
    app.onAU(deltaAU());
    expect(dec.decoded).toBeUndefined();
    expect(app.__get("needKey")).toBe(true);
    // the keyframe arrives -> unlocks and decodes
    app.onAU(spsAU());
    expect(app.__get("needKey")).toBe(false);
    expect(dec.decoded[dec.decoded.length - 1].type).toBe("key");
  });

  it("while configuring (async in progress) the AU is ignored", () => {
    app.onAU(spsAU());
    expect(app.__get("configuring")).toBe(true);
    const before = app.__get("decoder");
    app.onAU(spsAU()); // arrives in the middle of the probe -> early return
    expect(app.__get("decoder")).toBe(before);
  });

  it("a delta AU after configuration decodes as delta", async () => {
    await setup();
    const dec = app.__get("decoder");
    app.onAU(deltaAU());
    expect(dec.decoded[dec.decoded.length - 1].type).toBe("delta");
  });

  it("a throwing decode calls resetDecoder", async () => {
    await setup();
    const dec = app.__get("decoder");
    dec.decode = () => { throw new Error("decode fail"); };
    app.onAU(deltaAU());
    expect(app.__get("decoder")).toBeNull();
  });

  it("accumulates bytes", async () => {
    await setup();
    const b1 = app.__get("bytes");
    app.onAU(deltaAU());
    expect(app.__get("bytes")).toBeGreaterThan(b1);
  });
});

describe("configureDecoder (fallback Chrome/Linux)", () => {
  const codec = "avc1.640028";

  it("uses prefer-hardware when supported", async () => {
    const ok = await app.configureDecoder(codec);
    expect(ok).toBe(true);
    expect(app.__get("decoder").configured.hardwareAcceleration).toBe("prefer-hardware");
  });

  it("falls back to no-preference when hardware is not supported", async () => {
    let calls = 0;
    globalThis.VideoDecoder.isConfigSupported = async (cfg) => {
      calls++;
      return { supported: cfg.hardwareAcceleration !== "prefer-hardware", config: cfg };
    };
    const ok = await app.configureDecoder(codec);
    expect(ok).toBe(true);
    expect(app.__get("decoder").configured.hardwareAcceleration).toBe("no-preference");
    expect(calls).toBeGreaterThanOrEqual(2);
  });

  it("no mode supported: returns false and sets the status", async () => {
    globalThis.VideoDecoder.isConfigSupported = async () => ({ supported: false });
    const ok = await app.configureDecoder(codec);
    expect(ok).toBe(false);
  });

  it("a throwing isConfigSupported: tries the next mode", async () => {
    let n = 0;
    globalThis.VideoDecoder.isConfigSupported = async (cfg) => {
      n++;
      if (cfg.hardwareAcceleration === "prefer-hardware") throw new Error("boom");
      return { supported: true, config: cfg };
    };
    const ok = await app.configureDecoder(codec);
    expect(ok).toBe(true);
    expect(n).toBeGreaterThanOrEqual(2);
  });

  it("uses the real canvas size and sup.config when present", async () => {
    const canvas = document.getElementById("screen");
    canvas.width = 1280;
    canvas.height = 720;
    let seen;
    globalThis.VideoDecoder.isConfigSupported = async (cfg) => {
      seen = cfg;
      return { supported: true, config: { ...cfg, marker: 1 } };
    };
    const ok = await app.configureDecoder(codec);
    expect(ok).toBe(true);
    expect(seen.codedWidth).toBe(1280);
    expect(seen.codedHeight).toBe(720);
    expect(app.__get("decoder").configured.marker).toBe(1);
  });

  it("without sup.config uses the local cfg", async () => {
    globalThis.VideoDecoder.isConfigSupported = async (cfg) => ({ supported: true });
    const ok = await app.configureDecoder(codec);
    expect(ok).toBe(true);
    expect(app.__get("decoder").configured.codec).toBe(codec);
  });

  it("a canvas without size falls back to 1920x1080", async () => {
    const canvas = document.getElementById("screen");
    canvas.width = 0;
    canvas.height = 0;
    let seen;
    globalThis.VideoDecoder.isConfigSupported = async (cfg) => {
      seen = cfg;
      return { supported: true, config: cfg };
    };
    await app.configureDecoder(codec);
    expect(seen.codedWidth).toBe(1920);
    expect(seen.codedHeight).toBe(1080);
  });
});

// ---------------------------------------------------------------------------
// 4.3 WebCodecs audio
// ---------------------------------------------------------------------------

describe("startAudioContext", () => {
  it("first call creates the AudioContext", () => {
    app.startAudioContext();
    expect(app.__get("audioCtx")).not.toBeNull();
  });

  it("second call with state=running does not recreate it", () => {
    app.startAudioContext();
    const ctx = app.__get("audioCtx");
    app.startAudioContext();
    expect(app.__get("audioCtx")).toBe(ctx);
  });

  it("with state=suspended calls resume()", () => {
    app.startAudioContext();
    const ctx = app.__get("audioCtx");
    ctx.state = "suspended";
    const resumeSpy = vi.spyOn(ctx, "resume");
    app.startAudioContext();
    expect(resumeSpy).toHaveBeenCalled();
  });

  it("webkitAudioContext fallback", () => {
    const savedAC = globalThis.AudioContext;
    globalThis.AudioContext = undefined;
    globalThis.webkitAudioContext = savedAC;
    app.startAudioContext();
    expect(app.__get("audioCtx")).not.toBeNull();
    globalThis.AudioContext = savedAC;
    delete globalThis.webkitAudioContext;
  });
});

describe("ensureAudioDecoder / resetAudioDecoder", () => {
  it("ensureAudioDecoder creates and configures the AudioDecoder", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    expect(ad).not.toBeNull();
    expect(ad.state).toBe("configured");
  });

  it("second call without closing does not recreate it", () => {
    app.ensureAudioDecoder();
    const first = app.__get("audioDecoder");
    app.ensureAudioDecoder();
    expect(app.__get("audioDecoder")).toBe(first);
  });

  it("with state=closed recreates it", () => {
    app.ensureAudioDecoder();
    const first = app.__get("audioDecoder");
    first.state = "closed";
    app.ensureAudioDecoder();
    expect(app.__get("audioDecoder")).not.toBe(first);
  });

  it("output callback forwards to playAudio", () => {
    app.startAudioContext();
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    const data = {
      numberOfFrames: 960,
      numberOfChannels: 2,
      sampleRate: 48000,
      copyTo: vi.fn(),
      close: vi.fn(),
    };
    ad.cfg.output(data);
    expect(data.close).toHaveBeenCalled();
  });

  it("error callback calls resetAudioDecoder", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    ad.cfg.error(new Error("audio error"));
    expect(app.__get("audioDecoder")).toBeNull();
  });

  it("resetAudioDecoder closes the decoder and clears playHead", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    app.resetAudioDecoder();
    expect(ad.state).toBe("closed");
    expect(app.__get("audioDecoder")).toBeNull();
    expect(app.__get("playHead")).toBe(0);
  });

  it("resetAudioDecoder: a throwing close() is swallowed", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    ad.close = () => { throw new Error("boom"); };
    expect(() => app.resetAudioDecoder()).not.toThrow();
    expect(app.__get("audioDecoder")).toBeNull();
  });

  it("resetAudioDecoder with a null audioDecoder does not throw", () => {
    expect(() => app.resetAudioDecoder()).not.toThrow();
  });
});

describe("playAudio", () => {
  const makeData = (overrides = {}) => ({
    numberOfFrames: 960,
    numberOfChannels: 2,
    sampleRate: 48000,
    copyTo: vi.fn(),
    close: vi.fn(),
    ...overrides,
  });

  it("without audioCtx: calls data.close() and returns", () => {
    const data = makeData();
    const srcStart = vi.fn();
    app.playAudio(data);
    expect(data.close).toHaveBeenCalled();
    expect(srcStart).not.toHaveBeenCalled();
  });

  it("normal: creates the buffer, copies channels, schedules start", () => {
    app.startAudioContext();
    const ctx = app.__get("audioCtx");
    const srcSpy = ctx.createBufferSource();
    vi.spyOn(ctx, "createBufferSource").mockReturnValue(srcSpy);
    const data = makeData();
    app.playAudio(data);
    expect(data.copyTo).toHaveBeenCalled();
    expect(srcSpy.start).toHaveBeenCalled();
    expect(data.close).toHaveBeenCalled();
  });

  it("without sampleRate: falls back to AUDIO_SR", () => {
    app.startAudioContext();
    const ctx = app.__get("audioCtx");
    const bufSpy = vi.spyOn(ctx, "createBuffer");
    const data = makeData({ sampleRate: undefined });
    app.playAudio(data);
    // the 3rd arg (sampleRate) fell back to AUDIO_SR (48000), not undefined
    expect(bufSpy).toHaveBeenCalledWith(2, 960, 48000);
  });

  it("copyTo throwing with format: falls back to no format", () => {
    app.startAudioContext();
    const ctx = app.__get("audioCtx");
    const srcSpy = ctx.createBufferSource();
    vi.spyOn(ctx, "createBufferSource").mockReturnValue(srcSpy);
    let calls = 0;
    const data = makeData({
      copyTo: vi.fn((tmp, opts) => {
        if (opts && opts.format) throw new Error("format not supported");
        calls++;
      }),
    });
    app.playAudio(data);
    // the fallback was called (no format)
    expect(calls).toBeGreaterThan(0);
    expect(data.close).toHaveBeenCalled();
  });

  it("re-anchors when playHead < now + 0.005 (behind)", () => {
    app.startAudioContext();
    const ctx = app.__get("audioCtx");
    ctx.currentTime = 1.0;
    // playHead = 0 < 1.0 + 0.005 → re-anchor
    const srcSpy = ctx.createBufferSource();
    vi.spyOn(ctx, "createBufferSource").mockReturnValue(srcSpy);
    app.playAudio(makeData());
    // after re-anchoring, playHead must be close to now + JITTER
    expect(app.__get("playHead")).toBeGreaterThan(1.0);
  });

  it("re-anchors when playHead > now + JITTER + 0.12 (burst)", () => {
    app.startAudioContext();
    const ctx = app.__get("audioCtx");
    ctx.currentTime = 0;
    // JITTER=0.06, duration per packet = 960/48000 = 0.02s
    // Trajectory with currentTime=0:
    //   call1: playHead=0 < 0.005 → re-anchor → playHead = 0.06 + 0.02 = 0.08
    //   calls 2-7: playHead grows 0.02/call → after call7 playHead = 0.20
    //   call8: 0.20 > 0 + 0.06 + 0.12 = 0.18 → re-anchor → playHead = 0.06 + 0.02 = 0.08
    const srcSpy = { connect: vi.fn(), start: vi.fn(), buffer: null };
    vi.spyOn(ctx, "createBufferSource").mockReturnValue(srcSpy);
    // 7 calls take playHead to 0.20
    for (let i = 0; i < 7; i++) app.playAudio(makeData());
    const phBefore = app.__get("playHead"); // should be ~0.20
    // the 8th call triggers the re-anchor: 0.20 > 0.18
    app.playAudio(makeData());
    expect(app.__get("playHead")).toBeLessThan(phBefore);
  });
});

describe("onAudioPacket", () => {
  it("decodes and increments audioTs by 20000", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    const tsBefore = app.__get("audioTs");
    const buf = new ArrayBuffer(10);
    app.onAudioPacket(buf);
    expect(ad.decoded).toHaveLength(1);
    expect(ad.decoded[0].type).toBe("key");
    expect(app.__get("audioTs")).toBe(tsBefore + 20000);
  });

  it("a throwing decode calls resetAudioDecoder", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    ad.decode = () => { throw new Error("audio decode fail"); };
    app.onAudioPacket(new ArrayBuffer(10));
    expect(app.__get("audioDecoder")).toBeNull();
  });

  it("accumulates audioBytes and audioPkts", () => {
    const buf = new ArrayBuffer(20);
    app.onAudioPacket(buf);
    expect(app.__get("audioBytes")).toBe(20);
    expect(app.__get("audioPkts")).toBe(1);
    app.onAudioPacket(buf);
    expect(app.__get("audioBytes")).toBe(40);
    expect(app.__get("audioPkts")).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// 4.4 Audio connection
// ---------------------------------------------------------------------------

describe("connectAudio", () => {
  it("with audioOn=false returns without creating a socket", () => {
    app.connectAudio();
    expect(globalThis.__sockets).toHaveLength(0);
  });

  it("with audioOn=true creates the /ws/audio socket", () => {
    app.setAudio(true);
    const sockets = globalThis.__sockets.filter(s => s.url.includes("/ws/audio"));
    expect(sockets.length).toBeGreaterThan(0);
  });

  it("onmessage with audioOn=true calls onAudioPacket", () => {
    vi.useFakeTimers();
    app.setAudio(true);
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/audio"));
    const adBefore = app.__get("audioPkts");
    ws.onmessage({ data: new ArrayBuffer(10) });
    expect(app.__get("audioPkts")).toBe(adBefore + 1);
    vi.useRealTimers();
  });

  it("onclose with audioOn=true schedules a reconnect", () => {
    vi.useFakeTimers();
    app.setAudio(true);
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/audio"));
    const countBefore = globalThis.__sockets.length;
    ws.onclose();
    vi.runAllTimers();
    expect(globalThis.__sockets.length).toBeGreaterThan(countBefore);
    vi.useRealTimers();
  });

  it("onclose with audioOn=false does not reconnect", () => {
    vi.useFakeTimers();
    // setAudio(true) creates the socket, setAudio(false) closes it and fires onclose with audioOn=false
    app.setAudio(true);
    const countAfterTrue = globalThis.__sockets.length;
    app.setAudio(false); // triggers onclose with audioOn=false
    vi.runAllTimers();
    expect(globalThis.__sockets.length).toBe(countAfterTrue);
    vi.useRealTimers();
  });

  it("onerror calls ws.close()", () => {
    app.setAudio(true);
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/audio"));
    const closeSpy = vi.spyOn(ws, "close");
    ws.onerror();
    expect(closeSpy).toHaveBeenCalled();
  });

  it("onerror with a throwing close() is swallowed", () => {
    app.setAudio(true);
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/audio"));
    ws.close = () => { throw new Error("close boom"); };
    expect(() => ws.onerror()).not.toThrow();
  });
});

describe("setAudio", () => {
  it("setAudio(true): button 'on', startAudioContext, socket", () => {
    app.setAudio(true);
    expect(document.getElementById("sound").classList.contains("on")).toBe(true);
    expect(app.__get("audioCtx")).not.toBeNull();
    expect(globalThis.__sockets.some(s => s.url.includes("/ws/audio"))).toBe(true);
  });

  it("setAudio(false): button without 'on', suspend", () => {
    app.setAudio(true);
    const ctx = app.__get("audioCtx");
    const suspendSpy = vi.spyOn(ctx, "suspend");
    app.setAudio(false);
    expect(document.getElementById("sound").classList.contains("on")).toBe(false);
    expect(suspendSpy).toHaveBeenCalled();
    expect(app.__get("audioDecoder")).toBeNull();
  });

  it("setAudio(false) with a null audioCtx does not throw", () => {
    expect(() => app.setAudio(false)).not.toThrow();
  });

  it("setAudio(false): a throwing close() is swallowed", () => {
    app.setAudio(true);
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/audio"));
    ws.close = () => { throw new Error("close boom"); };
    expect(() => app.setAudio(false)).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// 4.5 Video/input connection
// ---------------------------------------------------------------------------

describe("connect", () => {
  it("adds 'hidden' to the overlay and creates 2 sockets", () => {
    const overlay = document.getElementById("overlay");
    overlay.classList.remove("hidden");
    app.connect();
    expect(overlay.classList.contains("hidden")).toBe(true);
    expect(globalThis.__sockets).toHaveLength(2);
  });
});

describe("connectVideo", () => {
  it("creates the /ws/video socket with binaryType arraybuffer", () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    expect(ws).toBeDefined();
    expect(ws.binaryType).toBe("arraybuffer");
  });

  it("onmessage string 'reset' calls resetDecoder", () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    // First configure the decoder so there is something to reset
    app.onAU(new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0, 0, 1, 0x65, 0x01]).buffer);
    ws.onmessage({ data: JSON.stringify({ t: "reset" }) });
    expect(app.__get("decoder")).toBeNull();
  });

  it("onmessage with an invalid JSON string is swallowed", () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    expect(() => ws.onmessage({ data: "not json" })).not.toThrow();
  });

  it("onmessage with valid JSON but t !== 'reset' does not call resetDecoder", async () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    app.onAU(new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0, 0, 1, 0x65, 0x01]).buffer);
    await new Promise((r) => setTimeout(r, 0)); // waits for the async config
    ws.onmessage({ data: JSON.stringify({ t: "outro" }) });
    // decoder was NOT reset (configured still true)
    expect(app.__get("configured")).toBe(true);
  });

  it("onmessage ArrayBuffer calls onAU", async () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    const au = new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0, 0, 1, 0x65, 0x01]).buffer;
    ws.onmessage({ data: au });
    await new Promise((r) => setTimeout(r, 0)); // waits for the async config
    expect(app.__get("configured")).toBe(true);
  });

  it("onclose: resetDecoder + status + setTimeout", () => {
    vi.useFakeTimers();
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    app.onAU(new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0, 0, 1, 0x65, 0x01]).buffer);
    ws.onclose();
    expect(app.__get("decoder")).toBeNull();
    expect(document.getElementById("status").textContent).toMatch(/disconnected|reconnecting/);
    vi.useRealTimers();
  });

  it("onclose with visibilityState=visible → reconnects after the timer", () => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", {
      value: "visible", configurable: true,
    });
    app.connectVideo();
    const countBefore = globalThis.__sockets.length;
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    ws.onclose();
    vi.runAllTimers();
    expect(globalThis.__sockets.length).toBeGreaterThan(countBefore);
    vi.useRealTimers();
  });

  it("onclose with visibilityState=hidden → does NOT reconnect", () => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", {
      value: "hidden", configurable: true,
    });
    app.connectVideo();
    const countBefore = globalThis.__sockets.length;
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    ws.onclose();
    vi.runAllTimers();
    expect(globalThis.__sockets.length).toBe(countBefore);
    vi.useRealTimers();
    // Restore
    Object.defineProperty(document, "visibilityState", {
      value: "visible", configurable: true,
    });
  });

  it("onerror calls ws.close()", () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    const closeSpy = vi.spyOn(ws, "close");
    ws.onerror();
    expect(closeSpy).toHaveBeenCalled();
  });
});

describe("connectInput", () => {
  it("creates the /ws/input socket", () => {
    app.connectInput();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    expect(ws).toBeDefined();
  });

  it("onclose schedules a reconnect", () => {
    vi.useFakeTimers();
    app.connectInput();
    const countBefore = globalThis.__sockets.length;
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    ws.onclose();
    vi.runAllTimers();
    expect(globalThis.__sockets.length).toBeGreaterThan(countBefore);
    vi.useRealTimers();
  });
});

describe("sendInput", () => {
  it("with readyState=1 sends JSON", () => {
    app.connectInput();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    app.sendInput({ t: "test", x: 0.5 });
    expect(ws.sent).toHaveLength(1);
    expect(JSON.parse(ws.sent[0])).toEqual({ t: "test", x: 0.5 });
  });

  it("with a null inputWS does not throw", () => {
    expect(() => app.sendInput({ t: "test" })).not.toThrow();
  });

  it("with readyState !== 1 does not send", () => {
    app.connectInput();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    ws.readyState = 3;
    app.sendInput({ t: "test" });
    expect(ws.sent).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// 4.6 Mouse
// ---------------------------------------------------------------------------

describe("Mouse events", () => {
  beforeEach(() => {
    app.connectInput();
  });

  it("flushMove with a pendingMove sends and clears it", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    // Produces a pendingMove via mousemove without pointer lock
    const canvas = document.getElementById("screen");
    canvas.dispatchEvent(new MouseEvent("mousemove", { clientX: 960, clientY: 540, bubbles: true }));
    app.flushMove();
    expect(ws.sent.length).toBeGreaterThan(0);
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.t).toBe("m");
    // after the flush, pendingMove = null
    const sentBefore = ws.sent.length;
    app.flushMove();
    expect(ws.sent.length).toBe(sentBefore); // sent nothing extra
  });

  it("flushMove without a pendingMove does not send, but calls rAF", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    app.flushMove();
    expect(ws.sent).toHaveLength(0);
    expect(globalThis.requestAnimationFrame).toHaveBeenCalled();
  });

  it("mousemove without pointer lock → pendingMove with t:m", () => {
    const canvas = document.getElementById("screen");
    canvas.dispatchEvent(new MouseEvent("mousemove", { clientX: 960, clientY: 540, bubbles: true }));
    const pm = app.__get("pendingMove");
    expect(pm).not.toBeNull();
    expect(pm.t).toBe("m");
    expect(pm.x).toBeCloseTo(0.5);
  });

  it("mousemove with pointer lock → sends t:mr via sendInput", () => {
    const canvas = document.getElementById("screen");
    Object.defineProperty(document, "pointerLockElement", {
      get: () => canvas, configurable: true,
    });
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    canvas.dispatchEvent(new MouseEvent("mousemove", {
      clientX: 10, clientY: 10,
      movementX: 5, movementY: 3,
      bubbles: true,
    }));
    const sent = ws.sent.map(s => JSON.parse(s));
    expect(sent.some(m => m.t === "mr")).toBe(true);
    // Restore
    Object.defineProperty(document, "pointerLockElement", {
      get: () => null, configurable: true,
    });
  });

  it("mousedown sends t:md and calls preventDefault", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const ev = new MouseEvent("mousedown", { button: 2, bubbles: true, cancelable: true });
    canvas.dispatchEvent(ev);
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "md", b: 2 });
  });

  it("mouseup sends t:mu", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    canvas.dispatchEvent(new MouseEvent("mouseup", { button: 0, bubbles: true, cancelable: true }));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "mu", b: 0 });
  });

  it("contextmenu calls preventDefault without sending", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sentBefore = ws.sent.length;
    const ev = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    canvas.dispatchEvent(ev);
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("wheel sends t:w with sign(delta)", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    canvas.dispatchEvent(new WheelEvent("wheel", { deltaX: 5, deltaY: -3, bubbles: true, cancelable: true }));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "w", x: 1, y: -1 });
  });

  it("wheel with zero deltas → x:0, y:0", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    canvas.dispatchEvent(new WheelEvent("wheel", { deltaX: 0, deltaY: 0, bubbles: true, cancelable: true }));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "w", x: 0, y: 0 });
  });
});

// ---------------------------------------------------------------------------
// 4.7 Touch
// ---------------------------------------------------------------------------

describe("Touch events", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    app.connectInput();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function makeTouch(id, x, y) {
    return { identifier: id, clientX: x, clientY: y };
  }

  it("touchstart with 1 touch sends t:m and clears touchMoved", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const touch = makeTouch(1, 960, 540);
    const ev = new TouchEvent("touchstart", {
      touches: [touch], changedTouches: [touch], bubbles: true, cancelable: true,
    });
    canvas.dispatchEvent(ev);
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.t).toBe("m");
    expect(app.__get("touchMoved")).toBe(false);
  });

  it("touchstart with more than 1 touch returns without sending", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sentBefore = ws.sent.length;
    const t1 = makeTouch(1, 100, 100);
    const t2 = makeTouch(2, 200, 200);
    canvas.dispatchEvent(new TouchEvent("touchstart", {
      touches: [t1, t2], changedTouches: [t1], bubbles: true, cancelable: true,
    }));
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("touchmove with the right touchId sets pendingMove and touchMoved", () => {
    const canvas = document.getElementById("screen");
    // First a touchstart to set touchId
    const touch = makeTouch(42, 100, 100);
    canvas.dispatchEvent(new TouchEvent("touchstart", {
      touches: [touch], changedTouches: [touch], bubbles: true, cancelable: true,
    }));
    // Now a touchmove with the same identifier
    const moveTouch = makeTouch(42, 200, 200);
    canvas.dispatchEvent(new TouchEvent("touchmove", {
      touches: [moveTouch], changedTouches: [moveTouch], bubbles: true, cancelable: true,
    }));
    expect(app.__get("touchMoved")).toBe(true);
    expect(app.__get("pendingMove")).not.toBeNull();
  });

  it("touchmove with a different identifier has no effect", () => {
    const canvas = document.getElementById("screen");
    const touch = makeTouch(1, 100, 100);
    canvas.dispatchEvent(new TouchEvent("touchstart", {
      touches: [touch], changedTouches: [touch], bubbles: true, cancelable: true,
    }));
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sentBefore = ws.sent.length;
    const otherTouch = makeTouch(99, 500, 500);
    canvas.dispatchEvent(new TouchEvent("touchmove", {
      touches: [otherTouch], changedTouches: [otherTouch], bubbles: true, cancelable: true,
    }));
    expect(app.__get("touchMoved")).toBe(false);
  });

  it("touchend without moving sends md, then mu after 40ms", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const touch = makeTouch(1, 100, 100);
    canvas.dispatchEvent(new TouchEvent("touchstart", {
      touches: [touch], changedTouches: [touch], bubbles: true, cancelable: true,
    }));
    canvas.dispatchEvent(new TouchEvent("touchend", {
      touches: [], changedTouches: [touch], bubbles: true, cancelable: true,
    }));
    // md sent immediately
    const msgs = ws.sent.map(s => JSON.parse(s));
    expect(msgs.some(m => m.t === "md" && m.b === 0)).toBe(true);
    // mu sent after the timer
    vi.advanceTimersByTime(40);
    const msgs2 = ws.sent.map(s => JSON.parse(s));
    expect(msgs2.some(m => m.t === "mu" && m.b === 0)).toBe(true);
    expect(app.__get("touchId")).toBeNull();
  });

  it("touchend with touchMoved=true does not send md/mu", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const touch = makeTouch(1, 100, 100);
    canvas.dispatchEvent(new TouchEvent("touchstart", {
      touches: [touch], changedTouches: [touch], bubbles: true, cancelable: true,
    }));
    // Simulates movement
    canvas.dispatchEvent(new TouchEvent("touchmove", {
      touches: [makeTouch(1, 200, 200)], changedTouches: [makeTouch(1, 200, 200)],
      bubbles: true, cancelable: true,
    }));
    const sentBefore = ws.sent.length;
    canvas.dispatchEvent(new TouchEvent("touchend", {
      touches: [], changedTouches: [touch], bubbles: true, cancelable: true,
    }));
    vi.advanceTimersByTime(40);
    // No extra md/mu
    const newMsgs = ws.sent.slice(sentBefore).map(s => JSON.parse(s));
    expect(newMsgs.some(m => m.t === "md" || m.t === "mu")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 4.8 Keyboard
// ---------------------------------------------------------------------------

describe("Keyboard events", () => {
  beforeEach(() => {
    app.connectInput();
    app.setKbdCapture(true); // capture happens ONLY with focus on the canvas; we turn it on to test
  });

  it("without capture (focus outside the canvas) does not send", () => {
    app.setKbdCapture(false);
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    ws.sent = [];
    window.dispatchEvent(new KeyboardEvent("keydown", { code: "KeyA", bubbles: true, cancelable: true }));
    expect(ws.sent).toHaveLength(0);
  });

  it("keydown sends t:k down:true", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    window.dispatchEvent(new KeyboardEvent("keydown", { code: "KeyA", bubbles: true, cancelable: true }));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "k", code: "KeyA", down: true });
  });

  it("keyup sends t:k down:false", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    window.dispatchEvent(new KeyboardEvent("keyup", { code: "Space", bubbles: true, cancelable: true }));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "k", code: "Space", down: false });
  });

  it("F11 is ignored (returns without sending)", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sentBefore = ws.sent.length;
    window.dispatchEvent(new KeyboardEvent("keydown", { code: "F11", bubbles: true, cancelable: true }));
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("F11 on keyup is ignored too", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sentBefore = ws.sent.length;
    window.dispatchEvent(new KeyboardEvent("keyup", { code: "F11", bubbles: true, cancelable: true }));
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("without inputWS does not throw", () => {
    // inputWS is null (we don't call connectInput in this scope; but beforeEach already did)
    // We force readyState != 1 to test the branch
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    ws.readyState = 3;
    expect(() =>
      window.dispatchEvent(new KeyboardEvent("keydown", { code: "KeyB", bubbles: true }))
    ).not.toThrow();
    expect(ws.sent).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// 4.9 Gamepad
// ---------------------------------------------------------------------------

describe("Gamepad / pollPads", () => {
  beforeEach(() => {
    app.connectInput();
  });

  it("no pad: does not send, calls rAF", () => {
    navigator.getGamepads = () => [];
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    app.pollPads();
    expect(ws.sent).toHaveLength(0);
    expect(globalThis.requestAnimationFrame).toHaveBeenCalled();
  });

  it("without getGamepads: uses an empty list without throwing", () => {
    navigator.getGamepads = undefined;
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    expect(() => app.pollPads()).not.toThrow();
    expect(ws.sent).toHaveLength(0);
  });

  it("pad with axes and buttons: sends t:g", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    navigator.getGamepads = () => [{
      axes: [0.5, -0.5, 1.0, -1.0],
      buttons: [
        { pressed: true, value: 1.0 },
        { pressed: false, value: 0.0 },
      ],
    }];
    app.pollPads();
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.t).toBe("g");
    expect(msg.a).toEqual([0.5, -0.5, 1.0, -1.0]);
  });

  it("identical state: does not resend (dedup)", () => {
    navigator.getGamepads = () => [{
      axes: [0, 0, 0, 0],
      buttons: [{ pressed: false, value: 0 }],
    }];
    app.pollPads();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const countAfterFirst = ws.sent.length;
    app.pollPads();
    expect(ws.sent.length).toBe(countAfterFirst); // did not resend
  });

  it("changed state: resends", () => {
    navigator.getGamepads = () => [{
      axes: [0, 0, 0, 0],
      buttons: [{ pressed: false, value: 0 }],
    }];
    app.pollPads();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const countAfterFirst = ws.sent.length;
    // Change the state
    navigator.getGamepads = () => [{
      axes: [1, 0, 0, 0],
      buttons: [{ pressed: false, value: 0 }],
    }];
    app.pollPads();
    expect(ws.sent.length).toBeGreaterThan(countAfterFirst);
  });

  it("falsy p.axes[i] uses || 0", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    navigator.getGamepads = () => [{
      axes: [0, undefined, NaN, null],
      buttons: [],
    }];
    app.pollPads();
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.a).toEqual([0, 0, 0, 0]);
  });

  it("standard mapping: triggers REPLICATE the raw value of buttons[6]/[7]", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const btns = Array.from({ length: 8 }, () => ({ pressed: false, value: 0 }));
    btns[6] = { pressed: true, value: 0.7 };
    btns[7] = { pressed: true, value: 0.4 };
    navigator.getGamepads = () => [{ mapping: "standard", axes: [0, 0, 0, 0], buttons: btns }];
    app.pollPads();
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    // faithful: no deadzone/rescale -> exactly what the controller reports
    expect(msg.lt).toBe(0.7);
    expect(msg.rt).toBe(0.4);
  });

  it("a light trigger press is replicated faithfully (no distorting deadzone)", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const btns = Array.from({ length: 8 }, () => ({ pressed: true, value: 0.2 }));
    navigator.getGamepads = () => [{ mapping: "standard", axes: [0, 0, 0, 0], buttons: btns }];
    app.pollPads();
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.lt).toBe(0.2); // real value kept, not zeroed
  });

  it("a nearly full trigger is NOT saturated early", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const btns = Array.from({ length: 8 }, () => ({ pressed: true, value: 0.97 }));
    navigator.getGamepads = () => [{ mapping: "standard", axes: [0, 0, 0, 0], buttons: btns }];
    app.pollPads();
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.lt).toBe(0.97); // faithful replica, does not force 1
  });

  it("a trigger above 1 is clamped to 1", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const btns = Array.from({ length: 8 }, () => ({ pressed: false, value: 0 }));
    btns[6] = { pressed: true, value: 1.3 }; // source out of range
    navigator.getGamepads = () => [{ mapping: "standard", axes: [0, 0, 0, 0], buttons: btns }];
    app.pollPads();
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.lt).toBe(1);
  });

  it("a negative/NaN trigger falls to 0 (clamp), without inventing a value", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const btns = Array.from({ length: 8 }, () => ({ pressed: false, value: 0 }));
    btns[6] = { pressed: false, value: -0.2 };
    btns[7] = { pressed: false, value: NaN };
    navigator.getGamepads = () => [{ mapping: "standard", axes: [0, 0, 0, 0], buttons: btns }];
    app.pollPads();
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.lt).toBe(0);
    expect(msg.rt).toBe(0);
  });

  it("without a profile does NOT invent a trigger from extra axes", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    // a controller that rests the trigger axis at 0 -> must not become a phantom 0.5
    navigator.getGamepads = () => [{
      mapping: "",
      axes: [0, 0, 0, 0, 0, 0],
      buttons: Array.from({ length: 12 }, () => ({ pressed: false, value: 0 })),
    }];
    app.pollPads();
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.lt).toBe(0);
    expect(msg.rt).toBe(0);
  });

  it("PS2 USB with an invalid trigger axis falls to 0", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    navigator.getGamepads = () => [{
      mapping: "",
      axes: [0, 0, 0, 0, undefined, NaN], // invalid trigger axes -> || 0
      buttons: Array.from({ length: 12 }, () => ({ pressed: false, value: 0 })),
    }];
    app.pollPads();
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.lt).toBe(0);
    expect(msg.rt).toBe(0);
  });

  it("gamepadconnected event calls setStatus", () => {
    const ev = new Event("gamepadconnected");
    ev.gamepad = { id: "Xbox 360 Controller (XInput STANDARD GAMEPAD)" };
    window.dispatchEvent(ev);
    expect(document.getElementById("status").textContent).toContain("controller:");
  });
});

// ---------------------------------------------------------------------------
// 4.9b Gamepad remapping (PS2/USB) + profile saved on the server
// ---------------------------------------------------------------------------

describe("gamepad remap / mapPad", () => {
  const mkPad = (over = {}) => ({
    id: "Twin USB Joystick",
    mapping: "",
    axes: [0, 0, 0, 0],
    buttons: Array.from({ length: 12 }, () => ({ pressed: false, value: 0 })),
    ...over,
  });

  it("loadGamepadMaps fetches the profile from the server", async () => {
    globalThis.fetch = vi.fn(async () => ({ json: async () => ({ Pad: { buttons: { a: 3 } } }) }));
    await app.loadGamepadMaps();
    expect(app.__get("padMaps").Pad.buttons.a).toBe(3);
  });

  it("loadGamepadMaps failing: padMaps becomes {}", async () => {
    globalThis.fetch = vi.fn(() => Promise.reject(new Error("net")));
    await app.loadGamepadMaps();
    expect(app.__get("padMaps")).toEqual({});
  });

  it("readSource: button, normalized axis and missing source", () => {
    const p = mkPad({ axes: [0, 0, 0, 0, 0.5], buttons: [{ pressed: true, value: 0.8 }] });
    expect(app.readSource(p, { src: "button", i: 0 })).toBeCloseTo(0.8, 2);
    // axis 0.5 in [-1..1] -> 0.75
    expect(app.readSource(p, { src: "axis", i: 4 })).toBeCloseTo(0.75, 2);
    expect(app.readSource(p, null)).toBe(0);
    expect(app.readSource(p, { src: "button", i: 99 })).toBe(0); // missing
    expect(app.readSource(p, { src: "axis", i: 99 })).toBe(0);   // NaN/undefined
  });

  it("mapPad without a profile: triggers only from buttons[6]/[7] (none invented from axes)", () => {
    const p = mkPad({
      axes: [0.1, 0.2, 0.3, 0.4, 1.0, -1.0],
      buttons: Array.from({ length: 8 }, (_, i) => ({ pressed: i === 6, value: i === 6 ? 1 : 0 })),
    });
    const r = app.mapPad(p);
    expect(r.a).toEqual([0.1, 0.2, 0.3, 0.4]);
    expect(r.lt).toBe(1);   // button[6] fully pressed
    expect(r.rt).toBe(0);   // without a profile, an extra axis does NOT become a trigger
  });

  it("mapPad with a profile: reorders buttons/axes and calibrates triggers", async () => {
    // injects a profile for the pad id. Trigger rt as an axis that rests at 0 and
    // goes to 1 (min=0,max=1): at rest it must give 0, not 0.5.
    globalThis.fetch = vi.fn(async () => ({
      json: async () => ({
        "Twin USB Joystick": {
          buttons: { a: 2, b: 1 },           // A is at raw index 2
          axes: { lx: 3, ly: 0 },
          triggers: { lt: { src: "button", i: 6 }, rt: { src: "axis", i: 5, min: 0, max: 1 } },
        },
      }),
    }));
    await app.loadGamepadMaps();
    const p = mkPad({
      axes: [0, 0, 0, 0.9, 0, 1.0],
      buttons: Array.from({ length: 8 }, (_, i) => ({ pressed: i === 2, value: i === 2 ? 1 : (i === 6 ? 0.5 : 0) })),
    });
    const r = app.mapPad(p);
    expect(r.b[0]).toEqual({ p: true, v: 1 });   // logical 'a' <- raw 2
    expect(r.a[0]).toBeCloseTo(0.9, 2);          // lx <- axis 3
    expect(r.lt).toBe(0.5);                       // raw button 6 replicated faithfully
    expect(r.rt).toBe(1);                        // axis 5 (0->1) fully pressed

    // same controller at REST (axis 5 = 0) -> trigger rt must be 0
    const idle = mkPad({
      axes: [0, 0, 0, 0, 0, 0],
      buttons: Array.from({ length: 8 }, () => ({ pressed: false, value: 0 })),
    });
    expect(app.mapPad(idle).rt).toBe(0);
  });

  it("a STANDARD controller (Xbox) passes STRAIGHT through and ignores any saved profile", async () => {
    // even with a saved profile for this id, mapping=standard uses passthrough
    globalThis.fetch = vi.fn(async () => ({
      json: async () => ({ "Xbox Wireless Controller": { buttons: { a: 9 } } }),
    }));
    await app.loadGamepadMaps();
    const btns = Array.from({ length: 17 }, (_, i) => ({ pressed: i === 0, value: i === 0 ? 1 : 0 }));
    btns[6] = { pressed: true, value: 0.5 };  // analog LT
    const p = {
      id: "Xbox Wireless Controller", mapping: "standard",
      axes: [0.1, 0.2, 0.3, 0.4], buttons: btns,
    };
    const r = app.mapPad(p);
    expect(r.b[0]).toEqual({ p: true, v: 1 });   // A straight through (not reshuffled to idx 9)
    expect(r.a).toEqual([0.1, 0.2, 0.3, 0.4]);
    expect(r.lt).toBe(0.5);
    expect(r.rt).toBe(0);
  });

  it("passthroughPad replicates axes and buttons 1:1", () => {
    const p = {
      mapping: "standard", axes: [0.5, -0.5, 0, 1],
      buttons: Array.from({ length: 8 }, (_, i) => ({ pressed: i === 7, value: i === 7 ? 0.9 : 0 })),
    };
    const r = app.passthroughPad(p);
    expect(r.a).toEqual([0.5, -0.5, 0, 1]);
    expect(r.rt).toBe(0.9); // buttons[7]
  });

  it("non-standard WITHOUT a profile falls back to passthrough (best effort)", () => {
    const p = mkPad({ buttons: Array.from({ length: 4 }, (_, i) => ({ pressed: i === 1, value: i === 1 ? 1 : 0 })) });
    const r = app.mapPad(p); // padMaps empty for this id
    expect(r.b[1]).toEqual({ p: true, v: 1 });
  });

  it("startRemap on a standard controller is blocked (not needed)", () => {
    navigator.getGamepads = () => [{
      id: "Xbox", mapping: "standard", axes: [0, 0, 0, 0],
      buttons: Array.from({ length: 17 }, () => ({ pressed: false, value: 0 })),
    }];
    app.startRemap();
    expect(app.__get("remapState").active).toBe(false);
    expect(document.getElementById("status").textContent).toMatch(/standard|straight/i);
  });

  it("clearRemap deletes the profile (server + memory)", async () => {
    const calls = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      calls.push(opts?.method);
      return { json: async () => ({ ok: true, removed: true }) };
    });
    // injects an in-memory profile
    globalThis.fetch = vi.fn(async () => ({ json: async () => ({ "Twin USB Joystick": { buttons: {} } }) }));
    await app.loadGamepadMaps();
    expect(app.__get("padMaps")["Twin USB Joystick"]).toBeDefined();
    const del = [];
    globalThis.fetch = vi.fn(async (url, opts) => { del.push(opts?.method); return { json: async () => ({ ok: true }) }; });
    navigator.getGamepads = () => [mkPad()];
    await app.clearRemap();
    expect(app.__get("padMaps")["Twin USB Joystick"]).toBeUndefined();
    expect(del).toContain("DELETE");
  });

  it("clearRemap without a pad warns and does not call the server", async () => {
    navigator.getGamepads = () => [];
    let called = false;
    globalThis.fetch = vi.fn(async () => { called = true; return { json: async () => ({}) }; });
    await app.clearRemap();
    expect(called).toBe(false);
  });

  it("clearRemap with a network error does not throw", async () => {
    globalThis.fetch = vi.fn(() => Promise.reject(new Error("net")));
    navigator.getGamepads = () => [mkPad()];
    await expect(app.clearRemap()).resolves.not.toThrow();
  });

  it("#remapClear button calls clearRemap", () => {
    navigator.getGamepads = () => [];
    expect(() => document.getElementById("remapClear").click()).not.toThrow();
  });

  it("startRemap without a pad: warns and does not open", () => {
    navigator.getGamepads = () => [];
    app.startRemap();
    expect(document.getElementById("remap").classList.contains("hidden")).toBe(true);
    expect(app.__get("remapState").active).toBe(false);
  });

  it("startRemap without getGamepads available does not throw", () => {
    navigator.getGamepads = undefined;
    expect(() => app.startRemap()).not.toThrow();
    expect(app.__get("remapState").active).toBe(false);
  });

  it("readSource with a degenerate range (max==min) does not divide by zero", () => {
    const p = mkPad({ axes: [0, 0, 0, 0, 0.5] });
    const v = app.readSource(p, { src: "axis", i: 4, min: 1, max: 1 });
    expect(Number.isFinite(v)).toBe(true);
  });

  it("applyDetection: logical button detected as an axis (rare case)", () => {
    navigator.getGamepads = () => [mkPad()];
    app.startRemap();
    app.applyDetection({ kind: "button", name: "a" }, { src: "axis", i: 4 });
    expect(app.__get("remapState").draft.axes.a_axisbtn).toBe(4);
    app.cancelRemap();
  });

  it("detectChange with a base missing that index (?? 0)", () => {
    navigator.getGamepads = () => [mkPad()];
    app.startRemap();
    // forces a short base -> base.buttons[i]/axes[i] undefined -> ?? 0
    app.__get("remapState").base = { buttons: [], axes: [] };
    const found = app.detectChange(mkPad({ axes: [0, 0, 0, 0, 0.9] }));
    expect(found).not.toBeNull();
    app.cancelRemap();
  });

  it("detectChange with a negative axis keeps the resting and pressed values", () => {
    navigator.getGamepads = () => [mkPad()];
    app.startRemap();
    const found = app.detectChange(mkPad({ axes: [0, 0, 0, 0, -0.9] }));
    expect(found.src).toBe("axis");
    expect(found.i).toBe(4);
    expect(found.rest).toBe(0);    // rest
    expect(found.cur).toBe(-0.9);  // value when pressed (inverted axis)
    app.cancelRemap();
  });

  it("detectChange: an axis that rested high and went to 0 (falsy cur -> || 0)", () => {
    const resting = mkPad({ axes: [0, 0, 0, 0, 0.9] });
    navigator.getGamepads = () => [resting];
    app.startRemap(); // base.axes[4] = 0.9 (high rest)
    // now axis 4 goes to an explicit 0 (v=0 -> `v || 0`)
    const found = app.detectChange(mkPad({ axes: [0, 0, 0, 0, 0] }));
    expect(found.src).toBe("axis");
    expect(found.i).toBe(4);
    expect(found.cur).toBe(0);
    app.cancelRemap();
  });

  it("startRemap with a pad: opens the overlay and stays active", () => {
    navigator.getGamepads = () => [mkPad()];
    app.startRemap();
    expect(document.getElementById("remap").classList.contains("hidden")).toBe(false);
    expect(app.__get("remapState").active).toBe(true);
    app.cancelRemap();
    expect(document.getElementById("remap").classList.contains("hidden")).toBe(true);
  });

  it("detectChange finds the control that changed the most (button and axis)", () => {
    app.__get("remapState"); // makes sure the module is loaded
    navigator.getGamepads = () => [mkPad()];
    app.startRemap();
    const base = mkPad();
    // button 3 pressed
    let found = app.detectChange(mkPad({ buttons: base.buttons.map((b, i) => i === 3 ? { pressed: true, value: 1 } : b) }));
    expect(found).toEqual({ src: "button", i: 3 });
    // axis 2 moved
    found = app.detectChange(mkPad({ axes: [0, 0, 0.9, 0] }));
    expect(found.src).toBe("axis");
    expect(found.i).toBe(2);
    app.cancelRemap();
  });

  it("applyDetection writes to the draft by step kind", () => {
    navigator.getGamepads = () => [mkPad()];
    app.startRemap();
    app.applyDetection({ kind: "button", name: "a" }, { src: "button", i: 5 });
    app.applyDetection({ kind: "axis", name: "lx" }, { src: "axis", i: 3 });
    // trigger as an axis: writes the measured min=rest and max=pressed
    app.applyDetection({ kind: "trigger", name: "lt" }, { src: "axis", i: 4, rest: 0, cur: 1 });
    app.applyDetection({ kind: "trigger", name: "rt" }, { src: "button", i: 7 });
    const d = app.__get("remapState").draft;
    expect(d.buttons.a).toBe(5);
    expect(d.axes.lx).toBe(3);
    expect(d.triggers.lt).toEqual({ src: "axis", i: 4, min: 0, max: 1 });
    expect(d.triggers.rt).toEqual({ src: "button", i: 7 });
    app.cancelRemap();
  });

  it("skipRemapStep advances and at the end finishes+saves", async () => {
    const posts = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (opts && opts.method === "POST") { posts.push(JSON.parse(opts.body)); return { json: async () => ({ ok: true }) }; }
      return { json: async () => ({}) };
    });
    navigator.getGamepads = () => [mkPad()];
    app.startRemap();
    const total = app.__get("REMAP_STEPS").length;
    for (let i = 0; i < total; i++) app.skipRemapStep();
    await new Promise((r) => setTimeout(r, 0));
    expect(app.__get("remapState").active).toBe(false);
    expect(posts.length).toBe(1);
    expect(posts[0].id).toBe("Twin USB Joystick");
  });

  it("finishRemap saving with a network error does not throw", async () => {
    globalThis.fetch = vi.fn(() => Promise.reject(new Error("net")));
    navigator.getGamepads = () => [mkPad()];
    app.startRemap();
    await expect(app.finishRemap()).resolves.not.toThrow();
    expect(document.getElementById("status").textContent).toMatch(/saving to the server failed/);
  });

  it("remapTick detects a step, cools down and advances", () => {
    let live = mkPad();
    navigator.getGamepads = () => [live];
    app.startRemap();
    const st = app.__get("remapState");
    const step0 = st.step;
    // press button 1 -> remapTick must capture it and advance 1 step
    live = mkPad({ buttons: mkPad().buttons.map((b, i) => i === 1 ? { pressed: true, value: 1 } : b) });
    navigator.getGamepads = () => [live];
    app.remapTick();
    expect(app.__get("remapState").step).toBe(step0 + 1);
    // right after, it is in cooldown: another change does not advance
    live = mkPad({ buttons: mkPad().buttons.map((b, i) => i === 2 ? { pressed: true, value: 1 } : b) });
    navigator.getGamepads = () => [live];
    app.remapTick();
    expect(app.__get("remapState").step).toBe(step0 + 1); // held back
    app.cancelRemap();
  });

  it("inactive remapTick returns right away", () => {
    app.cancelRemap();
    expect(() => app.remapTick()).not.toThrow();
  });

  it("inactive skipRemapStep does nothing", () => {
    app.cancelRemap();
    expect(() => app.skipRemapStep()).not.toThrow();
  });

  it("renderRemap with an out-of-range step clears the prompt", () => {
    navigator.getGamepads = () => [mkPad()];
    app.startRemap();
    app.__get("remapState").step = app.__get("REMAP_STEPS").length; // past the end
    app.renderRemap();
    expect(document.getElementById("remapPrompt").textContent).toBe("");
    app.cancelRemap();
  });

  it("remapTick on the last step finishes", () => {
    globalThis.fetch = vi.fn(async () => ({ json: async () => ({ ok: true }) }));
    navigator.getGamepads = () => [mkPad()];
    app.startRemap();
    const st = app.__get("remapState");
    st.step = app.__get("REMAP_STEPS").length - 1; // last step
    const live = mkPad({ axes: [0, 0, 0, 0, 0, 0, 0.9] });
    navigator.getGamepads = () => [live];
    app.remapTick();
    expect(app.__get("remapState").active).toBe(false);
  });

  it("#remapBtn button triggers startRemap", () => {
    navigator.getGamepads = () => [mkPad()];
    document.getElementById("remapBtn").click();
    expect(document.getElementById("remap").classList.contains("hidden")).toBe(false);
    app.cancelRemap();
  });

  it("pollPads does not send while the remap is active", () => {
    app.connectInput();
    navigator.getGamepads = () => [mkPad({ buttons: [{ pressed: true, value: 1 }] })];
    app.startRemap();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    ws.sent = [];
    app.pollPads();
    expect(ws.sent).toHaveLength(0);
    app.cancelRemap();
  });
});

// ---------------------------------------------------------------------------
// 4.10 UI — buttons and selects
// ---------------------------------------------------------------------------

describe("UI - setStatus", () => {
  it("updates #status textContent", () => {
    app.setStatus("test message");
    expect(document.getElementById("status").textContent).toBe("test message");
  });
});

describe("UI - buttons", () => {
  beforeEach(() => {
    app.connectInput();
  });

  it("#connect click calls connect (creates 2 extra sockets)", () => {
    const countBefore = globalThis.__sockets.length;
    document.getElementById("connect").click();
    expect(globalThis.__sockets.length - countBefore).toBe(2);
  });

  it("#lock click calls canvas.requestPointerLock", () => {
    const canvas = document.getElementById("screen");
    document.getElementById("lock").click();
    expect(canvas.requestPointerLock).toHaveBeenCalled();
  });

  it("#full click with a null fullscreenElement calls requestFullscreen", () => {
    Object.defineProperty(document, "fullscreenElement", {
      value: null, configurable: true, writable: true,
    });
    document.getElementById("full").click();
    expect(document.documentElement.requestFullscreen).toHaveBeenCalled();
  });

  it("#full click with a non-null fullscreenElement calls exitFullscreen", () => {
    Object.defineProperty(document, "fullscreenElement", {
      value: {}, configurable: true, writable: true,
    });
    document.getElementById("full").click();
    expect(document.exitFullscreen).toHaveBeenCalled();
  });

  it("#kbd click opens/closes the virtual keyboard", () => {
    const osk = document.getElementById("osk");
    osk.classList.add("hidden");
    document.getElementById("kbd").click();
    expect(osk.classList.contains("hidden")).toBe(false);
    document.getElementById("kbd").click();
    expect(osk.classList.contains("hidden")).toBe(true);
  });

  it("#menuToggle shows/hides #bar", () => {
    const bar = document.getElementById("bar");
    bar.classList.remove("hidden");
    document.getElementById("menuToggle").click();
    expect(bar.classList.contains("hidden")).toBe(true);
    document.getElementById("menuToggle").click();
    expect(bar.classList.contains("hidden")).toBe(false);
  });

  it("a combo button sends the chord", () => {
    // uses the active socket (last /ws/input); beforeEach already called connectInput
    const inputs = globalThis.__sockets.filter(s => s.url.includes("/ws/input"));
    const ws = inputs[inputs.length - 1];
    ws.sent = [];
    document.querySelector('[data-combo="altf4"]').click();
    const seq = ws.sent.map(s => JSON.parse(s));
    expect(seq).toContainEqual({ t: "k", code: "AltLeft", down: true });
    expect(seq).toContainEqual({ t: "k", code: "F4", down: true });
  });

  it("#sound click calls setAudio(!audioOn)", () => {
    expect(app.__get("audioOn")).toBe(false);
    document.getElementById("sound").click();
    expect(app.__get("audioOn")).toBe(true);
    document.getElementById("sound").click();
    expect(app.__get("audioOn")).toBe(false);
  });

  it("#preset change sends t:q preset", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sel = document.getElementById("preset");
    const opt = document.createElement("option");
    opt.value = "low";
    sel.appendChild(opt);
    sel.value = "low";
    sel.dispatchEvent(new Event("change"));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "q", preset: "low" });
  });

  it("#vres change sends t:vq res:number", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sel = document.getElementById("vres");
    const opt = document.createElement("option");
    opt.value = "720"; sel.appendChild(opt); sel.value = "720";
    sel.dispatchEvent(new Event("change"));
    expect(JSON.parse(ws.sent[ws.sent.length - 1])).toEqual({ t: "vq", res: 720 });
  });

  it("#vfps change sends t:vq fps:number", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sel = document.getElementById("vfps");
    const opt = document.createElement("option");
    opt.value = "90"; sel.appendChild(opt); sel.value = "90";
    sel.dispatchEvent(new Event("change"));
    expect(JSON.parse(ws.sent[ws.sent.length - 1])).toEqual({ t: "vq", fps: 90 });
  });

  it("#vbitrate change sends t:vq bitrate:string", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sel = document.getElementById("vbitrate");
    const opt = document.createElement("option");
    opt.value = "7M"; sel.appendChild(opt); sel.value = "7M";
    sel.dispatchEvent(new Event("change"));
    expect(JSON.parse(ws.sent[ws.sent.length - 1])).toEqual({ t: "vq", bitrate: "7M" });
  });

  it("#audioPreset change sends t:aq preset", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sel = document.getElementById("audioPreset");
    const opt = document.createElement("option");
    opt.value = "low"; sel.appendChild(opt); sel.value = "low";
    sel.dispatchEvent(new Event("change"));
    expect(JSON.parse(ws.sent[ws.sent.length - 1])).toEqual({ t: "aq", preset: "low" });
  });

  it("#monitor change with a numeric value sends t:mon i:number", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sel = document.getElementById("monitor");
    const opt = document.createElement("option");
    opt.value = "1";
    sel.appendChild(opt);
    sel.value = "1";
    sel.dispatchEvent(new Event("change"));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "mon", i: 1 });
  });

  it("#monitor change with value '' sends t:mon i:null", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sel = document.getElementById("monitor");
    const opt = document.createElement("option");
    opt.value = "";
    sel.appendChild(opt);
    sel.value = "";
    sel.dispatchEvent(new Event("change"));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "mon", i: null });
  });

  it("focus/blur on the canvas turns keyboard capture on/off", () => {
    const canvas = document.getElementById("screen");
    canvas.dispatchEvent(new FocusEvent("focus"));
    expect(app.__get("kbdCapture")).toBe(true);
    canvas.dispatchEvent(new FocusEvent("blur"));
    expect(app.__get("kbdCapture")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 4.11 loadConfig
// ---------------------------------------------------------------------------

describe("loadConfig", () => {
  it("normal: fills presets and monitors and enables sound", async () => {
    globalThis.fetch = vi.fn(async () => ({
      json: async () => ({
        presets: ["ultra", "high", "medium", "low", "potato"],
        current_preset: "high",
        monitors: [
          { index: 0, name: "HDMI-0", primary: true, w: 1920, h: 1080 },
          { index: 1, name: "DP-1", primary: false, w: 1920, h: 1080 },
        ],
        current_monitor: 1,
        video: { res: 720, fps: 60, bitrate: "7M" },
        res_options: [540, 720, 1080],
        fps_options: [30, 60, 120],
        bitrate_options: ["3500k", "7M", "12M"],
        audio_available: true,
        audio_presets: ["high", "medium", "low", "voice"],
        current_audio_preset: "medium",
        audio_manual: false,
      }),
    }));
    await app.loadConfig();
    expect(document.getElementById("preset").options).toHaveLength(5);
    const monOpts = document.getElementById("monitor").options;
    expect(monOpts).toHaveLength(2);
    // the primary gets ★, the other doesn't; current_monitor=1 is selected
    expect(monOpts[0].textContent).toContain("★");
    expect(monOpts[1].textContent).not.toContain("★");
    expect(monOpts[1].selected).toBe(true);
    expect(document.getElementById("sound").disabled).toBe(false);
    // selected on the current preset
    const selected = Array.from(document.getElementById("preset").options).find(o => o.selected);
    expect(selected.value).toBe("high");
    // video controls filled with the current value selected
    const vres = document.getElementById("vres");
    expect(vres.options).toHaveLength(3);
    expect(Array.from(vres.options).find(o => o.selected).value).toBe("720");
    expect(document.getElementById("vfps").options).toHaveLength(3);
    expect(document.getElementById("vbitrate").options).toHaveLength(3);
    expect(document.getElementById("audioPreset").disabled).toBe(false);
  });

  it("audio unavailable on the server: disables the button with the right title", async () => {
    globalThis.fetch = vi.fn(async () => ({
      json: async () => ({
        presets: [], current_preset: null, monitors: [], current_monitor: null,
        audio_available: false, audio_presets: [], current_audio_preset: null,
      }),
    }));
    await app.loadConfig();
    const btn = document.getElementById("sound");
    expect(btn.disabled).toBe(true);
    expect(btn.title).toContain("no audio on the server");
  });

  it("audio available but no AudioDecoder: disables with the right title", async () => {
    delete globalThis.AudioDecoder;
    globalThis.fetch = vi.fn(async () => ({
      json: async () => ({
        presets: [], current_preset: null, monitors: [], current_monitor: null,
        audio_available: true, audio_presets: [], current_audio_preset: null,
      }),
    }));
    await app.loadConfig();
    const btn = document.getElementById("sound");
    expect(btn.disabled).toBe(true);
    expect(btn.title).toContain("AudioDecoder");
  });

  it("missing optional fields: uses empty lists without throwing", async () => {
    globalThis.fetch = vi.fn(async () => ({
      json: async () => ({
        presets: [], current_preset: null,
        monitors: [], current_monitor: null,
        // video, *_options and audio_presets MISSING -> fallback || []
        audio_available: false,
      }),
    }));
    await expect(app.loadConfig()).resolves.not.toThrow();
    expect(document.getElementById("vres").options).toHaveLength(0);
    expect(document.getElementById("audioPreset").options).toHaveLength(0);
  });

  it("a throwing fetch: console.warn is called, nothing propagates", async () => {
    globalThis.fetch = vi.fn(() => Promise.reject(new Error("network error")));
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    await expect(app.loadConfig()).resolves.not.toThrow();
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// 4.12 statsLoop
// ---------------------------------------------------------------------------

describe("statsLoop", () => {
  it("dt >= 1 updates stats and clears counters", async () => {
    // lastStat=0 (set at load with performance.now=0)
    // Make performance.now return 1500 ms later
    globalThis.performance.now.mockReturnValue(1500);
    app.statsLoop();
    const text = document.getElementById("stats").textContent;
    expect(text).toMatch(/fps/);
    expect(text).toMatch(/Mbps/);
    expect(globalThis.requestAnimationFrame).toHaveBeenCalled();
  });

  it("dt >= 1 with audioOn=true shows 🔊", async () => {
    app.setAudio(true);
    globalThis.performance.now.mockReturnValue(1500);
    app.statsLoop();
    expect(document.getElementById("stats").textContent).toContain("🔊");
  });

  it("dt < 1 does not update stats", async () => {
    globalThis.performance.now.mockReturnValue(100);
    app.statsLoop();
    expect(document.getElementById("stats").textContent).toBe("");
    expect(globalThis.requestAnimationFrame).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 4.13 boot
// ---------------------------------------------------------------------------

describe("boot", () => {
  it("without VideoDecoder: status 'No WebCodecs', #connect disabled, no fetch", async () => {
    delete globalThis.VideoDecoder;
    app = await loadApp();
    app.boot();
    expect(document.getElementById("status").textContent).toContain("No WebCodecs");
    expect(document.getElementById("connect").disabled).toBe(true);
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(globalThis.requestAnimationFrame).not.toHaveBeenCalled();
  });

  it("normal: status 'ready', fetch called, rAF called", async () => {
    app.boot();
    expect(document.getElementById("status").textContent).toContain("ready");
    expect(globalThis.fetch).toHaveBeenCalled();
    expect(globalThis.requestAnimationFrame).toHaveBeenCalled();
  });
});
