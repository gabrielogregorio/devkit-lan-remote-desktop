/**
 * Catalog tests for web/i18n.js.
 *
 * English is the key, so a typo in a t("...") call or in the HTML silently shows English to a
 * pt-BR user. These tests collect every translated string from the source files and fail on a
 * missing pt-BR line, on different {placeholders} or on a pt-BR line nothing uses anymore.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";
import I18N from "../i18n.js";
import { loadApp } from "./setup.js";

const { PT_BR, detectLanguage, fill, translator, translatePage, keyOf } = I18N;

const read = (name) => fs.readFileSync(path.resolve(__dirname, "..", name), "utf8");
const app = read("app.js");
const pages = [read("index.html"), read("gamepad-debug.html")];

const unescapeJs = (text) => text.replace(/\\(["'\\])/g, "$1");
const unescapeHtml = (text) => text.replace(/&amp;/g, "&").replace(/&quot;/g, '"');

// t("...") and t('...') calls: app.js and the inline script of gamepad-debug.html.
const tCalls = (source) => [
  ...[...source.matchAll(/\bt\("((?:[^"\\]|\\.)*)"/g)].map((m) => unescapeJs(m[1])),
  ...[...source.matchAll(/\bt\('((?:[^'\\]|\\.)*)'/g)].map((m) => unescapeJs(m[1])),
];

// Elements marked data-i18n / data-i18n-html: their text is the key.
const markedText = (html) =>
  [...html.matchAll(/<(\w+)\b[^>]*\sdata-i18n(?:-html)?(?=[\s>])[^>]*>([\s\S]*?)<\/\1>/g)]
    .map((m) => keyOf(unescapeHtml(m[2])));

// Elements marked data-i18n-title: their title attribute is the key.
const markedTitles = (html) =>
  [...html.matchAll(/<\w+\b[^>]*\sdata-i18n-title\b[^>]*>/g)]
    .map((m) => /\stitle="([^"]*)"/.exec(m[0]))
    .filter(Boolean)
    .map((m) => keyOf(unescapeHtml(m[1])));

const usedStrings = new Set([
  ...tCalls(app),
  ...pages.flatMap((html) => [...tCalls(html), ...markedText(html), ...markedTitles(html)]),
]);

const placeholders = (text) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

describe("i18n catalog", () => {
  it("the pages use translated strings", () => {
    expect(usedStrings.size, `found only ${usedStrings.size}`).toBeGreaterThan(40);
    expect(usedStrings.has("ready - tap Connect")).toBe(true);
    expect(usedStrings.has("Connect")).toBe(true);
    expect(usedStrings.has("toggle audio")).toBe(true);
    expect(usedStrings.has("Gamepad diagnostics")).toBe(true);
  });

  it("every string on the pages has a pt-BR line", () => {
    const missing = [...usedStrings].filter((text) => !(text in PT_BR));
    expect(missing, "strings without a pt-BR line").toEqual([]);
  });

  it("pt-BR lines keep the same {placeholders}", () => {
    for (const [english, portuguese] of Object.entries(PT_BR)) {
      expect(placeholders(portuguese), english).toEqual(placeholders(english));
    }
  });

  it("no pt-BR line is left unused", () => {
    const unused = Object.keys(PT_BR).filter((text) => !usedStrings.has(text));
    expect(unused, "pt-BR lines nothing uses anymore").toEqual([]);
  });
});

describe("detectLanguage", () => {
  it("follows the first browser language: pt* is pt-BR, anything else English", () => {
    expect(detectLanguage("", ["pt-BR", "en"])).toBe("pt-BR");
    expect(detectLanguage("", ["pt"])).toBe("pt-BR");
    expect(detectLanguage("", ["pt-PT"])).toBe("pt-BR");
    expect(detectLanguage("", ["en-US", "pt-BR"])).toBe("en");
    expect(detectLanguage("", ["es"])).toBe("en");
    expect(detectLanguage("", [])).toBe("en");
    expect(detectLanguage(undefined, undefined)).toBe("en");
  });

  it("?lang= overrides the browser", () => {
    expect(detectLanguage("?lang=en", ["pt-BR"])).toBe("en");
    expect(detectLanguage("?lang=pt-BR", ["en-US"])).toBe("pt-BR");
    expect(detectLanguage("?lang=pt_BR", ["en-US"])).toBe("pt-BR");
  });
});

describe("translator / translatePage", () => {
  it("falls back to English and fills placeholders", () => {
    expect(translator("pt-BR")("Connect")).toBe("Conectar");
    expect(translator("en")("Connect")).toBe("Connect");
    expect(translator("pt-BR")("not in the catalog")).toBe("not in the catalog");
    expect(translator("pt-BR")("controller: {id}", { id: "Xbox" })).toBe("controle: Xbox");
    expect(fill("{a} and {b}", { a: 1 })).toBe("1 and {b}");
  });

  it("translates data-i18n, data-i18n-html and data-i18n-title and sets <html lang>", () => {
    document.body.innerHTML = `
      <button id="a" data-i18n>Connect</button>
      <p id="b" data-i18n-html>Move EVERYTHING on the controller (sticks, triggers all the way down,
        D-Pad, buttons). The values that change are the real indices the browser reports.
        <b>Take a screenshot of this</b> with the LT and RT triggers half pressed.</p>
      <button id="c" title="fullscreen" data-i18n-title>⛶</button>`;
    translatePage(document, "pt-BR");
    expect(document.documentElement.lang).toBe("pt-BR");
    expect(document.getElementById("a").textContent).toBe("Conectar");
    expect(document.getElementById("b").innerHTML).toContain("<b>Tire um print disto</b>");
    expect(document.getElementById("c").title).toBe("tela cheia");
    expect(document.getElementById("c").textContent).toBe("⛶");
  });
});

describe("app.js language", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("is English by default (jsdom reports en-US)", async () => {
    const app = await loadApp();
    expect(app.LANG).toBe("en");
    app.boot();
    expect(document.getElementById("status").textContent).toBe("ready - tap Connect");
    expect(document.documentElement.lang).toBe("en");
  });

  it("?lang=pt-BR shows Portuguese", async () => {
    vi.stubGlobal("location", { protocol: "https:", host: "h:8443", search: "?lang=pt-BR" });
    const app = await loadApp();
    expect(app.LANG).toBe("pt-BR");
    app.boot();
    expect(document.getElementById("status").textContent).toBe("pronto - toque em Conectar");
    expect(document.documentElement.lang).toBe("pt-BR");
    app.setAudio(true);
    expect(document.getElementById("sound").textContent).toBe("🔊 som");
  });
});
