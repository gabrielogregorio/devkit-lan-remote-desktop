import { beforeEach, vi } from "vitest";

const IDS = [
  "screen", "overlay", "status", "stats", "connect", "lock", "full",
  "kbd", "menuToggle", "sound", "preset", "monitor",
  "vres", "vfps", "vbitrate", "audioPreset",
  "bar", "osk", "menuRecall",
  "remapBtn", "remapClear", "remap", "remapTitle", "remapPrompt",
  "remapSkip", "remapCancel",
];
const SELECTS = ["preset", "monitor", "vres", "vfps", "vbitrate", "audioPreset"];

export function mountDOM() {
  document.body.innerHTML = "";
  for (const id of IDS) {
    const el =
      id === "screen" ? document.createElement("canvas") :
      SELECTS.includes(id) ? document.createElement("select") :
      document.createElement("div");
    el.id = id;
    document.body.appendChild(el);
  }
  // #osk e #remap começam escondidos (como no HTML real)
  document.getElementById("osk").className = "hidden";
  document.getElementById("remap").className = "hidden";
  // botões de combo (data-combo) que app.js liga no boot
  for (const c of ["altf4", "alttab", "ctrlaltdel", "super"]) {
    const b = document.createElement("button");
    b.dataset.combo = c;
    b.textContent = c;
    document.getElementById("bar").appendChild(b);
  }
  const cv = document.getElementById("screen");
  cv.getContext = () => ({ drawImage: vi.fn() });
  cv.requestPointerLock = vi.fn();
  cv.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1920, height: 1080 });

  // stub fullscreen APIs
  document.documentElement.requestFullscreen = vi.fn().mockResolvedValue(undefined);
  document.exitFullscreen = vi.fn().mockResolvedValue(undefined);
}

export function stubBrowser() {
  globalThis.__sockets = [];
  globalThis.WebSocket = class {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.sent = [];
      this.binaryType = "blob";
      globalThis.__sockets.push(this);
    }
    send(d) { this.sent.push(d); }
    close() {
      this.readyState = 3;
      if (this.onclose) this.onclose();
    }
  };

  globalThis.VideoDecoder = class {
    constructor(cfg) { this.cfg = cfg; this.state = "unconfigured"; }
    configure(c) { this.state = "configured"; this.configured = c; }
    decode(chunk) { (this.decoded = this.decoded || []).push(chunk); }
    close() { this.state = "closed"; }
    static async isConfigSupported(cfg) { return { supported: true, config: cfg }; }
  };
  globalThis.AudioDecoder = class {
    constructor(cfg) { this.cfg = cfg; this.state = "unconfigured"; }
    configure(c) { this.state = "configured"; }
    decode(chunk) { (this.decoded = this.decoded || []).push(chunk); }
    close() { this.state = "closed"; }
  };
  globalThis.EncodedVideoChunk = class { constructor(o) { Object.assign(this, o); } };
  globalThis.EncodedAudioChunk = class { constructor(o) { Object.assign(this, o); } };

  globalThis.AudioContext = class {
    constructor() { this.state = "running"; this.currentTime = 0; this.destination = {}; }
    createBuffer(ch, n, sr) {
      return { duration: n / (sr || 48000), copyToChannel: vi.fn() };
    }
    createBufferSource() {
      return { connect: vi.fn(), start: vi.fn(), buffer: null };
    }
    resume() { this.state = "running"; return Promise.resolve(); }
    suspend() { this.state = "suspended"; return Promise.resolve(); }
  };

  globalThis.requestAnimationFrame = vi.fn();
  // Force deterministic performance.now so lastStat=0 at module load
  globalThis.performance = { now: vi.fn(() => 0) };
  navigator.getGamepads = () => [];

  globalThis.fetch = vi.fn(async () => ({
    json: async () => ({
      presets: ["ultra", "high", "medium", "low", "potato"],
      current_preset: "high",
      video: { res: 900, fps: 60, bitrate: "12M" },
      res_options: [360, 480, 540, 720, 900, 1080],
      fps_options: [30, 45, 60, 90, 120],
      bitrate_options: ["1200k", "2500k", "3500k", "7M", "12M", "25M"],
      monitors: [{ index: 0, name: "HDMI-0", primary: true, w: 1920, h: 1080 }],
      current_monitor: null,
      audio_available: false,
      audio_presets: ["alta", "media", "baixa", "voz"],
      current_audio_preset: "media",
      audio_manual: false,
    }),
  }));
}

export async function loadApp() {
  globalThis.__sockets = [];
  vi.resetModules();
  return await import("../app.js");
}

beforeEach(() => {
  mountDOM();
  stubBrowser();
});
