/**
 * Testes da página de diagnóstico (`web/gamepad-debug.html`).
 *
 * É a ferramenta que se usa pra descobrir COMO um controle aparece antes de
 * mexer em mapeamento - se ela mentir, o remap sai errado e o gatilho volta a
 * dar "double click". O script dela é inline (proposital: a página tem que
 * abrir sozinha), então aqui a gente extrai o <script> e roda num DOM de
 * mentira, com o requestAnimationFrame na mão pra avançar quadro a quadro.
 */
import { describe, it, expect, beforeEach } from "vitest";
import fs from "node:fs";
import path from "node:path";

const HTML = fs.readFileSync(
  path.resolve(__dirname, "../gamepad-debug.html"), "utf8");

/** Monta a página no jsdom e devolve o `render` (1 quadro por chamada). */
function montaPagina() {
  const corpo = HTML.split("<body>")[1].split("<script>")[0];
  const script = HTML.split("<script>")[1].split("</script>")[0];
  document.body.innerHTML = corpo;

  let capturado = null;
  globalThis.requestAnimationFrame = (cb) => { capturado = cb; };
  new Function(script)();          // executa o inline igual o browser faria
  expect(capturado).toBeTypeOf("function");
  return () => capturado();        // 1 quadro
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

const saida = () => document.getElementById("out").innerHTML;

beforeEach(() => {
  navigator.getGamepads = () => [];
});

describe("gamepad-debug: renderização", () => {
  it("sem controle conectado mantém a instrução inicial", () => {
    const quadro = montaPagina();
    quadro();
    expect(saida()).toContain("Conecte um controle");
  });

  it("ignora as posições vazias de getGamepads (slots null)", () => {
    navigator.getGamepads = () => [null, null, pad()];
    const quadro = montaPagina();
    expect(() => quadro()).not.toThrow();
    expect(saida()).toContain("Twin USB Joystick");
  });

  it("mostra id, mapping, e o valor de cada botão e eixo", () => {
    navigator.getGamepads = () => [pad({
      axes: [0.5, -0.25, 0, 0],
      buttons: [btn(0), btn(1), btn(0.5)],
    })];
    const quadro = montaPagina();
    quadro();
    const html = saida();
    expect(html).toContain("Twin USB Joystick");
    expect(html).toContain("btn[2]");
    expect(html).toContain("0.500");     // valor analógico com 3 casas
    expect(html).toContain("axis[1]");
    expect(html).toContain("-0.250");
    expect(html).toContain("botões: 3");
    expect(html).toContain("eixos: 4");
  });

  it("marca mapping vazio como NÃO-standard (é quem precisa de remap)", () => {
    navigator.getGamepads = () => [pad({ mapping: "" })];
    const quadro = montaPagina();
    quadro();
    expect(saida()).toContain("NÃO-standard");
  });

  it("mostra 'standard' quando o navegador reconhece o controle", () => {
    navigator.getGamepads = () => [pad({ id: "Xbox", mapping: "standard" })];
    const quadro = montaPagina();
    quadro();
    expect(saida()).toContain("standard");
    expect(saida()).not.toContain("NÃO-standard");
  });

  it("eixo undefined/null não quebra a página", () => {
    navigator.getGamepads = () => [pad({ axes: [undefined, null, 0, 0] })];
    const quadro = montaPagina();
    expect(() => quadro()).not.toThrow();
    expect(saida()).toContain("0.000");
  });

  it("controle sem botões nem eixos ainda renderiza", () => {
    navigator.getGamepads = () => [pad({ axes: [], buttons: [] })];
    const quadro = montaPagina();
    expect(() => quadro()).not.toThrow();
    expect(saida()).toContain("botões: 0");
  });

  it("continua se reagendando (o loop não pode parar sozinho)", () => {
    navigator.getGamepads = () => [pad()];
    const quadro = montaPagina();
    for (let i = 0; i < 5; i++) expect(() => quadro()).not.toThrow();
  });
});

describe("gamepad-debug: histórico (min/max e oscilação)", () => {
  it("acumula o mínimo e o máximo vistos, não só o valor atual", () => {
    const p = pad({ buttons: [btn(0.2)] });
    navigator.getGamepads = () => [p];
    const quadro = montaPagina();
    quadro();
    p.buttons[0] = btn(0.9);
    quadro();
    p.buttons[0] = btn(0.5);
    quadro();
    expect(saida()).toContain("0.20..0.90");   // faixa toda, com o atual em 0.5
    expect(saida()).toContain("0.500");
  });

  it("gatilho oscilando (o bug do controle PS2) acumula oscilações e alerta", () => {
    const p = pad({ buttons: [btn(0)] });
    navigator.getGamepads = () => [p];
    const quadro = montaPagina();
    // sobe e desce várias vezes: é exatamente o padrão do "double click"
    for (const v of [0, 1, 0, 1, 0, 1, 0, 1]) {
      p.buttons[0] = btn(v);
      quadro();
    }
    expect(saida()).toContain("⚠");            // passou do limiar de 3 inversões
  });

  it("rampa suave (apertar devagar) NÃO conta como oscilação", () => {
    const p = pad({ buttons: [btn(0)] });
    navigator.getGamepads = () => [p];
    const quadro = montaPagina();
    for (let v = 0; v <= 1.0001; v += 0.1) {
      p.buttons[0] = btn(v);
      quadro();
    }
    expect(saida()).not.toContain("⚠");
  });

  it("ruído abaixo de 0.02 é ignorado (não vira oscilação falsa)", () => {
    const p = pad({ buttons: [btn(0.5)] });
    navigator.getGamepads = () => [p];
    const quadro = montaPagina();
    for (const v of [0.5, 0.505, 0.5, 0.51, 0.5, 0.495, 0.5, 0.505]) {
      p.buttons[0] = btn(v);
      quadro();
    }
    expect(saida()).not.toContain("⚠");
  });

  it("cada controle tem histórico próprio (não mistura os índices)", () => {
    const p0 = pad({ index: 0, id: "PAD-A", buttons: [btn(0)] });
    const p1 = pad({ index: 1, id: "PAD-B", buttons: [btn(0)] });
    navigator.getGamepads = () => [p0, p1];
    const quadro = montaPagina();
    for (const v of [0, 1, 0, 1, 0, 1, 0, 1]) {
      p0.buttons[0] = btn(v);       // só o A oscila
      quadro();
    }
    const html = saida();
    const blocoA = html.slice(html.indexOf("PAD-A"), html.indexOf("PAD-B"));
    const blocoB = html.slice(html.indexOf("PAD-B"));
    expect(blocoA).toContain("⚠");
    expect(blocoB).not.toContain("⚠");
    expect(blocoB).toContain("0.00..0.00");
  });

  it("o histórico dos eixos também é acumulado", () => {
    const p = pad({ axes: [0, 0, 0, 0] });
    navigator.getGamepads = () => [p];
    const quadro = montaPagina();
    quadro();
    p.axes = [-1, 0, 0, 0];
    quadro();
    p.axes = [1, 0, 0, 0];
    quadro();
    expect(saida()).toContain("-1.00..1.00");   // stick percorreu a faixa toda
  });
});
