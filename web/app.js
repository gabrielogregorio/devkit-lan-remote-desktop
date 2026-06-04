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
  configuring = false,
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
  configuring = false;
  codecStr = null;
  try {
    if (decoder && decoder.state !== "closed") decoder.close();
  } catch (e) {}
  decoder = null;
}

// Chrome no Linux costuma falhar com hardwareAcceleration:"prefer-hardware"
// (caminho VA-API/NVDEC instável) onde Firefox aceita. Então sondamos com
// isConfigSupported() e caímos pra "no-preference" -> "prefer-software". A
// configuração roda async; bufferizamos o 1º keyframe até o decoder ficar pronto.
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
      console.warn("configure falhou em", extra.hardwareAcceleration, e);
      resetDecoder();
    }
  }
  setStatus("este navegador não conseguiu iniciar o decoder H.264");
  return false;
}

function onAU(buf) {
  const au = new Uint8Array(buf);
  bytes += au.length;
  const nals = findNALs(au);
  const isKey = nals.some((n) => n.type === 5 || n.type === 7);

  if (!configured) {
    if (configuring) return; // sondagem async em andamento; espera o próximo keyframe
    const sps = nals.find((n) => n.type === 7);
    if (!sps) return;
    codecStr = codecFromSPS(au, sps.hdr);
    configuring = true;
    configureDecoder(codecStr).then((ok) => {
      configuring = false;
      configured = ok;
    });
    return; // este AU serve só pra configurar; o próximo keyframe começa o decode
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
// Captura SÓ com foco no canvas: clicar na tela remota manda o teclado físico
// pro PC; clicar fora (ou Esc-do-foco) devolve o controle ao navegador local.
// Assim Tab/F-keys/atalhos não vazam pro browser enquanto você está "dentro".
let kbdCapture = false;
canvas.tabIndex = 0; // permite foco/blur no canvas

function setKbdCapture(on) {
  kbdCapture = on;
  document.body.classList.toggle("kbd-capture", on);
  if (on) setStatus("teclado capturado - clique fora p/ liberar");
}
canvas.addEventListener("focus", () => setKbdCapture(true));
canvas.addEventListener("blur", () => setKbdCapture(false));
canvas.addEventListener("mousedown", () => canvas.focus());

window.addEventListener(
  "keydown",
  (e) => {
    if (!kbdCapture || !inputWS || inputWS.readyState !== 1) return;
    // F11 fica reservado ao fullscreen do navegador local; nunca enviado.
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

// ---------- combos / teclado virtual ----------
// Envia uma sequência de teclas como um chord: aperta todas em ordem, solta na
// ordem inversa. Não depende de atalho local (não colide com Win/Ubuntu/celular).
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
    // Gatilhos (LT/RT): no mapping "standard" são buttons[6]/[7] (analógicos).
    // Mas em mappings não-padrão (Firefox/Linux com alguns drivers) eles chegam
    // como EIXOS extras. Detectamos e mandamos explicitamente em `lt`/`rt` 0..1
    // pro servidor não depender do índice de botão.
    let lt = b[6] ? b[6].v : 0;
    let rt = b[7] ? b[7].v : 0;
    if (p.mapping !== "standard") {
      // heurística: eixos além dos 4 sticks costumam ser os gatilhos (−1 solto..+1 fundo)
      const ax = p.axes;
      if (ax.length >= 6) {
        lt = Math.max(lt, (ax[4] + 1) / 2 || 0);
        rt = Math.max(rt, (ax[5] + 1) / 2 || 0);
      }
    }
    lt = +lt.toFixed(3);
    rt = +rt.toFixed(3);
    const snap = JSON.stringify([a, b, lt, rt]);
    if (snap !== padSnap) {
      padSnap = snap;
      sendInput({ t: "g", a, b, lt, rt });
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
$("kbd").addEventListener("click", () => toggleOSK());
$("menuToggle").addEventListener("click", () => showBar(barHidden()));
$("sound").addEventListener("click", () => setAudio(!audioOn));

// ---------- recall da UI: mover o mouse pro topo revela a barra ----------
// Sem reservar tecla (não colide com Win/Ubuntu/celular). Em fullscreen ou com
// a barra escondida, encostar o cursor no topo (≤6% da altura) traz tudo de volta.
function barHidden() {
  return $("bar").classList.contains("hidden");
}
function showBar(on) {
  $("bar").classList.toggle("hidden", !on);
}
let barAutoHide = null;
window.addEventListener("mousemove", (e) => {
  if (e.clientY <= Math.max(40, window.innerHeight * 0.06)) {
    showBar(true);
    clearTimeout(barAutoHide);
    // some sozinha depois de 2.5s se o mouse sair do topo
    barAutoHide = setTimeout(() => {
      if (document.pointerLockElement !== canvas) showBar(false);
    }, 2500);
  }
});
// no mobile: swipe de cima pra baixo (ou toque na faixa superior) revela a barra
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
      setStatus(`enviado: ${btn.textContent.trim()}`);
    }
  });
});

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

// ---------- teclado virtual (OSK) ----------
// Painel clicável de PC completo: cada tecla manda KeyboardEvent.code direto.
// Modificadores (Shift/Ctrl/Alt/Meta) são "sticky": clica -> fica preso até a
// próxima tecla normal (ou clica de novo pra travar). Funciona em mobile e
// complementa o teclado físico (que continua valendo com foco no canvas).
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
  if (k.childElementCount) return; // já montado
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
  // tecla normal: aperta, solta; depois libera os modificadores sticky
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
    setStatus("Sem WebCodecs neste navegador. Use Chrome/Firefox/Edge via HTTPS.");
    $("connect").disabled = true;
    return;
  }
  buildOSK();
  loadConfig();
  setStatus("pronto - toque em Conectar");
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
    __get: (k) => eval(k),
  };
}
/* v8 ignore start -- so roda no browser real (sem CommonJS); coberto no e2e Playwright */
else {
  boot();
}
/* v8 ignore stop */
