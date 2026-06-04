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
  needKey = true, // 1º chunk após configure()/reset TEM que ser keyframe
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
  needKey = true;
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
      needKey = true; // só começa a decodificar no próximo keyframe
    });
    return; // este AU serve só pra configurar; o próximo keyframe começa o decode
  }

  // O VideoDecoder EXIGE que o 1º chunk após configure()/flush seja keyframe.
  // Como a config é assíncrona e o GOP é longo, os AUs que chegam logo depois
  // costumam ser delta -> descarta até cair um keyframe (senão dá erro
  // "key frame is required" no Firefox e no Chrome).
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
// Ordem lógica Xbox que o servidor espera em b[] (índices standard):
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

// Gatilho preciso: deadzone embaixo (mata o jitter em repouso que vira
// "double click") e satura no topo. Quantiza em degraus de 1/255 (resolução
// real do ABS_Z do Xbox) pra não reenviar por ruído infinitesimal.
const TRIG_DZ = 0.06; // abaixo disso = solto (0)
const TRIG_TOP = 0.95; // acima disso = fundo (1)
function shapeTrigger(v) {
  if (!(v > TRIG_DZ)) return 0; // inclui NaN/negativo/repouso
  if (v >= TRIG_TOP) return 1;
  const n = (v - TRIG_DZ) / (TRIG_TOP - TRIG_DZ);
  return Math.round(n * 255) / 255; // degrau de 1/255 (estável)
}

// lê um source de gatilho/eixo do perfil: {src:"axis",i,min,max} ou {src:"button",i}
function readSource(p, s) {
  if (!s) return 0;
  if (s.src === "button") {
    const btn = p.buttons[s.i];
    return btn ? btn.value : 0;
  }
  // eixo: normaliza de [min(repouso)..max(pressionado)] pra 0..1. Suporta eixo
  // invertido (max < min) porque o controle PS2/USB pode ir de 0 a -1.
  const v = p.axes[s.i];
  if (v === undefined || v === null || Number.isNaN(v)) return 0;
  const min = s.min ?? -1, max = s.max ?? 1;
  const span = max - min;
  if (Math.abs(span) < 1e-6) return 0; // calibração degenerada -> trata solto
  return Math.min(1, Math.max(0, (v - min) / span));
}

// Traduz o pad bruto -> formato padrão usando o perfil salvo (se houver).
function mapPad(p) {
  const m = padMaps[p.id];
  if (!m) {
    // sem perfil: heurística padrão (standard + eixos extras p/ gatilhos)
    const a = PAD_AXES.map((_, i) => p.axes[i] || 0);
    const b = p.buttons.map((x) => ({ p: x.pressed, v: +x.value.toFixed(3) }));
    // gatilhos só dos buttons[6]/[7] (mapping standard). NÃO inventamos a
    // partir de eixos sem perfil: controle PS2/USB costuma repousar o eixo em 0
    // (ou compartilhar um eixo LT/RT), e adivinhar gera gatilho fantasma /
    // double click. Pra esses, use o remap (🎮) que captura a fonte certa.
    const lt = shapeTrigger(b[6] ? b[6].v : 0);
    const rt = shapeTrigger(b[7] ? b[7].v : 0);
    return { a, b, lt, rt };
  }
  // com perfil: monta b[] na ordem lógica a partir dos índices mapeados
  const b = PAD_BUTTONS.map((name) => {
    const idx = m.buttons?.[name];
    const btn = idx != null ? p.buttons[idx] : null;
    return { p: btn ? btn.pressed : false, v: btn ? +btn.value.toFixed(3) : 0 };
  });
  const a = PAD_AXES.map((name) => {
    const idx = m.axes?.[name];
    return idx != null ? (p.axes[idx] || 0) : 0;
  });
  const lt = shapeTrigger(readSource(p, m.triggers?.lt));
  const rt = shapeTrigger(readSource(p, m.triggers?.rt));
  return { a, b, lt, rt };
}

let padSnap = "";
function pollPads() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  let p = null;
  for (const g of pads)
    if (g) {
      p = g;
      break;
    }
  if (p && !remapState.active) {
    const { a, b, lt, rt } = mapPad(p);
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

// ---------- remapeamento de gamepad ----------
// Fluxo "aperte o botão X": pra cada controle lógico, captura qual botão/eixo
// bruto do pad foi acionado. Pensado pros controles PS2/USB com mapping caótico.
// Cada passo: nome lógico + se é botão, eixo (stick) ou gatilho.
const REMAP_STEPS = [
  ...PAD_BUTTONS.filter((n) => n !== "lt" && n !== "rt")
    .map((n) => ({ kind: "button", name: n })),
  { kind: "trigger", name: "lt" },
  { kind: "trigger", name: "rt" },
  { kind: "axis", name: "lx" }, { kind: "axis", name: "ly" },
  { kind: "axis", name: "rx" }, { kind: "axis", name: "ry" },
];
const STEP_LABEL = {
  a: "A", b: "B", x: "X", y: "Y", lb: "LB (ombro esq.)", rb: "RB (ombro dir.)",
  back: "Back/Select", start: "Start", ls: "L3 (clicar stick esq.)",
  rs: "R3 (clicar stick dir.)", guide: "Guide/Home (ou pule)",
  dup: "D-Pad ↑", ddown: "D-Pad ↓", dleft: "D-Pad ←", dright: "D-Pad →",
  lt: "LT (gatilho esq.) - aperte até o fundo",
  rt: "RT (gatilho dir.) - aperte até o fundo",
  lx: "Stick ESQ. → direita", ly: "Stick ESQ. → baixo",
  rx: "Stick DIR. → direita", ry: "Stick DIR. → baixo",
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
    setStatus("conecte um controle e mexa nele antes de remapear");
    return;
  }
  remapState.active = true;
  remapState.step = 0;
  remapState.padId = p.id;
  remapState.draft = { buttons: {}, axes: {}, triggers: {} };
  // snapshot do estado de repouso pra detectar o que MUDOU
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
  $("remapTitle").textContent =
    `Remapear (${remapState.step + 1}/${REMAP_STEPS.length})`;
  // STEP_LABEL cobre todos os nomes; o `|| name` é só rede de segurança.
  /* v8 ignore next 2 */
  const label = step ? (STEP_LABEL[step.name] || step.name) : "";
  $("remapPrompt").textContent = step ? `Acione: ${label}` : "";
}

// detecta o controle bruto que mais se moveu desde o repouso
function detectChange(p) {
  const base = remapState.base;
  let best = null, bestDelta = 0.35; // limiar p/ ignorar ruído
  p.buttons.forEach((b, i) => {
    const d = Math.abs(b.value - (base.buttons[i] ?? 0));
    if (d > bestDelta) { bestDelta = d; best = { src: "button", i }; }
  });
  p.axes.forEach((v, i) => {
    const rest = base.axes[i] ?? 0;
    const d = Math.abs((v || 0) - rest);
    if (d > bestDelta) {
      bestDelta = d;
      // guarda repouso (rest) e valor pressionado (cur) p/ calibrar o gatilho:
      // controle PS2/USB pode repousar o eixo em 0 (não em -1), e assumir -1
      // deixa o gatilho 50% apertado parado -> double click.
      best = { src: "axis", i, rest, cur: v || 0 };
    }
  });
  return best;
}

function applyDetection(step, found) {
  const d = remapState.draft;
  if (step.kind === "button") {
    if (found.src === "button") d.buttons[step.name] = found.i;
    else d.axes[step.name + "_axisbtn"] = found.i; // raro: botão como eixo
  } else if (step.kind === "axis") {
    if (found.src === "axis") d.axes[step.name] = found.i;
  } else if (step.kind === "trigger") {
    // calibra min=repouso, max=pressionado (medidos no remap), não chuta -1..1.
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
      remapCooldown = 18; // ~0.3s de respiro entre passos (evita pular sozinho)
      // re-snapshot do repouso pro próximo passo
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
    setStatus("controle remapeado e salvo no servidor");
  } catch (e) {
    setStatus("remapeado (mas falhou ao salvar no servidor)");
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
$("sound").addEventListener("click", () => setAudio(!audioOn));
$("remapBtn").addEventListener("click", () => startRemap());
$("remapSkip").addEventListener("click", () => skipRemapStep());
$("remapCancel").addEventListener("click", () => cancelRemap());

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

// preset = atalho rápido (aplica resolução+fps+bitrate de uma vez; o áudio
// acompanha se você não tiver escolhido áudio na mão). Depois você refina cada
// parâmetro nos seletores separados abaixo.
$("preset").addEventListener("change", (e) => {
  sendInput({ t: "q", preset: e.target.value });
  setStatus(`preset: ${e.target.value}`);
  // após aplicar o preset, recarrega pra refletir os novos res/fps/bitrate
  setTimeout(loadConfig, 200);
});
// controles de vídeo INDEPENDENTES
$("vres").addEventListener("change", (e) => {
  sendInput({ t: "vq", res: Number(e.target.value) });
  setStatus(`resolução: ${e.target.value}p`);
});
$("vfps").addEventListener("change", (e) => {
  sendInput({ t: "vq", fps: Number(e.target.value) });
  setStatus(`fps: ${e.target.value}`);
});
$("vbitrate").addEventListener("change", (e) => {
  sendInput({ t: "vq", bitrate: e.target.value });
  setStatus(`bitrate: ${e.target.value}`);
});
// áudio INDEPENDENTE
$("audioPreset").addEventListener("change", (e) => {
  sendInput({ t: "aq", preset: e.target.value });
  setStatus(`áudio: ${e.target.value}`);
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

// preenche um <select> a partir de uma lista [{value,label}] marcando o atual.
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

    // controles de vídeo independentes
    const v = cfg.video || {};
    fillSelect("vres",
      (cfg.res_options || []).map((r) => ({ value: r, label: `${r}p` })), v.res);
    fillSelect("vfps",
      (cfg.fps_options || []).map((f) => ({ value: f, label: `${f} fps` })), v.fps);
    fillSelect("vbitrate",
      (cfg.bitrate_options || []).map((b) => ({ value: b, label: b })), v.bitrate);

    // áudio: agora tem seletor próprio (qualidade independente)
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
  loadGamepadMaps();
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
    loadGamepadMaps, mapPad, readSource, startRemap, cancelRemap,
    skipRemapStep, detectChange, applyDetection, finishRemap, remapTick,
    renderRemap,
    __get: (k) => eval(k),
  };
}
/* v8 ignore start -- so roda no browser real (sem CommonJS); coberto no e2e Playwright */
else {
  boot();
}
/* v8 ignore stop */
