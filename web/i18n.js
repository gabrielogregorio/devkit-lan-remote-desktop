/*
 * Page strings. English is the source text and the key; PT_BR holds the Brazilian Portuguese line.
 * The language comes from ?lang=en|pt-BR or else from the browser's first preferred language:
 * pt* shows pt-BR, anything else shows English. web/__tests__/i18n.test.js fails on a missing
 * line, on different {placeholders} or on a line that is no longer used.
 *
 * Plain script on purpose (no bundler): the browser loads it with <script src="/i18n.js"> before
 * app.js and gets the global `I18N`; vitest loads the same file with require().
 */
(function (root) {
  "use strict";

  const PT_BR = {
    // index.html
    "connecting…": "conectando…",
    Connect: "Conectar",
    menu: "menu",
    preset: "preset",
    resolution: "resolução",
    fps: "fps",
    bitrate: "bitrate",
    audio: "áudio",
    monitor: "monitor",
    "toggle audio": "ligar/desligar áudio",
    "capture mouse (games/FPS)": "capturar mouse (jogos/FPS)",
    "virtual keyboard": "teclado virtual",
    "remap controller (PS2/USB)": "remapear controle (PS2/USB)",
    "reset mapping (back to passthrough)": "resetar mapeamento (volta a passar direto)",
    fullscreen: "tela cheia",
    shortcuts: "atalhos",
    "close active window": "fechar janela ativa",
    "switch windows": "alternar janelas",
    "activities menu (Super)": "menu de atividades (Super)",
    "show menu": "mostrar menu",
    "Remap controller": "Remapear controle",
    "Press the controller…": "Acione o controle…",
    Skip: "Pular",
    Cancel: "Cancelar",
    "Tip: for PS2/USB controllers, follow each step by pressing the requested button/direction. The profile is saved on the server.":
      "Dica: para controles PS2/USB, siga cada passo apertando o botão/direção pedido. O perfil fica salvo no servidor.",

    // app.js
    "this browser could not start the H.264 decoder": "este navegador não conseguiu iniciar o decoder H.264",
    "🔊 sound": "🔊 som",
    "🔇 sound": "🔇 som",
    "video disconnected - reconnecting…": "vídeo desconectado - reconectando…",
    "keyboard captured - click outside to release": "teclado capturado - clique fora p/ liberar",
    "controller: {id}": "controle: {id}",
    "LB (left bumper)": "LB (ombro esq.)",
    "RB (right bumper)": "RB (ombro dir.)",
    "L3 (click left stick)": "L3 (clicar stick esq.)",
    "R3 (click right stick)": "R3 (clicar stick dir.)",
    "Guide/Home (or skip)": "Guide/Home (ou pule)",
    "LT (left trigger) - press all the way down": "LT (gatilho esq.) - aperte até o fundo",
    "RT (right trigger) - press all the way down": "RT (gatilho dir.) - aperte até o fundo",
    "LEFT stick → right": "Stick ESQ. → direita",
    "LEFT stick → down": "Stick ESQ. → baixo",
    "RIGHT stick → right": "Stick DIR. → direita",
    "RIGHT stick → down": "Stick DIR. → baixo",
    "connect a controller and move it before remapping": "conecte um controle e mexa nele antes de remapear",
    "standard controller (Xbox) - already passes straight through, no remap needed":
      "controle padrão (Xbox) - já passa direto, sem remap necessário",
    "Remap ({step}/{total})": "Remapear ({step}/{total})",
    "Press: {label}": "Acione: {label}",
    "controller remapped and saved on the server": "controle remapeado e salvo no servidor",
    "remapped (but saving to the server failed)": "remapeado (mas falhou ao salvar no servidor)",
    "connect a controller to reset the mapping": "conecte um controle pra resetar o mapeamento",
    "controller mapping reset (passes straight through)": "mapeamento do controle resetado (passa direto)",
    "reset locally (server failed)": "resetado localmente (falhou no servidor)",
    "sent: {combo}": "enviado: {combo}",
    "preset: {value}": "preset: {value}",
    "resolution: {value}p": "resolução: {value}p",
    "fps: {value}": "fps: {value}",
    "bitrate: {value}": "bitrate: {value}",
    "audio: {value}": "áudio: {value}",
    "✕ close keyboard": "✕ fechar teclado",
    "browser without AudioDecoder": "navegador sem AudioDecoder",
    "no audio on the server": "sem áudio no servidor",
    "No WebCodecs in this browser. Use Chrome/Firefox/Edge over HTTPS.":
      "Sem WebCodecs neste navegador. Use Chrome/Firefox/Edge via HTTPS.",
    "ready - tap Connect": "pronto - toque em Conectar",

    // gamepad-debug.html
    "LAN Remote Desktop · gamepad diagnostics": "LAN Remote Desktop · diagnóstico de gamepad",
    "Gamepad diagnostics": "Diagnóstico de gamepad",
    "Move EVERYTHING on the controller (sticks, triggers all the way down, D-Pad, buttons). The values that change are the real indices the browser reports. <b>Take a screenshot of this</b> with the LT and RT triggers half pressed.":
      "Mexa em TUDO no controle (sticks, gatilhos até o fundo, D-Pad, botões). Os valores que mudam são os índices reais que o navegador reporta. <b>Tire um print disto</b> com os gatilhos LT e RT apertados pela metade.",
    "Connect a controller and press any button…": "Conecte um controle e aperte qualquer botão…",
    "(empty / NOT standard)": "(vazio / NÃO-standard)",
    "buttons: {buttons} · axes: {axes}": "botões: {buttons} · eixos: {axes}",
    button: "botão",
    value: "valor",
    axis: "eixo",
    'For a real Xbox, expect: mapping = "standard", btn[6]/btn[7] = LT/RT triggers (value from 0 to 1), btn[12..15] = D-Pad, axis[0..3] = sticks. If yours shows an empty mapping and the triggers as axes, it is a non-standard controller (it needs the remap).':
      'Para um Xbox real esperado: mapping = "standard", btn[6]/btn[7] = gatilhos LT/RT (value de 0 a 1), btn[12..15] = D-Pad, axis[0..3] = sticks. Se o seu mostra mapping vazio e os gatilhos como axis, é um controle não-standard (precisa do remap).',
  };

  const isPortuguese = (language) => /^pt([-_]|$)/i.test(language || "");

  function detectLanguage(search = "", languages = []) {
    const forced = new URLSearchParams(search || "").get("lang");
    if (forced) {
      return isPortuguese(forced) ? "pt-BR" : "en";
    }
    return isPortuguese(languages && languages[0]) ? "pt-BR" : "en";
  }

  const fill = (text, values = {}) =>
    text.replace(/\{(\w+)\}/g, (match, name) => (name in values ? String(values[name]) : match));

  const translator = (language) => (text, values) =>
    fill(language === "pt-BR" ? (PT_BR[text] ?? text) : text, values);

  // The HTML text is the key, so whitespace from source indentation is collapsed before lookup
  // (the test collapses it the same way).
  const keyOf = (text) => text.trim().replace(/\s+/g, " ");

  // Applies the static strings of a page: data-i18n (textContent), data-i18n-html (innerHTML, for
  // lines with markup) and data-i18n-title (title attribute). Also sets <html lang>.
  function translatePage(doc, language) {
    const t = translator(language);
    doc.documentElement.lang = language;
    doc.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(keyOf(el.textContent));
    });
    doc.querySelectorAll("[data-i18n-html]").forEach((el) => {
      el.innerHTML = t(keyOf(el.innerHTML));
    });
    doc.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.title = t(keyOf(el.title));
    });
  }

  const api = { PT_BR, detectLanguage, fill, translator, translatePage, keyOf };
  if (typeof module === "object" && module && module.exports) module.exports = api;
  else root.I18N = api;
})(typeof window !== "undefined" ? window : globalThis);
