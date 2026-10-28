/**
 * Tests for the diagnostics page (`web/gamepad-debug.html`).
 *
 * It is the tool used to find out HOW a controller shows up before touching any
 * mapping - if it lies, the remap comes out wrong and the trigger goes back to
 * "double clicking". Its script is inline (on purpose: the page has to open on
 * its own), so here we extract the <script> and run it in a fake DOM, driving
 * requestAnimationFrame by hand to step frame by frame.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";
import I18N from "../i18n.js";

const HTML = fs.readFileSync(
  path.resolve(__dirname, "../gamepad-debug.html"), "utf8");

/** Mounts the page in jsdom and returns `render` (1 frame per call). */
function mountPage() {
  const body = HTML.split("<body>")[1].split("<script")[0];
  const script = HTML.split("<script>")[1].split("</script>")[0];
  document.body.innerHTML = body;
  // the real page gets this global from <script src="/i18n.js">
  window.I18N = I18N;

  let captured = null;
  globalThis.requestAnimationFrame = (cb) => { captured = cb; };
  new Function(script)();          // runs the inline script like the browser would
  expect(captured).toBeTypeOf("function");
  return () => captured();         // 1 frame
}

const btn = (v, pressed = v > 0.5) => ({ pressed, value: v });

function pad(over = {}) {
  return {
    index: 0,
    id: "Twin USB Joystick",
    mapping: "",
    axes: [0, 0, 0, 0],
    buttons: [btn(0), btn(0)],
    ...over,
  };
}

const output = () => document.getElementById("out").innerHTML;

beforeEach(() => {
  navigator.getGamepads = () => [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("gamepad-debug: rendering", () => {
  it("with no controller connected it keeps the initial instruction", () => {
    const frame = mountPage();
    frame();
    expect(output()).toContain("Connect a controller");
  });

  it("skips the empty getGamepads positions (null slots)", () => {
    navigator.getGamepads = () => [null, null, pad()];
    const frame = mountPage();
    expect(() => frame()).not.toThrow();
    expect(output()).toContain("Twin USB Joystick");
  });

  it("shows the id, the mapping and the value of every button and axis", () => {
    navigator.getGamepads = () => [pad({
      axes: [0.5, -0.25, 0, 0],
      buttons: [btn(0), btn(1), btn(0.5)],
    })];
    const frame = mountPage();
    frame();
    const html = output();
    expect(html).toContain("Twin USB Joystick");
    expect(html).toContain("btn[2]");
    expect(html).toContain("0.500");     // analog value with 3 decimals
    expect(html).toContain("axis[1]");
    expect(html).toContain("-0.250");
    expect(html).toContain("buttons: 3");
    expect(html).toContain("axes: 4");
  });

  it("marks an empty mapping as NOT standard (that is the one that needs a remap)", () => {
    navigator.getGamepads = () => [pad({ mapping: "" })];
    const frame = mountPage();
    frame();
    expect(output()).toContain("NOT standard");
  });

  it("shows 'standard' when the browser recognizes the controller", () => {
    navigator.getGamepads = () => [pad({ id: "Xbox", mapping: "standard" })];
    const frame = mountPage();
    frame();
    expect(output()).toContain("standard");
    expect(output()).not.toContain("NOT standard");
  });

  it("an undefined/null axis does not break the page", () => {
    navigator.getGamepads = () => [pad({ axes: [undefined, null, 0, 0] })];
    const frame = mountPage();
    expect(() => frame()).not.toThrow();
    expect(output()).toContain("0.000");
  });

  it("a controller with no buttons or axes still renders", () => {
    navigator.getGamepads = () => [pad({ axes: [], buttons: [] })];
    const frame = mountPage();
    expect(() => frame()).not.toThrow();
    expect(output()).toContain("buttons: 0");
  });

  it("keeps rescheduling itself (the loop must not stop on its own)", () => {
    navigator.getGamepads = () => [pad()];
    const frame = mountPage();
    for (let i = 0; i < 5; i++) expect(() => frame()).not.toThrow();
  });
});

describe("gamepad-debug: history (min/max and oscillation)", () => {
  it("accumulates the min and max seen, not only the current value", () => {
    const p = pad({ buttons: [btn(0.2)] });
    navigator.getGamepads = () => [p];
    const frame = mountPage();
    frame();
    p.buttons[0] = btn(0.9);
    frame();
    p.buttons[0] = btn(0.5);
    frame();
    expect(output()).toContain("0.20..0.90");   // the whole range, with the current one at 0.5
    expect(output()).toContain("0.500");
  });

  it("an oscillating trigger (the PS2 controller bug) accumulates oscillations and warns", () => {
    const p = pad({ buttons: [btn(0)] });
    navigator.getGamepads = () => [p];
    const frame = mountPage();
    // goes up and down several times: exactly the "double click" pattern
    for (const v of [0, 1, 0, 1, 0, 1, 0, 1]) {
      p.buttons[0] = btn(v);
      frame();
    }
    expect(output()).toContain("⚠");            // crossed the 3-reversal threshold
  });

  it("a smooth ramp (pressing slowly) does NOT count as oscillation", () => {
    const p = pad({ buttons: [btn(0)] });
    navigator.getGamepads = () => [p];
    const frame = mountPage();
    for (let v = 0; v <= 1.0001; v += 0.1) {
      p.buttons[0] = btn(v);
      frame();
    }
    expect(output()).not.toContain("⚠");
  });

  it("noise below 0.02 is ignored (no false oscillation)", () => {
    const p = pad({ buttons: [btn(0.5)] });
    navigator.getGamepads = () => [p];
    const frame = mountPage();
    for (const v of [0.5, 0.505, 0.5, 0.51, 0.5, 0.495, 0.5, 0.505]) {
      p.buttons[0] = btn(v);
      frame();
    }
    expect(output()).not.toContain("⚠");
  });

  it("each controller has its own history (indices are not mixed)", () => {
    const p0 = pad({ index: 0, id: "PAD-A", buttons: [btn(0)] });
    const p1 = pad({ index: 1, id: "PAD-B", buttons: [btn(0)] });
    navigator.getGamepads = () => [p0, p1];
    const frame = mountPage();
    for (const v of [0, 1, 0, 1, 0, 1, 0, 1]) {
      p0.buttons[0] = btn(v);       // only A oscillates
      frame();
    }
    const html = output();
    const blockA = html.slice(html.indexOf("PAD-A"), html.indexOf("PAD-B"));
    const blockB = html.slice(html.indexOf("PAD-B"));
    expect(blockA).toContain("⚠");
    expect(blockB).not.toContain("⚠");
    expect(blockB).toContain("0.00..0.00");
  });

  it("the axis history is accumulated too", () => {
    const p = pad({ axes: [0, 0, 0, 0] });
    navigator.getGamepads = () => [p];
    const frame = mountPage();
    frame();
    p.axes = [-1, 0, 0, 0];
    frame();
    p.axes = [1, 0, 0, 0];
    frame();
    expect(output()).toContain("-1.00..1.00");   // the stick went through the whole range
  });
});

describe("gamepad-debug: language", () => {
  it("is English by default", () => {
    navigator.getGamepads = () => [pad()];
    const frame = mountPage();
    frame();
    expect(document.querySelector("h1").textContent).toBe("Gamepad diagnostics");
    expect(output()).toContain("buttons: 2");
  });

  it("?lang=pt-BR shows Portuguese, static and dynamic text alike", () => {
    vi.stubGlobal("location", { search: "?lang=pt-BR" });
    navigator.getGamepads = () => [pad()];
    const frame = mountPage();
    expect(document.querySelector("h1").textContent).toBe("Diagnóstico de gamepad");
    expect(document.querySelector("p").innerHTML).toContain("<b>Tire um print disto</b>");
    frame();
    expect(output()).toContain("botões: 2 · eixos: 4");
    expect(output()).toContain("NÃO-standard");
  });
});
