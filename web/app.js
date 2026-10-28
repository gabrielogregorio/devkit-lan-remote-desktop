/* LAN Remote Desktop client
   video: binary WebSocket -> H.264 Access Units -> WebCodecs VideoDecoder -> canvas
   audio: binary WebSocket -> Opus packets -> WebCodecs AudioDecoder -> AudioContext
   input: mouse/keyboard/gamepad -> text WebSocket JSON
   strings: English in the code, pt-BR from i18n.js (loaded before this file)
*/
"use strict";

// i18n.js is a global (`I18N`) in the browser and a CommonJS module under vitest.
const I18N =
  typeof module !== "undefined" && module.exports ? require("./i18n.js") : window.I18N;
const LANG = I18N.detectLanguage(
  location.search,
  navigator.languages || [navigator.language],
);
const t = I18N.translator(LANG);

const $ = (id) => document.getElementById(id);
const canvas = $("screen");
const ctx = canvas.getContext("2d", { alpha: false, desynchronized: true });

const wsBase = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;

let videoWS = null,
  inputWS = null;
let decoder = null,
  configured = false,
  configuring = false,
  needKey = true, // the 1st chunk after configure()/reset MUST be a keyframe
  codecStr = null;
let tsCounter = 0;
let frames = 0,
  bytes = 0,
  lastStat = performance.now();
let lastFrameAt = 0,
  decodeLatency = 0;

// ---------- WebCodecs (video) ----------
function hex2(n) {
  return n.toString(16).padStart(2, "0");
}

function findNALs(au) {
  const out = [];
  for (let i = 0; i + 3 < au.length; i++) {
    if (au[i] === 0 && au[i + 1] === 0 && au[i + 2] === 1) {
      out.push({ type: au[i + 3] & 0x1f, hdr: i + 3 });
      i += 2;
    }
  }
  return out;
}

function codecFromSPS(au, spsHdr) {
  const p = au[spsHdr + 1],
    c = au[spsHdr + 2],
    l = au[spsHdr + 3];
  return `avc1.${hex2(p)}${hex2(c)}${hex2(l)}`;
}

function ensureDecoder() {
  if (decoder && decoder.state !== "closed") return;
  decoder = new VideoDecoder({
    output: (frame) => {
      if (
        canvas.width !== frame.displayWidth ||
        canvas.height !== frame.displayHeight
      ) {
        canvas.width = frame.displayWidth;
        canvas.height = frame.displayHeight;
      }
      ctx.drawImage(frame, 0, 0);
      frame.close();
      frames++;
      const now = performance.now();
      if (lastFrameAt)
        decodeLatency = decodeLatency * 0.9 + (now - lastFrameAt) * 0.1;
      lastFrameAt = now;
    },
    error: (e) => {
      console.error("decoder", e);
      resetDecoder();
    },
  });
}

function resetDecoder() {
  configured = false;
  configuring = false;
  needKey = true;
  codecStr = null;
  try {
    if (decoder && decoder.state !== "closed") decoder.close();
  } catch (e) {}
  decoder = null;
}

// Chrome on Linux often fails with hardwareAcceleration:"prefer-hardware"
// (unstable VA-API/NVDEC path) where Firefox accepts it. So we probe with
// isConfigSupported() and fall back to "no-preference" -> "prefer-software".
// Configuration is async; the 1st keyframe waits until the decoder is ready.
async function configureDecoder(codec) {
  const bases = [
    { hardwareAcceleration: "prefer-hardware" },
    { hardwareAcceleration: "no-preference" },
    { hardwareAcceleration: "prefer-software" },
  ];
  for (const extra of bases) {
    const cfg = {
      codec,
      optimizeForLatency: true,
      codedWidth: canvas.width || 1920,
      codedHeight: canvas.height || 1080,
      ...extra,
    };
    try {
      const sup = await VideoDecoder.isConfigSupported(cfg);
      if (!sup || !sup.supported) continue;
      ensureDecoder();
      decoder.configure(sup.config || cfg);
      return true;
    } catch (e) {
      console.warn("configure failed with", extra.hardwareAcceleration, e);
      resetDecoder();
    }
  }
  setStatus(t("this browser could not start the H.264 decoder"));
  return false;
}

function onAU(buf) {
  const au = new Uint8Array(buf);
  bytes += au.length;
  const nals = findNALs(au);
  const isKey = nals.some((n) => n.type === 5 || n.type === 7);

  if (!configured) {
    if (configuring) return; // async probe in progress; wait for the next keyframe
    const sps = nals.find((n) => n.type === 7);
    if (!sps) return;
    codecStr = codecFromSPS(au, sps.hdr);
    configuring = true;
    configureDecoder(codecStr).then((ok) => {
      configuring = false;
      configured = ok;
      needKey = true; // decoding only starts at the next keyframe
    });
    return; // this AU only configures; the next keyframe starts decoding
  }

  // VideoDecoder REQUIRES the 1st chunk after configure()/flush to be a keyframe.
  // Since configuration is async and the GOP is long, the AUs that arrive right
  // after are usually delta -> drop them until a keyframe shows up (otherwise
  // Firefox and Chrome throw "key frame is required").
  if (needKey) {
    if (!isKey) return;
    needKey = false;
  }

  try {
    decoder.decode(
      new EncodedVideoChunk({
        type: isKey ? "key" : "delta",
        timestamp: tsCounter++,
        data: au,
      }),
    );
  } catch (e) {
    console.warn("decode failed, resyncing", e);
    resetDecoder();
  }
}

// ---------- WebCodecs (audio) ----------
const AUDIO_SR = 48000,
  AUDIO_CH = 2;
let audioWS = null,
  audioCtx = null,
  audioDecoder = null;
let audioOn = false; // does the user want sound?
let playHead = 0; // scheduling clock (s)
let audioTs = 0;
let audioBytes = 0,
  audioPkts = 0;
const JITTER = 0.06; // target buffer (s) - latency vs stability trade-off

function startAudioContext() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: AUDIO_SR,
      latencyHint: "interactive",
    });
  }
  if (audioCtx.state === "suspended") audioCtx.resume();
}

function ensureAudioDecoder() {
  if (audioDecoder && audioDecoder.state !== "closed") return;
  audioDecoder = new AudioDecoder({
    output: (data) => playAudio(data),
    error: (e) => {
      console.error("audioDecoder", e);
      resetAudioDecoder();
    },
  });
  audioDecoder.configure({
    codec: "opus",
    sampleRate: AUDIO_SR,
    numberOfChannels: AUDIO_CH,
  });
}

function resetAudioDecoder() {
  try {
    if (audioDecoder && audioDecoder.state !== "closed") audioDecoder.close();
  } catch (e) {}
  audioDecoder = null;
  playHead = 0;
}

function playAudio(data) {
  if (!audioCtx) {
    data.close();
    return;
  }
  const n = data.numberOfFrames,
    ch = data.numberOfChannels;
  const buf = audioCtx.createBuffer(ch, n, data.sampleRate || AUDIO_SR);
  const tmp = new Float32Array(n);
  for (let c = 0; c < ch; c++) {
    try {
      data.copyTo(tmp, { planeIndex: c, format: "f32-planar" });
    } catch (e) {
      data.copyTo(tmp, { planeIndex: c });
    }
    buf.copyToChannel(tmp, c);
  }
  data.close();

  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(audioCtx.destination);

  const now = audioCtx.currentTime;
  // re-anchor if we fell behind (playHead in the past) OR if the scheduled buffer
  // grew too much (a burst piled up latency) -> stay real time, never stack up.
  if (playHead < now + 0.005 || playHead > now + JITTER + 0.12) {
    playHead = now + JITTER;
  }
  src.start(playHead);
  playHead += buf.duration;
}

function onAudioPacket(arrbuf) {
  audioBytes += arrbuf.byteLength;
  audioPkts++;
  ensureAudioDecoder();
  try {
    audioDecoder.decode(
      new EncodedAudioChunk({
        type: "key", // every Opus packet decodes on its own
        timestamp: audioTs,
        data: arrbuf,
      }),
    );
    audioTs += 20000; // 20ms per frame (µs)
  } catch (e) {
    console.warn("audio decode failed", e);
    resetAudioDecoder();
  }
}

function connectAudio() {
  if (!audioOn) return;
  audioWS = new WebSocket(`${wsBase}/ws/audio`);
  audioWS.binaryType = "arraybuffer";
  audioWS.onmessage = (ev) => {
    if (audioOn) onAudioPacket(ev.data);
  };
  audioWS.onclose = () => {
    audioWS = null;
    if (audioOn) setTimeout(connectAudio, 1000);
  };
  audioWS.onerror = () => {
    try {
      audioWS.close();
    } catch (e) {}
  };
}

function setAudio(on) {
  audioOn = on;
  const btn = $("sound");
  if (on) {
    btn.textContent = t("🔊 sound");
    btn.classList.add("on");
    startAudioContext();
    if (!audioWS) connectAudio();
  } else {
    btn.textContent = t("🔇 sound");
    btn.classList.remove("on");
    if (audioWS) {
      try {
        audioWS.close();
      } catch (e) {}
      audioWS = null;
    }
    resetAudioDecoder();
    if (audioCtx) audioCtx.suspend();
  }
}

// ---------- connection ----------
function connect() {
  $("overlay").classList.add("hidden");
  connectInput();
  connectVideo();
}

function connectVideo() {
  videoWS = new WebSocket(`${wsBase}/ws/video`);
  videoWS.binaryType = "arraybuffer";
  videoWS.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      try {
        if (JSON.parse(ev.data).t === "reset") resetDecoder();
      } catch (e) {}
      return;
    }
    onAU(ev.data);
  };
  videoWS.onclose = () => {
    resetDecoder();
    setStatus(t("video disconnected - reconnecting…"));
    setTimeout(() => {
      if (document.visibilityState === "visible") connectVideo();
    }, 1000);
  };
  videoWS.onerror = () => videoWS.close();
}

function connectInput() {
  inputWS = new WebSocket(`${wsBase}/ws/input`);
  inputWS.onclose = () => setTimeout(connectInput, 1000);
}

function sendInput(obj) {
  if (inputWS && inputWS.readyState === 1) inputWS.send(JSON.stringify(obj));
}

// ---------- mouse ----------
let pendingMove = null;
function flushMove() {
  if (pendingMove) {
    sendInput(pendingMove);
    pendingMove = null;
  }
  requestAnimationFrame(flushMove);
}

// The REAL rectangle where the video is drawn inside the <canvas>. Since the
// canvas uses object-fit:contain to fill the screen, black bars may be left (e.g.
// the source is 16:9 and the phone is upright). The mouse has to map to this
// rect, not to the whole element, or clicks/moves miss by the size of the bar.
function displayedRect() {
  const r = canvas.getBoundingClientRect();
  const bw = canvas.width || r.width;
  const bh = canvas.height || r.height;
  const scale = Math.min(r.width / bw, r.height / bh);
  const w = bw * scale,
    h = bh * scale;
  return {
    left: r.left + (r.width - w) / 2,
    top: r.top + (r.height - h) / 2,
    width: w,
    height: h,
  };
}

function normFromEvent(e) {
  const r = displayedRect();
  return {
    x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
    y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
  };
}

canvas.addEventListener("mousemove", (e) => {
  if (document.pointerLockElement === canvas) {
    const r = displayedRect();
    sendInput({
      t: "mr",
      dx: e.movementX / r.width,
      dy: e.movementY / r.height,
    });
  } else {
    const n = normFromEvent(e);
    pendingMove = { t: "m", x: n.x, y: n.y };
  }
});
canvas.addEventListener("mousedown", (e) => {
  e.preventDefault();
  sendInput({ t: "md", b: e.button });
});
canvas.addEventListener("mouseup", (e) => {
  e.preventDefault();
  sendInput({ t: "mu", b: e.button });
});
canvas.addEventListener("contextmenu", (e) => e.preventDefault());
canvas.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    sendInput({ t: "w", x: Math.sign(e.deltaX), y: Math.sign(e.deltaY) });
  },
  { passive: false },
);

// ---------- touch (mobile): tap = click, drag = move ----------
let touchId = null,
  touchMoved = false;
canvas.addEventListener(
  "touchstart",
  (e) => {
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    touchId = t.identifier;
    touchMoved = false;
    const n = normFromEvent(t);
    sendInput({ t: "m", x: n.x, y: n.y });
    e.preventDefault();
  },
  { passive: false },
);
canvas.addEventListener(
  "touchmove",
  (e) => {
    for (const t of e.touches)
      if (t.identifier === touchId) {
        touchMoved = true;
        const n = normFromEvent(t);
        pendingMove = { t: "m", x: n.x, y: n.y };
      }
    e.preventDefault();
  },
  { passive: false },
);
canvas.addEventListener(
  "touchend",
  (e) => {
    if (!touchMoved) {
      sendInput({ t: "md", b: 0 });
      setTimeout(() => sendInput({ t: "mu", b: 0 }), 40);
    }
    touchId = null;
    e.preventDefault();
  },
  { passive: false },
);

// ---------- keyboard ----------
// Capture ONLY while the canvas has focus: clicking the remote screen sends the
// physical keyboard to the PC; clicking outside (or leaving focus) hands control
// back to the local browser. So Tab/F-keys/shortcuts don't leak into the browser
// while you are "inside".
let kbdCapture = false;
canvas.tabIndex = 0; // allows focus/blur on the canvas

function setKbdCapture(on) {
  kbdCapture = on;
  document.body.classList.toggle("kbd-capture", on);
  if (on) setStatus(t("keyboard captured - click outside to release"));
}
canvas.addEventListener("focus", () => setKbdCapture(true));
canvas.addEventListener("blur", () => setKbdCapture(false));
canvas.addEventListener("mousedown", () => canvas.focus());

window.addEventListener(
  "keydown",
  (e) => {
    if (!kbdCapture || !inputWS || inputWS.readyState !== 1) return;
    // F11 stays reserved for the local browser's fullscreen; never sent.
    if (e.code === "F11") return;
    e.preventDefault();
    sendInput({ t: "k", code: e.code, down: true });
  },
  true,
);
window.addEventListener(
  "keyup",
  (e) => {
    if (!kbdCapture || !inputWS || inputWS.readyState !== 1) return;
    if (e.code === "F11") return;
    e.preventDefault();
    sendInput({ t: "k", code: e.code, down: false });
  },
  true,
);

// ---------- combos / virtual keyboard ----------
// Sends a key sequence as a chord: presses all in order, releases in reverse
// order. Needs no local shortcut (doesn't clash with Win/Ubuntu/phone).
function sendChord(codes) {
  for (const c of codes) sendInput({ t: "k", code: c, down: true });
  for (let i = codes.length - 1; i >= 0; i--)
    sendInput({ t: "k", code: codes[i], down: false });
}

const COMBOS = {
  altf4: ["AltLeft", "F4"],
  ctrlaltdel: ["ControlLeft", "AltLeft", "Delete"],
  alttab: ["AltLeft", "Tab"],
  super: ["MetaLeft"],
  ctrlc: ["ControlLeft", "KeyC"],
  ctrlv: ["ControlLeft", "KeyV"],
  esc: ["Escape"],
};

// ---------- gamepad ----------
// Logical Xbox order the server expects in b[] (standard indices):
// 0=A 1=B 2=X 3=Y 4=LB 5=RB 6=LT 7=RT 8=Back 9=Start 10=LS 11=RS
// 12=DPup 13=DPdown 14=DPleft 15=DPright 16=Guide. a[]=[lx,ly,rx,ry].
const PAD_BUTTONS = [
  "a", "b", "x", "y", "lb", "rb", "lt", "rt", "back", "start",
  "ls", "rs", "dup", "ddown", "dleft", "dright", "guide",
];
const PAD_AXES = ["lx", "ly", "rx", "ry"];
let padMaps = {}; // { "<gamepad.id>": {buttons:{a:idx,...}, axes:{lx:idx,...},
                  //   triggers:{lt:{src,i,...}, rt:{...}} } }

async function loadGamepadMaps() {
  try {
    padMaps = await (await fetch("/api/gamepad-map")).json();
  } catch (e) {
    padMaps = {};
  }
}

// FAITHFUL trigger: replicates the controller's raw 0..1 value, with no
// deadzone/rescale/quantization. Distorting it (as before) is what caused the
// "double click": the deadzone/threshold flickered near the edge and every
// crossing became press/release. The server does round(v*255) -> ABS_Z; evdev
// already dedups equal values, so there is no jitter to "smooth". Only clamps to [0,1].
function clamp01(v) {
  if (!(v >= 0)) return 0; // NaN/negative/undefined -> released
  return v > 1 ? 1 : v;
}

// A usable number or the default. A knock-off controller (or an odd driver)
// may report undefined/NaN/Infinity on an axis or button; letting it through
// becomes NaN in the JSON -> null on the server -> crazy axis/trigger.
function padNum(v, fallback = 0) {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

// reads a trigger/axis source from the profile: {src:"axis",i,min,max} or {src:"button",i}
function readSource(p, s) {
  if (!s) return 0;
  if (s.src === "button") {
    const btn = Array.isArray(p.buttons) ? p.buttons[s.i] : null;
    return btn ? clamp01(padNum(btn.value)) : 0;
  }
  // axis: normalizes [min(rest)..max(pressed)] to 0..1. Supports an inverted
  // axis (max < min) because a PS2/USB controller may go from 0 to -1.
  const v = Array.isArray(p.axes) ? p.axes[s.i] : undefined;
  // only a finite number counts: missing/NaN/garbage -> released (0). Careful: you
  // CAN'T "turn it into 0 and normalize", because 0 on a -1..1 axis is half pressed.
  if (typeof v !== "number" || !Number.isFinite(v)) return 0;
  const min = padNum(s.min, -1), max = padNum(s.max, 1);
  const span = max - min;
  if (Math.abs(span) < 1e-6) return 0; // degenerate calibration -> treat as released
  return Math.min(1, Math.max(0, (v - min) / span));
}

// STRAIGHT passthrough: the browser already reports the controller in the W3C
// standard order ("standard mapping"). For an Xbox that is exactly the order the
// server wants (b[0]=A..b[16]=Guide, a[0..3]=sticks, b[6]/b[7]=analog triggers).
// So we forward it as is, untouched. It is the path of ANY controller the
// browser recognizes (Xbox, DualShock, etc.).
function passthroughPad(p) {
  const ax = Array.isArray(p.axes) ? p.axes : [];
  const btns = Array.isArray(p.buttons) ? p.buttons : [];
  const a = [padNum(ax[0]), padNum(ax[1]), padNum(ax[2]), padNum(ax[3])];
  const b = btns.map((x) => ({
    p: !!(x && x.pressed),
    v: +clamp01(padNum(x && x.value)).toFixed(3),
  }));
  const lt = clamp01(padNum(btns[6] && btns[6].value));
  const rt = clamp01(padNum(btns[7] && btns[7].value));
  return { a, b, lt, rt };
}

// Translates the raw pad -> standard format.
// RULE: a "standard" controller (Xbox and most others) passes STRAIGHT through,
// with no profile at all (an Xbox never needs a remap). Only a NON-standard
// controller (PS2/USB clone) uses the saved remap profile - and only if one exists.
function mapPad(p) {
  if (p.mapping === "standard") return passthroughPad(p);
  const m = padMaps[p.id];
  if (!m) return passthroughPad(p); // no profile: best effort (straight through)
  // with a profile (non-standard): builds b[] in logical order from the indices
  const b = PAD_BUTTONS.map((name) => {
    const idx = m.buttons?.[name];
    const btn = idx != null && Array.isArray(p.buttons) ? p.buttons[idx] : null;
    return {
      p: !!(btn && btn.pressed),
      v: btn ? +clamp01(padNum(btn.value)).toFixed(3) : 0,
    };
  });
  const a = PAD_AXES.map((name) => {
    const idx = m.axes?.[name];
    return idx != null && Array.isArray(p.axes) ? padNum(p.axes[idx]) : 0;
  });
  const lt = clamp01(readSource(p, m.triggers?.lt));
  const rt = clamp01(readSource(p, m.triggers?.rt));
  return { a, b, lt, rt };
}

let padSnap = "";
function pollPads() {
  // try/catch around EVERYTHING (getGamepads included): if anything escapes
  // here, the requestAnimationFrame at the end never runs and polling DIES for
  // the rest of the session - the controller just stops working, silently.
  try {
    const pads = navigator.getGamepads ? navigator.getGamepads() : [];
    let p = null;
    for (const g of pads)
      if (g) {
        p = g;
        break;
      }
    if (p && !remapState.active) {
      const { a, b, lt, rt } = mapPad(p);
      // Dedup at the DESTINATION RESOLUTION: the server writes an axis as int16
      // and a button/trigger as a 0..255 byte, and evdev drops repeated values.
      // So comparing the raw float would resend on every bit of controller
      // micro-noise - wasted traffic and CPU, 60x/s. What GOES on the wire is
      // still the faithful value; only the comparison is quantized.
      const ltByte = Math.round(lt * 255), rtByte = Math.round(rt * 255);
      const snap = JSON.stringify([
        a.map((v) => Math.round(v * 32767)),
        b.map((x) => [x.p, Math.round(x.v * 255)]),
        ltByte, rtByte,
      ]);
      if (snap !== padSnap) {
        padSnap = snap;
        sendInput({ t: "g", a, b, lt, rt });
      }
    }
  } catch (e) {
    console.warn("gamepad", e);
  }
  requestAnimationFrame(pollPads);
}
window.addEventListener("gamepadconnected", (e) => {
  setStatus(t("controller: {id}", { id: e.gamepad.id.slice(0, 28) }));
});

// ---------- gamepad remapping ----------
// "Press button X" flow: for each logical control, captures which raw pad
// button/axis was pressed. Meant for PS2/USB controllers with chaotic mappings.
// Each step: logical name + whether it is a button, an axis (stick) or a trigger.
const REMAP_STEPS = [
  ...PAD_BUTTONS.filter((n) => n !== "lt" && n !== "rt")
    .map((n) => ({ kind: "button", name: n })),
  { kind: "trigger", name: "lt" },
  { kind: "trigger", name: "rt" },
  { kind: "axis", name: "lx" }, { kind: "axis", name: "ly" },
  { kind: "axis", name: "rx" }, { kind: "axis", name: "ry" },
];
const STEP_LABEL = {
  a: "A", b: "B", x: "X", y: "Y", lb: t("LB (left bumper)"), rb: t("RB (right bumper)"),
  back: "Back/Select", start: "Start", ls: t("L3 (click left stick)"),
  rs: t("R3 (click right stick)"), guide: t("Guide/Home (or skip)"),
  dup: "D-Pad ↑", ddown: "D-Pad ↓", dleft: "D-Pad ←", dright: "D-Pad →",
  lt: t("LT (left trigger) - press all the way down"),
  rt: t("RT (right trigger) - press all the way down"),
  lx: t("LEFT stick → right"), ly: t("LEFT stick → down"),
  rx: t("RIGHT stick → right"), ry: t("RIGHT stick → down"),
};
const remapState = { active: false, step: 0, draft: null, base: null, padId: null };

function curPad() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  for (const g of pads) if (g) return g;
  return null;
}

function startRemap() {
  const p = curPad();
  if (!p) {
    setStatus(t("connect a controller and move it before remapping"));
    return;
  }
  // a "standard" controller (Xbox and the like) does NOT need a remap - it passes
  // straight through. Remapping a standard one only breaks it. Block and say so.
  if (p.mapping === "standard") {
    setStatus(t("standard controller (Xbox) - already passes straight through, no remap needed"));
    return;
  }
  remapState.active = true;
  remapState.step = 0;
  remapState.padId = p.id;
  remapState.draft = { buttons: {}, axes: {}, triggers: {} };
  // snapshot of the resting state to detect what CHANGED
  remapState.base = { buttons: p.buttons.map((b) => b.value), axes: p.axes.slice() };
  $("remap").classList.remove("hidden");
  renderRemap();
  requestAnimationFrame(remapTick);
}

function cancelRemap() {
  remapState.active = false;
  $("remap").classList.add("hidden");
}

function renderRemap() {
  const step = REMAP_STEPS[remapState.step];
  $("remapTitle").textContent = t("Remap ({step}/{total})", {
    step: remapState.step + 1,
    total: REMAP_STEPS.length,
  });
  // STEP_LABEL covers every name; the `|| name` is only a safety net.
  /* v8 ignore next 2 */
  const label = step ? (STEP_LABEL[step.name] || step.name) : "";
  $("remapPrompt").textContent = step ? t("Press: {label}", { label }) : "";
}

// detects the raw control that moved the most since rest
function detectChange(p) {
  const base = remapState.base;
  let best = null, bestDelta = 0.35; // threshold to ignore noise
  p.buttons.forEach((b, i) => {
    const d = Math.abs(b.value - (base.buttons[i] ?? 0));
    if (d > bestDelta) { bestDelta = d; best = { src: "button", i }; }
  });
  p.axes.forEach((v, i) => {
    const rest = base.axes[i] ?? 0;
    const d = Math.abs((v || 0) - rest);
    if (d > bestDelta) {
      bestDelta = d;
      // keeps the resting (rest) and pressed (cur) values to calibrate the trigger:
      // a PS2/USB controller may rest the axis at 0 (not -1), and assuming -1
      // leaves the trigger 50% pressed while idle -> double click.
      best = { src: "axis", i, rest, cur: v || 0 };
    }
  });
  return best;
}

function applyDetection(step, found) {
  const d = remapState.draft;
  if (step.kind === "button") {
    if (found.src === "button") d.buttons[step.name] = found.i;
    else d.axes[step.name + "_axisbtn"] = found.i; // rare: a button reported as an axis
  } else if (step.kind === "axis") {
    if (found.src === "axis") d.axes[step.name] = found.i;
  } else if (step.kind === "trigger") {
    // calibrates min=rest, max=pressed (measured during the remap), no -1..1 guess.
    d.triggers[step.name] = found.src === "axis"
      ? { src: "axis", i: found.i, min: found.rest, max: found.cur }
      : { src: "button", i: found.i };
  }
}

let remapCooldown = 0;
function remapTick() {
  if (!remapState.active) return;
  const p = curPad();
  if (p && remapState.step < REMAP_STEPS.length && remapCooldown <= 0) {
    const found = detectChange(p);
    if (found) {
      applyDetection(REMAP_STEPS[remapState.step], found);
      remapState.step++;
      remapCooldown = 18; // ~0.3s pause between steps (avoids skipping by itself)
      // re-snapshot the resting state for the next step
      remapState.base = { buttons: p.buttons.map((b) => b.value), axes: p.axes.slice() };
      if (remapState.step >= REMAP_STEPS.length) return finishRemap();
      renderRemap();
    }
  }
  if (remapCooldown > 0) remapCooldown--;
  requestAnimationFrame(remapTick);
}

async function finishRemap() {
  remapState.active = false;
  $("remap").classList.add("hidden");
  padMaps[remapState.padId] = remapState.draft;
  try {
    await fetch("/api/gamepad-map", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: remapState.padId, map: remapState.draft }),
    });
    setStatus(t("controller remapped and saved on the server"));
  } catch (e) {
    setStatus(t("remapped (but saving to the server failed)"));
  }
}

// Deletes the current controller's remap profile (on the server and in memory).
// Useful if a bad profile was saved for a controller that is actually standard.
async function clearRemap() {
  const p = curPad();
  if (!p) {
    setStatus(t("connect a controller to reset the mapping"));
    return;
  }
  delete padMaps[p.id];
  try {
    await fetch("/api/gamepad-map", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: p.id }),
    });
    setStatus(t("controller mapping reset (passes straight through)"));
  } catch (e) {
    setStatus(t("reset locally (server failed)"));
  }
}

function skipRemapStep() {
  if (!remapState.active) return;
  remapState.step++;
  if (remapState.step >= REMAP_STEPS.length) return finishRemap();
  renderRemap();
}

// ---------- UI ----------
function setStatus(s) {
  $("status").textContent = s;
}

$("connect").addEventListener("click", connect);
$("lock").addEventListener("click", () => canvas.requestPointerLock());
$("full").addEventListener("click", () => {
  if (!document.fullscreenElement) document.documentElement.requestFullscreen();
  else document.exitFullscreen();
});
$("kbd").addEventListener("click", () => toggleOSK());
$("menuToggle").addEventListener("click", () => showBar(barHidden()));
// bar hidden by the gear: the corner where it was becomes an invisible
// "button" (#menuRecall, shown by CSS only while the bar is hidden). Outside it
// clicks go to the remote PC as usual.
$("menuRecall").addEventListener("click", () => showBar(true));
$("sound").addEventListener("click", () => setAudio(!audioOn));
$("remapBtn").addEventListener("click", () => startRemap());
$("remapClear").addEventListener("click", () => clearRemap());
$("remapSkip").addEventListener("click", () => skipRemapStep());
$("remapCancel").addEventListener("click", () => cancelRemap());

// ---------- UI recall: moving the mouse to the top reveals the bar ----------
// No reserved key (doesn't clash with Win/Ubuntu/phone). In fullscreen or with
// the bar hidden, touching the top with the cursor (≤6% of the height) brings it back.
function barHidden() {
  return $("bar").classList.contains("hidden");
}
function showBar(on) {
  $("bar").classList.toggle("hidden", !on);
}
// It only OPENS: once open, the bar stays until the gear (⚙) is clicked again -
// there is no auto-hide (hiding by itself got in the way of using the selectors).
window.addEventListener("mousemove", (e) => {
  if (e.clientY <= Math.max(40, window.innerHeight * 0.06)) showBar(true);
});
// on mobile: a top-down swipe (or a touch on the top strip) reveals the bar
window.addEventListener(
  "touchstart",
  (e) => {
    const t = e.touches[0];
    if (t && t.clientY <= Math.max(40, window.innerHeight * 0.06)) showBar(true);
  },
  { passive: true },
);

// ---------- combos (chords) ----------
document.querySelectorAll("[data-combo]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const combo = COMBOS[btn.dataset.combo];
    if (combo) {
      sendChord(combo);
      setStatus(t("sent: {combo}", { combo: btn.textContent.trim() }));
    }
  });
});

// preset = quick shortcut (applies resolution+fps+bitrate at once; audio
// follows unless you picked audio by hand). Then you fine-tune each parameter
// in the separate selectors below.
$("preset").addEventListener("change", (e) => {
  sendInput({ t: "q", preset: e.target.value });
  setStatus(t("preset: {value}", { value: e.target.value }));
  // after applying the preset, reload to reflect the new res/fps/bitrate
  setTimeout(loadConfig, 200);
});
// INDEPENDENT video controls
$("vres").addEventListener("change", (e) => {
  sendInput({ t: "vq", res: Number(e.target.value) });
  setStatus(t("resolution: {value}p", { value: e.target.value }));
});
$("vfps").addEventListener("change", (e) => {
  sendInput({ t: "vq", fps: Number(e.target.value) });
  setStatus(t("fps: {value}", { value: e.target.value }));
});
$("vbitrate").addEventListener("change", (e) => {
  sendInput({ t: "vq", bitrate: e.target.value });
  setStatus(t("bitrate: {value}", { value: e.target.value }));
});
// INDEPENDENT audio
$("audioPreset").addEventListener("change", (e) => {
  sendInput({ t: "aq", preset: e.target.value });
  setStatus(t("audio: {value}", { value: e.target.value }));
});
$("monitor").addEventListener("change", (e) => {
  const v = e.target.value;
  sendInput({ t: "mon", i: v === "" ? null : Number(v) });
});

// ---------- virtual keyboard (OSK) ----------
// Clickable full PC panel: every key sends its KeyboardEvent.code directly.
// Modifiers (Shift/Ctrl/Alt/Meta) are "sticky": click -> held until the next
// normal key (or click again to lock). Works on mobile and complements the
// physical keyboard (which still works with focus on the canvas).
const OSK_ROWS = [
  ["Escape:Esc", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F12"],
  ["Backquote:`", "Digit1:1", "Digit2:2", "Digit3:3", "Digit4:4", "Digit5:5",
   "Digit6:6", "Digit7:7", "Digit8:8", "Digit9:9", "Digit0:0", "Minus:-",
   "Equal:=", "Backspace:⌫"],
  ["Tab:↹", "KeyQ:q", "KeyW:w", "KeyE:e", "KeyR:r", "KeyT:t", "KeyY:y",
   "KeyU:u", "KeyI:i", "KeyO:o", "KeyP:p", "BracketLeft:[", "BracketRight:]",
   "Backslash:\\"],
  ["CapsLock:⇪", "KeyA:a", "KeyS:s", "KeyD:d", "KeyF:f", "KeyG:g", "KeyH:h",
   "KeyJ:j", "KeyK:k", "KeyL:l", "Semicolon:;", "Quote:'", "Enter:⏎"],
  ["ShiftLeft:⇧", "KeyZ:z", "KeyX:x", "KeyC:c", "KeyV:v", "KeyB:b", "KeyN:n",
   "KeyM:m", "Comma:,", "Period:.", "Slash:/", "ArrowUp:↑", "ShiftRight:⇧"],
  ["ControlLeft:Ctrl", "MetaLeft:Super", "AltLeft:Alt", "Space:␣",
   "AltRight:AltGr", "ArrowLeft:←", "ArrowDown:↓", "ArrowRight:→"],
];
const STICKY = new Set([
  "ShiftLeft", "ShiftRight", "ControlLeft", "AltLeft", "AltRight",
  "MetaLeft", "CapsLock",
]);
const stickyHeld = new Set();

function buildOSK() {
  const k = $("osk");
  if (k.childElementCount) return; // already built
  // the OSK covers the bar (and the ⌨ button), so it needs its own close button
  const head = document.createElement("div");
  head.className = "osk-head";
  const close = document.createElement("button");
  close.className = "osk-close";
  close.textContent = t("✕ close keyboard");
  close.addEventListener("click", (ev) => {
    ev.preventDefault();
    toggleOSK(false);
  });
  head.appendChild(close);
  k.appendChild(head);
  for (const row of OSK_ROWS) {
    const r = document.createElement("div");
    r.className = "osk-row";
    for (const spec of row) {
      const [code, label] = spec.includes(":") ? spec.split(":") : [spec, spec];
      const b = document.createElement("button");
      b.className = "osk-key";
      b.textContent = label;
      b.dataset.code = code;
      if (STICKY.has(code)) b.classList.add("sticky");
      if (code === "Space") b.classList.add("wide");
      b.addEventListener("click", (ev) => {
        ev.preventDefault();
        oskPress(code, b);
      });
      r.appendChild(b);
    }
    k.appendChild(r);
  }
}

function oskPress(code, btn) {
  if (STICKY.has(code)) {
    if (stickyHeld.has(code)) {
      stickyHeld.delete(code);
      btn.classList.remove("held");
      sendInput({ t: "k", code, down: false });
    } else {
      stickyHeld.add(code);
      btn.classList.add("held");
      sendInput({ t: "k", code, down: true });
    }
    return;
  }
  // normal key: press, release; then release the sticky modifiers
  sendInput({ t: "k", code, down: true });
  sendInput({ t: "k", code, down: false });
  for (const m of stickyHeld) {
    sendInput({ t: "k", code: m, down: false });
    const mb = $("osk").querySelector(`[data-code="${m}"]`);
    if (mb) mb.classList.remove("held");
  }
  stickyHeld.clear();
}

function toggleOSK(force) {
  buildOSK();
  const k = $("osk");
  const show = force === undefined ? k.classList.contains("hidden") : force;
  k.classList.toggle("hidden", !show);
  $("kbd").classList.toggle("on", show);
}

// fills a <select> from a [{value,label}] list, selecting the current one.
function fillSelect(id, items, current) {
  const sel = $(id);
  sel.innerHTML = "";
  for (const it of items) {
    const o = document.createElement("option");
    o.value = String(it.value);
    o.textContent = it.label;
    if (String(it.value) === String(current)) o.selected = true;
    sel.appendChild(o);
  }
}

async function loadConfig() {
  try {
    const cfg = await (await fetch("/api/monitors")).json();
    fillSelect("preset",
      cfg.presets.map((p) => ({ value: p, label: p })), cfg.current_preset);

    // independent video controls
    const v = cfg.video || {};
    fillSelect("vres",
      (cfg.res_options || []).map((r) => ({ value: r, label: `${r}p` })), v.res);
    fillSelect("vfps",
      (cfg.fps_options || []).map((f) => ({ value: f, label: `${f} fps` })), v.fps);
    fillSelect("vbitrate",
      (cfg.bitrate_options || []).map((b) => ({ value: b, label: b })), v.bitrate);

    // audio: has its own selector (independent quality)
    fillSelect("audioPreset",
      (cfg.audio_presets || []).map((a) => ({ value: a, label: a })),
      cfg.current_audio_preset);

    fillSelect("monitor",
      cfg.monitors.map((m) => ({
        value: m.index,
        label: `${m.name}${m.primary ? " ★" : ""} (${m.w}×${m.h})`,
      })), cfg.current_monitor);

    const sbtn = $("sound");
    const audioPresetSel = $("audioPreset");
    if (cfg.audio_available && "AudioDecoder" in window) {
      sbtn.disabled = false;
      audioPresetSel.disabled = false;
    } else {
      sbtn.disabled = true;
      audioPresetSel.disabled = true;
      sbtn.title = cfg.audio_available
        ? t("browser without AudioDecoder")
        : t("no audio on the server");
    }
  } catch (e) {
    console.warn("config", e);
  }
}

// ---------- stats ----------
function statsLoop() {
  const now = performance.now();
  const dt = (now - lastStat) / 1000;
  if (dt >= 1) {
    const fps = (frames / dt).toFixed(0);
    const mbps = ((bytes * 8) / dt / 1e6).toFixed(1);
    let s = `${fps} fps · ${mbps} Mbps · ~${decodeLatency.toFixed(0)} ms`;
    if (audioOn) {
      const akbps = ((audioBytes * 8) / dt / 1e3).toFixed(0);
      s += ` · 🔊 ${akbps} kbps`;
    }
    $("stats").textContent = s;
    frames = 0;
    bytes = 0;
    audioBytes = 0;
    audioPkts = 0;
    lastStat = now;
  }
  requestAnimationFrame(statsLoop);
}

// ---------- boot ----------
function boot() {
  I18N.translatePage(document, LANG);
  if (!("VideoDecoder" in window)) {
    setStatus(t("No WebCodecs in this browser. Use Chrome/Firefox/Edge over HTTPS."));
    $("connect").disabled = true;
    return;
  }
  buildOSK();
  loadConfig();
  loadGamepadMaps();
  setStatus(t("ready - tap Connect"));
  statsLoop();
  pollPads();
  requestAnimationFrame(flushMove);
}
// ---------- boot / test seam ----------
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    hex2, findNALs, codecFromSPS, configureDecoder,
    ensureDecoder, resetDecoder, onAU,
    startAudioContext, ensureAudioDecoder, resetAudioDecoder,
    playAudio, onAudioPacket, connectAudio, setAudio,
    connect, connectVideo, connectInput, sendInput,
    flushMove, normFromEvent, displayedRect, pollPads,
    setStatus, loadConfig, statsLoop, boot,
    sendChord, COMBOS, toggleOSK, oskPress, buildOSK,
    setKbdCapture, showBar, barHidden,
    loadGamepadMaps, mapPad, readSource, startRemap, cancelRemap,
    skipRemapStep, detectChange, applyDetection, finishRemap, remapTick,
    clearRemap, passthroughPad,
    renderRemap, t, LANG,
    __get: (k) => eval(k),
  };
}
/* v8 ignore start -- only runs in the real browser (no CommonJS); covered by the Playwright e2e */
else {
  boot();
}
/* v8 ignore stop */
