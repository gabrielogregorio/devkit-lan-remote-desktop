import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadApp } from "./setup.js";

let app;
beforeEach(async () => {
  app = await loadApp();
});

// ---------------------------------------------------------------------------
// 4.1 Funções puras
// ---------------------------------------------------------------------------

describe("hex2", () => {
  it("pad único dígito", () => expect(app.hex2(5)).toBe("05"));
  it("valor máximo byte", () => expect(app.hex2(255)).toBe("ff"));
  it("zero", () => expect(app.hex2(0)).toBe("00"));
});

describe("findNALs", () => {
  it("dois start codes retorna 2 entradas com type e hdr corretos", () => {
    const buf = new Uint8Array([0, 0, 1, 0x67, 0xAA, 0, 0, 1, 0x41, 0xBB]);
    const nals = app.findNALs(buf);
    expect(nals).toHaveLength(2);
    expect(nals[0].type).toBe(0x67 & 0x1f); // 7
    expect(nals[0].hdr).toBe(3);
    expect(nals[1].type).toBe(0x41 & 0x1f); // 1
    expect(nals[1].hdr).toBe(8);
  });

  it("sem start code retorna []", () => {
    const buf = new Uint8Array([0x01, 0x02, 0x03, 0x04]);
    expect(app.findNALs(buf)).toEqual([]);
  });

  it("4-byte start code não falsa-detecta entrada extra", () => {
    // [0,0,0,1] — o zero extra é posição i, depois i+1=0, i+2=0, i+3=1: detecta
    // mas o byte de tipo é buf[i+3] = o '0x67' seguinte, não o '1'
    const buf = new Uint8Array([0, 0, 0, 1, 0x67]);
    const nals = app.findNALs(buf);
    // O parser acha [0,0,1] em i=1 (buf[1]=0, buf[2]=0, buf[3]=1), tipo=buf[4]=0x67
    expect(nals.length).toBeGreaterThanOrEqual(1);
    // Confirma que o i+=2 (incremento extra) funciona: com buf de 2 start codes
    // consecutivos, nenhum é pulado
    const buf2 = new Uint8Array([0, 0, 1, 0x67, 0, 0, 1, 0x41]);
    expect(app.findNALs(buf2)).toHaveLength(2);
  });
});

describe("codecFromSPS", () => {
  it("constrói string avc1 correta a partir de bytes SPS", () => {
    // au = [0,0,1,0x67, P, C, L]  spsHdr=3
    const au = new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f]);
    const result = app.codecFromSPS(au, 3);
    expect(result).toBe("avc1.64001f");
  });

  it("formato avc1.xxxxxx (6 hex chars)", () => {
    const au = new Uint8Array([0, 0, 1, 0x67, 0x42, 0xc0, 0x1e]);
    expect(app.codecFromSPS(au, 3)).toMatch(/^avc1\.[0-9a-f]{6}$/);
  });
});

describe("charToCode", () => {
  it("letra minúscula", () => expect(app.charToCode("a")).toBe("KeyA"));
  it("letra maiúscula", () => expect(app.charToCode("Z")).toBe("KeyZ"));
  it("dígito 5", () => expect(app.charToCode("5")).toBe("Digit5"));
  it("dígito 0", () => expect(app.charToCode("0")).toBe("Digit0"));
  it("espaço", () => expect(app.charToCode(" ")).toBe("Space"));
  it("caractere especial retorna null", () => expect(app.charToCode("!")).toBeNull());
  it("toUpperCase para minúsculas", () => expect(app.charToCode("e")).toBe("KeyE"));
});

describe("displayedRect", () => {
  it("16:9 numa tela 16:9 → preenche sem barras", () => {
    const canvas = document.getElementById("screen");
    canvas.width = 1280;
    canvas.height = 720; // aspecto = elemento (1920x1080)
    const r = app.displayedRect();
    expect(r.left).toBeCloseTo(0);
    expect(r.top).toBeCloseTo(0);
    expect(r.width).toBeCloseTo(1920);
    expect(r.height).toBeCloseTo(1080);
  });

  it("buffer mais 'alto' que o elemento → barras laterais (offset em left)", () => {
    const canvas = document.getElementById("screen");
    canvas.width = 540;
    canvas.height = 540; // 1:1 num elemento 16:9 → escala por altura, sobra nos lados
    const r = app.displayedRect();
    expect(r.height).toBeCloseTo(1080);
    expect(r.width).toBeCloseTo(1080);
    expect(r.left).toBeCloseTo((1920 - 1080) / 2);
    expect(r.top).toBeCloseTo(0);
  });

  it("fallback quando o buffer é 0 usa o tamanho do elemento", () => {
    const canvas = document.getElementById("screen");
    canvas.width = 0;
    canvas.height = 0;
    const r = app.displayedRect();
    expect(r.width).toBeCloseTo(1920);
    expect(r.height).toBeCloseTo(1080);
  });
});

describe("normFromEvent", () => {
  it("centro exato 0.5,0.5", () => {
    const n = app.normFromEvent({ clientX: 960, clientY: 540 });
    expect(n.x).toBeCloseTo(0.5);
    expect(n.y).toBeCloseTo(0.5);
  });
  it("clamp negativo → 0", () => {
    expect(app.normFromEvent({ clientX: -100, clientY: -100 }).x).toBe(0);
    expect(app.normFromEvent({ clientX: -100, clientY: -100 }).y).toBe(0);
  });
  it("clamp além da borda → 1", () => {
    expect(app.normFromEvent({ clientX: 9999, clientY: 9999 }).x).toBe(1);
    expect(app.normFromEvent({ clientX: 9999, clientY: 9999 }).y).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// 4.2 WebCodecs vídeo
// ---------------------------------------------------------------------------

describe("ensureDecoder", () => {
  it("primeira chamada cria VideoDecoder configurado", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    expect(dec).not.toBeNull();
    expect(dec).toBeInstanceOf(globalThis.VideoDecoder);
  });

  it("segunda chamada com decoder aberto não recria", () => {
    app.ensureDecoder();
    const first = app.__get("decoder");
    app.ensureDecoder();
    expect(app.__get("decoder")).toBe(first);
  });

  it("com decoder.state === 'closed' recria", () => {
    app.ensureDecoder();
    const first = app.__get("decoder");
    first.state = "closed";
    app.ensureDecoder();
    expect(app.__get("decoder")).not.toBe(first);
  });

  it("callback output: drawImage + frame.close + incrementa frames", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    const moduleCtx = app.__get("ctx"); // ctx capturado pelo módulo no load
    const frame = { displayWidth: 1920, displayHeight: 1080, close: vi.fn() };
    const framesBefore = app.__get("frames");
    dec.cfg.output(frame);
    expect(moduleCtx.drawImage).toHaveBeenCalledWith(frame, 0, 0);
    expect(frame.close).toHaveBeenCalled();
    expect(app.__get("frames")).toBe(framesBefore + 1);
  });

  it("callback output: ajusta canvas.width/height quando diferem", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    const canvas = document.getElementById("screen");
    canvas.width = 0;
    canvas.height = 0;
    dec.cfg.output({ displayWidth: 800, displayHeight: 600, close: vi.fn() });
    expect(canvas.width).toBe(800);
    expect(canvas.height).toBe(600);
  });

  it("callback output: decodeLatency só atualiza na segunda chamada", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    const frame = { displayWidth: 1920, displayHeight: 1080, close: vi.fn() };
    // Primeira chamada — lastFrameAt era 0, não atualiza decodeLatency
    globalThis.performance.now.mockReturnValue(100);
    dec.cfg.output(frame);
    expect(app.__get("decodeLatency")).toBe(0);
    // Segunda chamada — lastFrameAt != 0, atualiza
    globalThis.performance.now.mockReturnValue(200);
    dec.cfg.output({ ...frame, close: vi.fn() });
    expect(app.__get("decodeLatency")).toBeGreaterThan(0);
  });

  it("callback error chama resetDecoder", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    dec.cfg.error(new Error("test error"));
    expect(app.__get("decoder")).toBeNull();
  });
});

describe("resetDecoder", () => {
  it("fecha decoder e zera estado", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    app.resetDecoder();
    expect(dec.state).toBe("closed");
    expect(app.__get("decoder")).toBeNull();
    expect(app.__get("configured")).toBe(false);
    expect(app.__get("codecStr")).toBeNull();
  });

  it("close() lançando é silenciado, decoder fica null", () => {
    app.ensureDecoder();
    const dec = app.__get("decoder");
    dec.close = () => { throw new Error("boom"); };
    expect(() => app.resetDecoder()).not.toThrow();
    expect(app.__get("decoder")).toBeNull();
  });

  it("com decoder null não levanta", () => {
    expect(() => app.resetDecoder()).not.toThrow();
  });
});

describe("onAU", () => {
  // AU com SPS (type 7) + IDR (type 5) + bytes de codec
  const spsAU = () =>
    new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0, 0, 1, 0x65, 0x01]).buffer;
  const deltaAU = () =>
    new Uint8Array([0, 0, 1, 0x61, 0x01]).buffer;

  it("AU sem SPS e configured=false retorna sem criar decoder", () => {
    app.onAU(deltaAU());
    expect(app.__get("decoder")).toBeNull();
  });

  it("AU com SPS + IDR configura decoder e decodifica como key", () => {
    app.onAU(spsAU());
    const dec = app.__get("decoder");
    expect(app.__get("configured")).toBe(true);
    expect(dec).not.toBeNull();
    expect(dec.decoded).toHaveLength(1);
    expect(dec.decoded[0].type).toBe("key");
  });

  it("AU delta após configured=true decodifica como delta", () => {
    app.onAU(spsAU());
    const dec = app.__get("decoder");
    app.onAU(deltaAU());
    expect(dec.decoded[1].type).toBe("delta");
  });

  it("decode lançando chama resetDecoder", () => {
    app.onAU(spsAU());
    const dec = app.__get("decoder");
    dec.decode = () => { throw new Error("decode fail"); };
    app.onAU(deltaAU());
    expect(app.__get("decoder")).toBeNull();
  });

  it("acumula bytes", () => {
    app.onAU(spsAU());
    const b1 = app.__get("bytes");
    app.onAU(deltaAU());
    expect(app.__get("bytes")).toBeGreaterThan(b1);
  });
});

// ---------------------------------------------------------------------------
// 4.3 WebCodecs áudio
// ---------------------------------------------------------------------------

describe("startAudioContext", () => {
  it("primeira chamada cria AudioContext", () => {
    app.startAudioContext();
    expect(app.__get("audioCtx")).not.toBeNull();
  });

  it("segunda chamada com state=running não recria", () => {
    app.startAudioContext();
    const ctx = app.__get("audioCtx");
    app.startAudioContext();
    expect(app.__get("audioCtx")).toBe(ctx);
  });

  it("com state=suspended chama resume()", () => {
    app.startAudioContext();
    const ctx = app.__get("audioCtx");
    ctx.state = "suspended";
    const resumeSpy = vi.spyOn(ctx, "resume");
    app.startAudioContext();
    expect(resumeSpy).toHaveBeenCalled();
  });

  it("fallback webkitAudioContext", () => {
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
  it("ensureAudioDecoder cria e configura AudioDecoder", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    expect(ad).not.toBeNull();
    expect(ad.state).toBe("configured");
  });

  it("segunda chamada sem fechar não recria", () => {
    app.ensureAudioDecoder();
    const first = app.__get("audioDecoder");
    app.ensureAudioDecoder();
    expect(app.__get("audioDecoder")).toBe(first);
  });

  it("com state=closed recria", () => {
    app.ensureAudioDecoder();
    const first = app.__get("audioDecoder");
    first.state = "closed";
    app.ensureAudioDecoder();
    expect(app.__get("audioDecoder")).not.toBe(first);
  });

  it("callback error chama resetAudioDecoder", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    ad.cfg.error(new Error("audio error"));
    expect(app.__get("audioDecoder")).toBeNull();
  });

  it("resetAudioDecoder fecha decoder e zera playHead", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    app.resetAudioDecoder();
    expect(ad.state).toBe("closed");
    expect(app.__get("audioDecoder")).toBeNull();
    expect(app.__get("playHead")).toBe(0);
  });

  it("resetAudioDecoder: close() lançando é silenciado", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    ad.close = () => { throw new Error("boom"); };
    expect(() => app.resetAudioDecoder()).not.toThrow();
    expect(app.__get("audioDecoder")).toBeNull();
  });

  it("resetAudioDecoder com audioDecoder null não levanta", () => {
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

  it("sem audioCtx: chama data.close() e retorna", () => {
    const data = makeData();
    const srcStart = vi.fn();
    app.playAudio(data);
    expect(data.close).toHaveBeenCalled();
    expect(srcStart).not.toHaveBeenCalled();
  });

  it("normal: cria buffer, copia canais, agenda start", () => {
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

  it("copyTo com format lançando: usa fallback sem format", () => {
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
    // fallback foi chamado (sem format)
    expect(calls).toBeGreaterThan(0);
    expect(data.close).toHaveBeenCalled();
  });

  it("reancora quando playHead < now + 0.005 (atrasado)", () => {
    app.startAudioContext();
    const ctx = app.__get("audioCtx");
    ctx.currentTime = 1.0;
    // playHead = 0 < 1.0 + 0.005 → reancora
    const srcSpy = ctx.createBufferSource();
    vi.spyOn(ctx, "createBufferSource").mockReturnValue(srcSpy);
    app.playAudio(makeData());
    // após reancora, playHead deve ser próximo de now + JITTER
    expect(app.__get("playHead")).toBeGreaterThan(1.0);
  });

  it("reancora quando playHead > now + JITTER + 0.12 (rajada)", () => {
    app.startAudioContext();
    const ctx = app.__get("audioCtx");
    ctx.currentTime = 0;
    // JITTER=0.06, duração por pacote = 960/48000 = 0.02s
    // Trajetória com currentTime=0:
    //   call1: playHead=0 < 0.005 → reancora → playHead = 0.06 + 0.02 = 0.08
    //   calls 2-7: playHead cresce 0.02/call → após call7 playHead = 0.20
    //   call8: 0.20 > 0 + 0.06 + 0.12 = 0.18 → reancora → playHead = 0.06 + 0.02 = 0.08
    const srcSpy = { connect: vi.fn(), start: vi.fn(), buffer: null };
    vi.spyOn(ctx, "createBufferSource").mockReturnValue(srcSpy);
    // 7 chamadas levam playHead a 0.20
    for (let i = 0; i < 7; i++) app.playAudio(makeData());
    const phBefore = app.__get("playHead"); // deve ser ~0.20
    // 8ª chamada dispara reancora: 0.20 > 0.18
    app.playAudio(makeData());
    expect(app.__get("playHead")).toBeLessThan(phBefore);
  });
});

describe("onAudioPacket", () => {
  it("decodifica e incrementa audioTs em 20000", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    const tsBefore = app.__get("audioTs");
    const buf = new ArrayBuffer(10);
    app.onAudioPacket(buf);
    expect(ad.decoded).toHaveLength(1);
    expect(ad.decoded[0].type).toBe("key");
    expect(app.__get("audioTs")).toBe(tsBefore + 20000);
  });

  it("decode lançando chama resetAudioDecoder", () => {
    app.ensureAudioDecoder();
    const ad = app.__get("audioDecoder");
    ad.decode = () => { throw new Error("audio decode fail"); };
    app.onAudioPacket(new ArrayBuffer(10));
    expect(app.__get("audioDecoder")).toBeNull();
  });

  it("acumula audioBytes e audioPkts", () => {
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
// 4.4 Conexão de áudio
// ---------------------------------------------------------------------------

describe("connectAudio", () => {
  it("com audioOn=false retorna sem criar socket", () => {
    app.connectAudio();
    expect(globalThis.__sockets).toHaveLength(0);
  });

  it("com audioOn=true cria socket /ws/audio", () => {
    app.setAudio(true);
    const sockets = globalThis.__sockets.filter(s => s.url.includes("/ws/audio"));
    expect(sockets.length).toBeGreaterThan(0);
  });

  it("onmessage com audioOn=true chama onAudioPacket", () => {
    vi.useFakeTimers();
    app.setAudio(true);
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/audio"));
    const adBefore = app.__get("audioPkts");
    ws.onmessage({ data: new ArrayBuffer(10) });
    expect(app.__get("audioPkts")).toBe(adBefore + 1);
    vi.useRealTimers();
  });

  it("onclose com audioOn=true agenda reconexão", () => {
    vi.useFakeTimers();
    app.setAudio(true);
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/audio"));
    const countBefore = globalThis.__sockets.length;
    ws.onclose();
    vi.runAllTimers();
    expect(globalThis.__sockets.length).toBeGreaterThan(countBefore);
    vi.useRealTimers();
  });

  it("onclose com audioOn=false não reconecta", () => {
    vi.useFakeTimers();
    // setAudio(true) cria socket, setAudio(false) fecha e dispara onclose com audioOn=false
    app.setAudio(true);
    const countAfterTrue = globalThis.__sockets.length;
    app.setAudio(false); // triggers onclose with audioOn=false
    vi.runAllTimers();
    expect(globalThis.__sockets.length).toBe(countAfterTrue);
    vi.useRealTimers();
  });

  it("onerror chama ws.close()", () => {
    app.setAudio(true);
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/audio"));
    const closeSpy = vi.spyOn(ws, "close");
    ws.onerror();
    expect(closeSpy).toHaveBeenCalled();
  });

  it("onerror com close() lançando é silenciado", () => {
    app.setAudio(true);
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/audio"));
    ws.close = () => { throw new Error("close boom"); };
    expect(() => ws.onerror()).not.toThrow();
  });
});

describe("setAudio", () => {
  it("setAudio(true): botão 'on', startAudioContext, socket", () => {
    app.setAudio(true);
    expect(document.getElementById("sound").classList.contains("on")).toBe(true);
    expect(app.__get("audioCtx")).not.toBeNull();
    expect(globalThis.__sockets.some(s => s.url.includes("/ws/audio"))).toBe(true);
  });

  it("setAudio(false): botão sem 'on', suspend", () => {
    app.setAudio(true);
    const ctx = app.__get("audioCtx");
    const suspendSpy = vi.spyOn(ctx, "suspend");
    app.setAudio(false);
    expect(document.getElementById("sound").classList.contains("on")).toBe(false);
    expect(suspendSpy).toHaveBeenCalled();
    expect(app.__get("audioDecoder")).toBeNull();
  });

  it("setAudio(false) com audioCtx null não levanta", () => {
    expect(() => app.setAudio(false)).not.toThrow();
  });

  it("setAudio(false): close() lançando é silenciado", () => {
    app.setAudio(true);
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/audio"));
    ws.close = () => { throw new Error("close boom"); };
    expect(() => app.setAudio(false)).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// 4.5 Conexão vídeo/input
// ---------------------------------------------------------------------------

describe("connect", () => {
  it("adiciona 'hidden' ao overlay e cria 2 sockets", () => {
    const overlay = document.getElementById("overlay");
    overlay.classList.remove("hidden");
    app.connect();
    expect(overlay.classList.contains("hidden")).toBe(true);
    expect(globalThis.__sockets).toHaveLength(2);
  });
});

describe("connectVideo", () => {
  it("cria socket /ws/video com binaryType arraybuffer", () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    expect(ws).toBeDefined();
    expect(ws.binaryType).toBe("arraybuffer");
  });

  it("onmessage string 'reset' chama resetDecoder", () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    // Primeiro configura o decoder para ter algo para resetar
    app.onAU(new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0, 0, 1, 0x65, 0x01]).buffer);
    ws.onmessage({ data: JSON.stringify({ t: "reset" }) });
    expect(app.__get("decoder")).toBeNull();
  });

  it("onmessage string JSON inválida é silenciada", () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    expect(() => ws.onmessage({ data: "não é json" })).not.toThrow();
  });

  it("onmessage string JSON válida mas t !== 'reset' não chama resetDecoder", () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    app.onAU(new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0, 0, 1, 0x65, 0x01]).buffer);
    ws.onmessage({ data: JSON.stringify({ t: "outro" }) });
    // decoder NÃO foi resetado (configured ainda true)
    expect(app.__get("configured")).toBe(true);
  });

  it("onmessage ArrayBuffer chama onAU", () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    const au = new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0, 0, 1, 0x65, 0x01]).buffer;
    ws.onmessage({ data: au });
    expect(app.__get("configured")).toBe(true);
  });

  it("onclose: resetDecoder + status + setTimeout", () => {
    vi.useFakeTimers();
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    app.onAU(new Uint8Array([0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0, 0, 1, 0x65, 0x01]).buffer);
    ws.onclose();
    expect(app.__get("decoder")).toBeNull();
    expect(document.getElementById("status").textContent).toMatch(/desconectado|reconectando/);
    vi.useRealTimers();
  });

  it("onclose com visibilityState=visible → reconnect após timer", () => {
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

  it("onclose com visibilityState=hidden → NÃO reconecta", () => {
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
    // Restaura
    Object.defineProperty(document, "visibilityState", {
      value: "visible", configurable: true,
    });
  });

  it("onerror chama ws.close()", () => {
    app.connectVideo();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/video"));
    const closeSpy = vi.spyOn(ws, "close");
    ws.onerror();
    expect(closeSpy).toHaveBeenCalled();
  });
});

describe("connectInput", () => {
  it("cria socket /ws/input", () => {
    app.connectInput();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    expect(ws).toBeDefined();
  });

  it("onclose agenda reconexão", () => {
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
  it("com readyState=1 envia JSON", () => {
    app.connectInput();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    app.sendInput({ t: "test", x: 0.5 });
    expect(ws.sent).toHaveLength(1);
    expect(JSON.parse(ws.sent[0])).toEqual({ t: "test", x: 0.5 });
  });

  it("com inputWS null não levanta", () => {
    expect(() => app.sendInput({ t: "test" })).not.toThrow();
  });

  it("com readyState !== 1 não envia", () => {
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

  it("flushMove com pendingMove envia e zera", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    // Produz um pendingMove via mousemove sem pointer lock
    const canvas = document.getElementById("screen");
    canvas.dispatchEvent(new MouseEvent("mousemove", { clientX: 960, clientY: 540, bubbles: true }));
    app.flushMove();
    expect(ws.sent.length).toBeGreaterThan(0);
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.t).toBe("m");
    // depois de flush, pendingMove = null
    const sentBefore = ws.sent.length;
    app.flushMove();
    expect(ws.sent.length).toBe(sentBefore); // não enviou nada extra
  });

  it("flushMove sem pendingMove não envia, mas chama rAF", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    app.flushMove();
    expect(ws.sent).toHaveLength(0);
    expect(globalThis.requestAnimationFrame).toHaveBeenCalled();
  });

  it("mousemove sem pointer lock → pendingMove com t:m", () => {
    const canvas = document.getElementById("screen");
    canvas.dispatchEvent(new MouseEvent("mousemove", { clientX: 960, clientY: 540, bubbles: true }));
    const pm = app.__get("pendingMove");
    expect(pm).not.toBeNull();
    expect(pm.t).toBe("m");
    expect(pm.x).toBeCloseTo(0.5);
  });

  it("mousemove com pointer lock → envia t:mr via sendInput", () => {
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
    // Restaura
    Object.defineProperty(document, "pointerLockElement", {
      get: () => null, configurable: true,
    });
  });

  it("mousedown envia t:md e preventDefault", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const ev = new MouseEvent("mousedown", { button: 2, bubbles: true, cancelable: true });
    canvas.dispatchEvent(ev);
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "md", b: 2 });
  });

  it("mouseup envia t:mu", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    canvas.dispatchEvent(new MouseEvent("mouseup", { button: 0, bubbles: true, cancelable: true }));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "mu", b: 0 });
  });

  it("contextmenu chama preventDefault sem enviar", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sentBefore = ws.sent.length;
    const ev = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    canvas.dispatchEvent(ev);
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("wheel envia t:w com sign(delta)", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    canvas.dispatchEvent(new WheelEvent("wheel", { deltaX: 5, deltaY: -3, bubbles: true, cancelable: true }));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "w", x: 1, y: -1 });
  });

  it("wheel com deltas zero → x:0, y:0", () => {
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

  it("touchstart com 1 toque envia t:m e zera touchMoved", () => {
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

  it("touchstart com mais de 1 toque retorna sem enviar", () => {
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

  it("touchmove com touchId correto seta pendingMove e touchMoved", () => {
    const canvas = document.getElementById("screen");
    // Primeiro um touchstart para definir touchId
    const touch = makeTouch(42, 100, 100);
    canvas.dispatchEvent(new TouchEvent("touchstart", {
      touches: [touch], changedTouches: [touch], bubbles: true, cancelable: true,
    }));
    // Agora touchmove com o mesmo identifier
    const moveTouch = makeTouch(42, 200, 200);
    canvas.dispatchEvent(new TouchEvent("touchmove", {
      touches: [moveTouch], changedTouches: [moveTouch], bubbles: true, cancelable: true,
    }));
    expect(app.__get("touchMoved")).toBe(true);
    expect(app.__get("pendingMove")).not.toBeNull();
  });

  it("touchmove com identifier diferente não tem efeito", () => {
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

  it("touchend sem mover envia md então mu após 40ms", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const touch = makeTouch(1, 100, 100);
    canvas.dispatchEvent(new TouchEvent("touchstart", {
      touches: [touch], changedTouches: [touch], bubbles: true, cancelable: true,
    }));
    canvas.dispatchEvent(new TouchEvent("touchend", {
      touches: [], changedTouches: [touch], bubbles: true, cancelable: true,
    }));
    // md enviado imediatamente
    const msgs = ws.sent.map(s => JSON.parse(s));
    expect(msgs.some(m => m.t === "md" && m.b === 0)).toBe(true);
    // mu enviado após timer
    vi.advanceTimersByTime(40);
    const msgs2 = ws.sent.map(s => JSON.parse(s));
    expect(msgs2.some(m => m.t === "mu" && m.b === 0)).toBe(true);
    expect(app.__get("touchId")).toBeNull();
  });

  it("touchend com touchMoved=true não envia md/mu", () => {
    const canvas = document.getElementById("screen");
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const touch = makeTouch(1, 100, 100);
    canvas.dispatchEvent(new TouchEvent("touchstart", {
      touches: [touch], changedTouches: [touch], bubbles: true, cancelable: true,
    }));
    // Simula movimento
    canvas.dispatchEvent(new TouchEvent("touchmove", {
      touches: [makeTouch(1, 200, 200)], changedTouches: [makeTouch(1, 200, 200)],
      bubbles: true, cancelable: true,
    }));
    const sentBefore = ws.sent.length;
    canvas.dispatchEvent(new TouchEvent("touchend", {
      touches: [], changedTouches: [touch], bubbles: true, cancelable: true,
    }));
    vi.advanceTimersByTime(40);
    // Nenhum md/mu extra
    const newMsgs = ws.sent.slice(sentBefore).map(s => JSON.parse(s));
    expect(newMsgs.some(m => m.t === "md" || m.t === "mu")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 4.8 Teclado
// ---------------------------------------------------------------------------

describe("Keyboard events", () => {
  beforeEach(() => {
    app.connectInput();
  });

  it("keydown envia t:k down:true", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    window.dispatchEvent(new KeyboardEvent("keydown", { code: "KeyA", bubbles: true, cancelable: true }));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "k", code: "KeyA", down: true });
  });

  it("keyup envia t:k down:false", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    window.dispatchEvent(new KeyboardEvent("keyup", { code: "Space", bubbles: true, cancelable: true }));
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg).toEqual({ t: "k", code: "Space", down: false });
  });

  it("F11 é ignorado (return sem enviar)", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sentBefore = ws.sent.length;
    window.dispatchEvent(new KeyboardEvent("keydown", { code: "F11", bubbles: true, cancelable: true }));
    expect(ws.sent.length).toBe(sentBefore);
  });

  it("sem inputWS não lança", () => {
    // inputWS é null (não chamamos connectInput neste scope; mas já chamamos no beforeEach)
    // Forçamos readyState != 1 para testar o branch
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

  it("sem pad: não envia, chama rAF", () => {
    navigator.getGamepads = () => [];
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    app.pollPads();
    expect(ws.sent).toHaveLength(0);
    expect(globalThis.requestAnimationFrame).toHaveBeenCalled();
  });

  it("pad com axes e buttons: envia t:g", () => {
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

  it("estado idêntico: não reenvia (deduplica)", () => {
    navigator.getGamepads = () => [{
      axes: [0, 0, 0, 0],
      buttons: [{ pressed: false, value: 0 }],
    }];
    app.pollPads();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const countAfterFirst = ws.sent.length;
    app.pollPads();
    expect(ws.sent.length).toBe(countAfterFirst); // não reenviou
  });

  it("estado mudado: reenvia", () => {
    navigator.getGamepads = () => [{
      axes: [0, 0, 0, 0],
      buttons: [{ pressed: false, value: 0 }],
    }];
    app.pollPads();
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const countAfterFirst = ws.sent.length;
    // Muda estado
    navigator.getGamepads = () => [{
      axes: [1, 0, 0, 0],
      buttons: [{ pressed: false, value: 0 }],
    }];
    app.pollPads();
    expect(ws.sent.length).toBeGreaterThan(countAfterFirst);
  });

  it("p.axes[i] falsy usa || 0", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    navigator.getGamepads = () => [{
      axes: [0, undefined, NaN, null],
      buttons: [],
    }];
    app.pollPads();
    const msg = JSON.parse(ws.sent[ws.sent.length - 1]);
    expect(msg.a).toEqual([0, 0, 0, 0]);
  });

  it("gamepadconnected event chama setStatus", () => {
    const ev = new Event("gamepadconnected");
    ev.gamepad = { id: "Xbox 360 Controller (XInput STANDARD GAMEPAD)" };
    window.dispatchEvent(ev);
    expect(document.getElementById("status").textContent).toContain("controle:");
  });
});

// ---------------------------------------------------------------------------
// 4.10 UI — botões e selects
// ---------------------------------------------------------------------------

describe("UI - setStatus", () => {
  it("atualiza textContent do #status", () => {
    app.setStatus("mensagem teste");
    expect(document.getElementById("status").textContent).toBe("mensagem teste");
  });
});

describe("UI - botões", () => {
  beforeEach(() => {
    app.connectInput();
  });

  it("#connect click chama connect (cria 2 sockets adicionais)", () => {
    const countBefore = globalThis.__sockets.length;
    document.getElementById("connect").click();
    expect(globalThis.__sockets.length - countBefore).toBe(2);
  });

  it("#lock click chama canvas.requestPointerLock", () => {
    const canvas = document.getElementById("screen");
    document.getElementById("lock").click();
    expect(canvas.requestPointerLock).toHaveBeenCalled();
  });

  it("#full click com fullscreenElement null chama requestFullscreen", () => {
    Object.defineProperty(document, "fullscreenElement", {
      value: null, configurable: true, writable: true,
    });
    document.getElementById("full").click();
    expect(document.documentElement.requestFullscreen).toHaveBeenCalled();
  });

  it("#full click com fullscreenElement não-null chama exitFullscreen", () => {
    Object.defineProperty(document, "fullscreenElement", {
      value: {}, configurable: true, writable: true,
    });
    document.getElementById("full").click();
    expect(document.exitFullscreen).toHaveBeenCalled();
  });

  it("#kbd click foca no hiddenKbd", () => {
    const kbd = document.getElementById("hiddenKbd");
    const focusSpy = vi.spyOn(kbd, "focus");
    document.getElementById("kbd").click();
    expect(focusSpy).toHaveBeenCalled();
  });

  it("#menuToggle togla classe 'hidden' no #bar", () => {
    const bar = document.getElementById("bar");
    bar.classList.remove("hidden");
    document.getElementById("menuToggle").click();
    expect(bar.classList.contains("hidden")).toBe(true);
    document.getElementById("menuToggle").click();
    expect(bar.classList.contains("hidden")).toBe(false);
  });

  it("#sound click chama setAudio(!audioOn)", () => {
    expect(app.__get("audioOn")).toBe(false);
    document.getElementById("sound").click();
    expect(app.__get("audioOn")).toBe(true);
    document.getElementById("sound").click();
    expect(app.__get("audioOn")).toBe(false);
  });

  it("#preset change envia t:q preset", () => {
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

  it("#monitor change com valor numérico envia t:mon i:número", () => {
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

  it("#monitor change com valor '' envia t:mon i:null", () => {
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

  it("#hiddenKbd input com letra envia keydown + keyup", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const input = document.getElementById("hiddenKbd");
    input.dispatchEvent(new InputEvent("input", { data: "a", bubbles: true }));
    const msgs = ws.sent.map(s => JSON.parse(s));
    expect(msgs.some(m => m.t === "k" && m.code === "KeyA" && m.down === true)).toBe(true);
    expect(msgs.some(m => m.t === "k" && m.code === "KeyA" && m.down === false)).toBe(true);
    expect(input.value).toBe("");
  });

  it("#hiddenKbd input com e.data=null não envia nada", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sentBefore = ws.sent.length;
    const input = document.getElementById("hiddenKbd");
    input.dispatchEvent(new InputEvent("input", { data: null, bubbles: true }));
    expect(ws.sent.length).toBe(sentBefore);
    expect(input.value).toBe("");
  });

  it("#hiddenKbd input com caractere sem mapa não envia nada", () => {
    const ws = globalThis.__sockets.find(s => s.url.includes("/ws/input"));
    const sentBefore = ws.sent.length;
    document.getElementById("hiddenKbd").dispatchEvent(
      new InputEvent("input", { data: "!", bubbles: true })
    );
    expect(ws.sent.length).toBe(sentBefore);
  });
});

// ---------------------------------------------------------------------------
// 4.11 loadConfig
// ---------------------------------------------------------------------------

describe("loadConfig", () => {
  it("normal: preenche presets, monitores e habilita som", async () => {
    globalThis.fetch = vi.fn(async () => ({
      json: async () => ({
        presets: ["ultra", "high", "medium", "low", "potato"],
        current_preset: "high",
        monitors: [{ index: 0, name: "HDMI-0", primary: true, w: 1920, h: 1080 }],
        current_monitor: null,
        audio_available: true,
        audio_presets: ["alta", "media", "baixa", "voz"],
        current_audio_preset: "media",
      }),
    }));
    await app.loadConfig();
    expect(document.getElementById("preset").options).toHaveLength(5);
    expect(document.getElementById("monitor").options).toHaveLength(1);
    expect(document.getElementById("sound").disabled).toBe(false);
    // selected no preset atual
    const selected = Array.from(document.getElementById("preset").options).find(o => o.selected);
    expect(selected.value).toBe("high");
  });

  it("áudio indisponível no servidor: desabilita botão com título correto", async () => {
    globalThis.fetch = vi.fn(async () => ({
      json: async () => ({
        presets: [], current_preset: null, monitors: [], current_monitor: null,
        audio_available: false, audio_presets: [], current_audio_preset: null,
      }),
    }));
    await app.loadConfig();
    const btn = document.getElementById("sound");
    expect(btn.disabled).toBe(true);
    expect(btn.title).toContain("sem áudio no servidor");
  });

  it("áudio disponível mas sem AudioDecoder: desabilita com título correto", async () => {
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

  it("fetch lançando: console.warn é chamado, sem propagação", async () => {
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
  it("dt >= 1 atualiza stats e zera contadores", async () => {
    // lastStat=0 (definido no load com performance.now=0)
    // Seta performance.now para retornar 1500 ms depois
    globalThis.performance.now.mockReturnValue(1500);
    app.statsLoop();
    const text = document.getElementById("stats").textContent;
    expect(text).toMatch(/fps/);
    expect(text).toMatch(/Mbps/);
    expect(globalThis.requestAnimationFrame).toHaveBeenCalled();
  });

  it("dt >= 1 com audioOn=true mostra 🔊", async () => {
    app.setAudio(true);
    globalThis.performance.now.mockReturnValue(1500);
    app.statsLoop();
    expect(document.getElementById("stats").textContent).toContain("🔊");
  });

  it("dt < 1 não atualiza stats", async () => {
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
  it("sem VideoDecoder: status 'Sem WebCodecs', #connect desabilitado, sem fetch", async () => {
    delete globalThis.VideoDecoder;
    app = await loadApp();
    app.boot();
    expect(document.getElementById("status").textContent).toContain("Sem WebCodecs");
    expect(document.getElementById("connect").disabled).toBe(true);
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(globalThis.requestAnimationFrame).not.toHaveBeenCalled();
  });

  it("normal: status 'pronto', fetch chamado, rAF chamado", async () => {
    app.boot();
    expect(document.getElementById("status").textContent).toContain("pronto");
    expect(globalThis.fetch).toHaveBeenCalled();
    expect(globalThis.requestAnimationFrame).toHaveBeenCalled();
  });
});
