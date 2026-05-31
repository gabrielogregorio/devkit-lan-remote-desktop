/* remote-link client
   vídeo: WebSocket binário -> Access Units H.264 -> WebCodecs VideoDecoder -> canvas
   áudio: WebSocket binário -> pacotes Opus -> WebCodecs AudioDecoder -> AudioContext
   input: mouse/teclado/gamepad -> WebSocket texto JSON
*/
"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("screen");
const ctx = canvas.getContext("2d", { alpha: false, desynchronized: true });

const wsBase = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;

let videoWS = null,
  inputWS = null;
let decoder = null,
  configured = false,
  codecStr = null;
let tsCounter = 0;
let frames = 0,
  bytes = 0,
  lastStat = performance.now();
let lastFrameAt = 0,
  decodeLatency = 0;

// ---------- WebCodecs (vídeo) ----------
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
  codecStr = null;
  try {
    if (decoder && decoder.state !== "closed") decoder.close();
  } catch (e) {}
  decoder = null;
}

function onAU(buf) {
  const au = new Uint8Array(buf);
  bytes += au.length;
  const nals = findNALs(au);
  const isKey = nals.some((n) => n.type === 5 || n.type === 7);

  if (!configured) {
    const sps = nals.find((n) => n.type === 7);
    if (!sps) return;
    codecStr = codecFromSPS(au, sps.hdr);
    ensureDecoder();
    decoder.configure({
      codec: codecStr,
      optimizeForLatency: true,
      hardwareAcceleration: "prefer-hardware",
    });
    configured = true;
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
    console.warn("decode falhou, ressync", e);
    resetDecoder();
  }
}

// ---------- WebCodecs (áudio) ----------
const AUDIO_SR = 48000,
  AUDIO_CH = 2;
let audioWS = null,
  audioCtx = null,
  audioDecoder = null;
let audioOn = false; // usuário quer som?
let playHead = 0; // relógio de agendamento (s)
let audioTs = 0;
let audioBytes = 0,
  audioPkts = 0;
const JITTER = 0.06; // buffer alvo (s) - equilíbrio latência × estabilidade

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
  // reancora se atrasou (playHead no passado) OU se o buffer agendado cresceu
  // demais (rajada acumulou latencia) -> mantém tempo real, sem deixar empilhar.
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
        type: "key", // cada pacote Opus é decodificável de forma independente
        timestamp: audioTs,
        data: arrbuf,
      }),
    );
    audioTs += 20000; // 20ms por frame (µs)
  } catch (e) {
    console.warn("audio decode falhou", e);
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
    btn.textContent = "🔊 som";
    btn.classList.add("on");
    startAudioContext();
    if (!audioWS) connectAudio();
  } else {
    btn.textContent = "🔇 som";
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

// ---------- conexão ----------
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
    setStatus("vídeo desconectado - reconectando…");
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

// Retângulo REAL onde o vídeo é desenhado dentro do <canvas>. Como o canvas usa
// object-fit:contain pra ocupar a tela toda, pode sobrar barra preta (ex.: a
// fonte é 16:9 e o celular está em pé). O mouse tem que mapear pra esse rect, não
// pro elemento inteiro, senão clicar/mover erra na proporção da barra.
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

// ---------- toque (mobile): tap = clique, arraste = mover ----------
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

// ---------- teclado ----------
window.addEventListener("keydown", (e) => {
  if (!inputWS || inputWS.readyState !== 1) return;
  if (e.code === "F11") return;
  e.preventDefault();
  sendInput({ t: "k", code: e.code, down: true });
});
window.addEventListener("keyup", (e) => {
  if (!inputWS || inputWS.readyState !== 1) return;
  if (e.code === "F11") return;
  e.preventDefault();
  sendInput({ t: "k", code: e.code, down: false });
});

// ---------- gamepad ----------
let padSnap = "";
function pollPads() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  let p = null;
  for (const g of pads)
    if (g) {
      p = g;
      break;
    }
  if (p) {
    const a = [p.axes[0] || 0, p.axes[1] || 0, p.axes[2] || 0, p.axes[3] || 0];
    const b = p.buttons.map((x) => ({ p: x.pressed, v: +x.value.toFixed(3) }));
    const snap = JSON.stringify([a, b]);
    if (snap !== padSnap) {
      padSnap = snap;
      sendInput({ t: "g", a, b });
    }
  }
  requestAnimationFrame(pollPads);
}
window.addEventListener("gamepadconnected", (e) => {
  setStatus(`controle: ${e.gamepad.id.slice(0, 28)}`);
});

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
$("kbd").addEventListener("click", () => $("hiddenKbd").focus());
$("menuToggle").addEventListener("click", () =>
  $("bar").classList.toggle("hidden"),
);
$("sound").addEventListener("click", () => setAudio(!audioOn));

// qualidade é um ESTADO ÚNICO: o servidor arrasta o áudio junto (ver
// main.AUDIO_FOR_QUALITY). Feedback na tela pra a troca ser perceptível.
$("preset").addEventListener("change", (e) => {
  sendInput({ t: "q", preset: e.target.value });
  setStatus(`qualidade: ${e.target.value}`);
});
$("monitor").addEventListener("change", (e) => {
  const v = e.target.value;
  sendInput({ t: "mon", i: v === "" ? null : Number(v) });
});

$("hiddenKbd").addEventListener("input", (e) => {
  const ch = e.data;
  if (ch) {
    const code = charToCode(ch);
    if (code) {
      sendInput({ t: "k", code, down: true });
      sendInput({ t: "k", code, down: false });
    }
  }
  e.target.value = "";
});
function charToCode(ch) {
  if (/[a-zA-Z]/.test(ch)) return "Key" + ch.toUpperCase();
  if (/[0-9]/.test(ch)) return "Digit" + ch;
  if (ch === " ") return "Space";
  return null;
}

async function loadConfig() {
  try {
    const cfg = await (await fetch("/api/monitors")).json();
    const ps = $("preset");
    ps.innerHTML = "";
    for (const p of cfg.presets) {
      const o = document.createElement("option");
      o.value = p;
      o.textContent = p;
      if (p === cfg.current_preset) o.selected = true;
      ps.appendChild(o);
    }
    const ms = $("monitor");
    ms.innerHTML = "";
    for (const m of cfg.monitors) {
      const o = document.createElement("option");
      o.value = m.index;
      o.textContent = `${m.name}${m.primary ? " ★" : ""} (${m.w}×${m.h})`;
      if (m.index === cfg.current_monitor) o.selected = true;
      ms.appendChild(o);
    }
    // áudio: sem seletor próprio - segue o nível de qualidade. Só liga/desliga.
    const sbtn = $("sound");
    if (cfg.audio_available && "AudioDecoder" in window) {
      sbtn.disabled = false;
    } else {
      sbtn.disabled = true;
      sbtn.title = cfg.audio_available
        ? "navegador sem AudioDecoder"
        : "sem áudio no servidor";
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
  if (!("VideoDecoder" in window)) {
    setStatus("Sem WebCodecs neste navegador. Use Chrome/Edge via HTTPS.");
    $("connect").disabled = true;
    return;
  }
  loadConfig();
  setStatus("pronto - toque em Conectar");
  statsLoop();
  pollPads();
  requestAnimationFrame(flushMove);
}
// ---------- boot / test seam ----------
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    hex2, findNALs, codecFromSPS,
    ensureDecoder, resetDecoder, onAU,
    startAudioContext, ensureAudioDecoder, resetAudioDecoder,
    playAudio, onAudioPacket, connectAudio, setAudio,
    connect, connectVideo, connectInput, sendInput,
    flushMove, normFromEvent, displayedRect, pollPads,
    setStatus, charToCode, loadConfig, statsLoop, boot,
    __get: (k) => eval(k),
  };
} else {
  boot();
}
