"""
Fuzz determinístico do caminho de input e do saneamento de qualidade.

A ideia não é achar "o" bug e sim garantir INVARIANTES sob entrada hostil - o
canal de input é LAN aberta, qualquer coisa pode chegar nele:

  1. o que sai pro /dev/uinput fica SEMPRE dentro da faixa declarada no device
     (senão o evdev/jogo vê eixo maluco, botão preso, gatilho no fundo);
  2. um frame de gamepad torto vira estado neutro, nunca meio-escrito;
  3. os saneadores de vídeo nunca levantam e nunca saem da faixa.

Sementes fixas: falhou uma vez, falha sempre (dá pra depurar).
"""
import math
import random

import pytest
from evdev import ecodes as e

from server import inject, video
from tests.helpers import FakeUInput

SEEDS = [1, 7, 42, 1337, 2024]

# faixas declaradas no Injector (têm que bater com os AbsInfo de inject.py)
ABS_STICK = (-32768, 32767)
ABS_TRIGGER = (0, 255)
ABS_HAT = (-1, 1)

# valores "hostis" que um cliente (ou um controle bugado) pode mandar
CURSED = [
    None, True, False, 0, 1, -1, 0.5, -0.5, 2.5, -2.5,
    1e9, -1e9, float("inf"), float("-inf"), float("nan"),
    "abc", "", "0.5", [], [1, 2], {}, {"v": 1},
]


@pytest.fixture
def inj(monkeypatch):
    FakeUInput.instances.clear()
    monkeypatch.setattr(inject, "UInput", FakeUInput)
    return inject.Injector()


def _rand_value(rnd: random.Random):
    if rnd.random() < 0.45:
        return rnd.choice(CURSED)
    return rnd.uniform(-3, 3)


def _rand_button(rnd: random.Random):
    if rnd.random() < 0.15:
        return rnd.choice([None, 1, "x", [], {"nada": 1}])
    return {"p": rnd.choice([True, False, 1, 0, None, "sim"]),
            "v": _rand_value(rnd)}


def _rand_gamepad_msg(rnd: random.Random) -> dict:
    n_axes = rnd.choice([0, 1, 3, 4, 6, 12])
    n_btn = rnd.choice([0, 1, 6, 8, 17, 24])
    msg = {"t": "g",
           "a": [_rand_value(rnd) for _ in range(n_axes)],
           "b": [_rand_button(rnd) for _ in range(n_btn)]}
    if rnd.random() < 0.7:
        msg["lt"] = _rand_value(rnd)
    if rnd.random() < 0.7:
        msg["rt"] = _rand_value(rnd)
    if rnd.random() < 0.1:
        msg["a"] = rnd.choice(["nao-e-lista", 5, None, {}])
    if rnd.random() < 0.1:
        msg["b"] = rnd.choice(["nao-e-lista", 5, None, {}])
    return msg


def _check_ranges(dev: FakeUInput):
    """Todo valor escrito cabe na faixa declarada pro seu código ABS/EV_KEY."""
    for etype, code, val in dev.writes:
        assert isinstance(val, int), f"valor não-inteiro em {code}: {val!r}"
        if etype == e.EV_ABS:
            if code in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY):
                lo, hi = ABS_STICK
            elif code in (e.ABS_Z, e.ABS_RZ):
                lo, hi = ABS_TRIGGER
            else:
                lo, hi = ABS_HAT
            assert lo <= val <= hi, f"ABS {code}={val} fora de [{lo},{hi}]"
        elif etype == e.EV_KEY:
            assert val in (0, 1), f"botão {code} com valor {val}"


@pytest.mark.parametrize("seed", SEEDS)
def test_fuzz_gamepad_nunca_sai_da_faixa(inj, seed):
    rnd = random.Random(seed)
    for _ in range(300):
        msg = _rand_gamepad_msg(rnd)
        inj.handle(msg)          # não pode levantar
        _check_ranges(inj.gamepad)


@pytest.mark.parametrize("seed", SEEDS)
def test_fuzz_gamepad_sempre_fecha_com_syn(inj, seed):
    """Estado meio-escrito é o que gera botão/gatilho 'preso' no jogo."""
    rnd = random.Random(seed)
    for i in range(1, 200):
        inj.handle(_rand_gamepad_msg(rnd))
        assert inj.gamepad.syns == i, "frame de gamepad tem que terminar em syn()"


@pytest.mark.parametrize("valor", CURSED)
def test_gatilho_hostil_vira_valor_valido(inj, valor):
    inj.gamepad_state([0, 0, 0, 0], [], lt=valor, rt=valor)
    z = [v for et, c, v in inj.gamepad.writes if c in (e.ABS_Z, e.ABS_RZ)]
    assert z and all(0 <= v <= 255 for v in z)


def test_nan_no_gatilho_e_solto_nao_fundo(inj):
    """NaN precisa virar 0 (solto). Virar 255 seria acelerar sozinho."""
    inj.gamepad_state([0, 0, 0, 0], [], lt=float("nan"), rt=float("nan"))
    z = {c: v for et, c, v in inj.gamepad.writes if c in (e.ABS_Z, e.ABS_RZ)}
    assert z[e.ABS_Z] == 0 and z[e.ABS_RZ] == 0


def test_nan_no_stick_e_centro(inj):
    inj.gamepad_state([float("nan")] * 4, [])
    sticks = [v for et, c, v in inj.gamepad.writes
              if c in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY)]
    assert sticks == [0, 0, 0, 0]


def test_infinito_no_stick_satura_no_extremo(inj):
    inj.gamepad_state([float("inf"), float("-inf"), 5.0, -5.0], [])
    sticks = [v for et, c, v in inj.gamepad.writes
              if c in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY)]
    assert sticks == [32767, -32767, 32767, -32767]


@pytest.mark.parametrize("seed", SEEDS)
def test_fuzz_mensagens_variadas_nao_corrompem_o_ponteiro(inj, seed):
    """Mensagens de mouse/teclado aleatórias: ou aplicam, ou falham limpo."""
    rnd = random.Random(seed)
    tipos = ["m", "mr", "md", "mu", "w", "k", "g", "zzz", None]
    for _ in range(400):
        t = rnd.choice(tipos)
        msg = {"t": t}
        for campo in ("x", "y", "dx", "dy", "b", "code", "down"):
            if rnd.random() < 0.6:
                msg[campo] = _rand_value(rnd)
        try:
            inj.handle(msg)
        except Exception as ex:
            # ws_input captura e segue; o proibido é escrever fora da faixa
            assert isinstance(ex, (TypeError, ValueError, KeyError, OverflowError))
        for etype, code, val in inj.pointer.writes:
            if etype == e.EV_ABS:
                assert 0 <= val <= inject.ABS_MAX


def test_mouse_absoluto_sempre_dentro_da_tela(inj):
    inj.set_geometry((3840, 2160), {"x": 1920, "y": 0, "w": 1920, "h": 1080})
    for nx, ny in [(-5, -5), (0, 0), (0.5, 0.5), (1, 1), (9, 9)]:
        inj.move_abs(nx, ny)
    for etype, code, val in inj.pointer.writes:
        if etype == e.EV_ABS:
            assert 0 <= val <= inject.ABS_MAX


def test_move_rel_acumulado_nao_escapa(inj):
    """Pointer-lock manda deltas sem parar; o acumulado tem que ficar preso."""
    for _ in range(500):
        inj.move_rel(0.05, -0.05)
    for etype, code, val in inj.pointer.writes:
        if etype == e.EV_ABS:
            assert 0 <= val <= inject.ABS_MAX
    assert inj._cx == 1.0 and inj._cy == 0.0


# ─────────────────── saneamento da qualidade de vídeo ───────────────────

@pytest.mark.parametrize("valor", CURSED + ["720p", "-30", "1e5"])
def test_clamp_res_nunca_levanta_e_respeita_a_faixa(valor):
    out = video.clamp_res(valor, 900)
    assert isinstance(out, int)
    assert out == 900 or video.RES_MIN <= out <= video.RES_MAX


@pytest.mark.parametrize("valor", CURSED + ["60fps", "0"])
def test_clamp_fps_nunca_levanta_e_respeita_a_faixa(valor):
    out = video.clamp_fps(valor, 60)
    assert isinstance(out, int)
    assert out == 60 or video.FPS_MIN <= out <= video.FPS_MAX


@pytest.mark.parametrize("valor", CURSED + ["12M; rm -rf /", "$(id)", "--x", "12 M",
                                            "1.55", "0", "999M", "1k"])
def test_clean_bitrate_so_deixa_passar_taxa_utilizavel(valor):
    out = video.clean_bitrate(valor, "7M")
    assert isinstance(out, str)
    assert video._BITRATE_RE.match(out), f"{valor!r} virou bitrate inválido {out!r}"
    kbps = video._kbps(out)
    assert video.BITRATE_MIN_KBPS <= kbps <= video.BITRATE_MAX_KBPS


@pytest.mark.parametrize("bom", ["12M", "3500k", "1200K", "0.5M", "800000", "25m"])
def test_clean_bitrate_aceita_os_formatos_reais(bom):
    assert video.clean_bitrate(bom, "7M") == bom


@pytest.mark.parametrize("seed", SEEDS)
def test_fuzz_saneadores_sempre_geram_comando_de_ffmpeg_valido(seed):
    """Seja qual for o lixo, o cmd montado tem números/bitrate utilizáveis."""
    rnd = random.Random(seed)
    enc = video.ScreenEncoder(lambda k, au: None)
    region = {"name": "s", "w": 1920, "h": 1080, "x": 0, "y": 0}
    for _ in range(200):
        s = video.VideoSettings(
            max_h=video.clamp_res(_rand_value(rnd), 900),
            fps=video.clamp_fps(_rand_value(rnd), 60),
            bitrate=video.clean_bitrate(_rand_value(rnd), "7M"),
        )
        cmd = enc._build_cmd(s, region)
        assert str(s.fps) == cmd[cmd.index("-framerate") + 1]
        assert s.gop >= 2
        bitrate = cmd[cmd.index("-b:v") + 1]
        assert video._BITRATE_RE.match(bitrate)
        assert video.BITRATE_MIN_KBPS <= video._kbps(bitrate) <= video.BITRATE_MAX_KBPS
        assert all(isinstance(p, str) and "\x00" not in p for p in cmd)


def test_kbps_nunca_levanta_com_lixo():
    for v in CURSED:
        assert isinstance(video._kbps(v), int)


def test_gop_minimo_mesmo_com_fps_baixissimo():
    assert video.VideoSettings(max_h=360, fps=1, bitrate="1M").gop == 2
    assert not math.isnan(video.VideoSettings(max_h=360, fps=60, bitrate="1M").gop)
