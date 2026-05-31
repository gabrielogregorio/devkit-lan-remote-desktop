import pytest
from server import inject
from tests.helpers import FakeUInput


@pytest.fixture
def inj(monkeypatch):
    FakeUInput.instances.clear()
    monkeypatch.setattr(inject, "UInput", FakeUInput)
    return inject.Injector()


def test_init_cria_tres_devices(inj):
    assert len(FakeUInput.instances) == 3


def test_set_geometry(inj):
    inj.set_geometry((3840, 1080), {"x": 10, "y": 0, "w": 1920, "h": 1080})
    assert inj._tw == 3840
    assert inj._th == 1080
    assert inj._region["x"] == 10
    assert inj._region["w"] == 1920


def test_move_abs_clamp(inj):
    inj.move_abs(-1, 2)
    pointer = inj.pointer
    written_codes = [w[1] for w in pointer.writes]
    assert pointer.syns >= 1
    # ABS_X and ABS_Y should have been written
    from evdev import ecodes as e
    abs_codes = {w[1] for w in pointer.writes}
    assert e.ABS_X in abs_codes
    assert e.ABS_Y in abs_codes
    # values clamped to [0, ABS_MAX]
    for etype, code, val in pointer.writes:
        if code in (e.ABS_X, e.ABS_Y):
            assert 0 <= val <= inject.ABS_MAX


def test_move_rel(inj):
    inj.move_abs(0.5, 0.5)
    writes_before = len(inj.pointer.writes)
    inj.move_rel(0.1, 0.1)
    assert len(inj.pointer.writes) > writes_before


def test_mouse_button_valido_e_invalido(inj):
    from evdev import ecodes as e
    writes_before = len(inj.pointer.writes)
    inj.mouse_button(0, True)
    assert len(inj.pointer.writes) > writes_before
    # invalid button: no-op
    writes_after = len(inj.pointer.writes)
    inj.mouse_button(99, True)
    assert len(inj.pointer.writes) == writes_after


def test_wheel_sinais(inj):
    from evdev import ecodes as e
    # dy > 0
    inj.wheel(0, 1)
    # dy < 0
    inj.wheel(0, -1)
    # dx > 0
    inj.wheel(1, 0)
    # dx < 0
    inj.wheel(-1, 0)
    # both zero — still calls syn
    syns_before = inj.pointer.syns
    inj.wheel(0, 0)
    assert inj.pointer.syns > syns_before
    # check REL_WHEEL and REL_HWHEEL written
    all_codes = {w[1] for w in inj.pointer.writes}
    assert e.REL_WHEEL in all_codes
    assert e.REL_HWHEEL in all_codes


def test_key_valido_e_invalido(inj):
    writes_before = len(inj.keyboard.writes)
    inj.key("KeyA", True)
    assert len(inj.keyboard.writes) > writes_before
    writes_after = len(inj.keyboard.writes)
    inj.key("NaoExiste", True)
    assert len(inj.keyboard.writes) == writes_after


def test_gamepad_axes_completo(inj):
    from evdev import ecodes as e
    axes = [0.5, -0.5, 1.0, -1.0]
    # buttons: indices 0-6 have p=True/v=0.5, indices 7-16 have p=False/v=0.0
    buttons = [{"p": True, "v": 0.5}] * 7 + [{"p": False, "v": 0.0}] * 10
    inj.gamepad_state(axes, buttons)
    gp = inj.gamepad
    assert gp.syns >= 1
    all_codes = {w[1] for w in gp.writes}
    assert e.ABS_X in all_codes
    assert e.ABS_Y in all_codes
    assert e.ABS_RX in all_codes
    assert e.ABS_RY in all_codes
    assert e.ABS_Z in all_codes
    assert e.ABS_RZ in all_codes


def test_gamepad_axes_curto(inj):
    from evdev import ecodes as e
    # len(axes) < 4: não entra no bloco de eixos mas escreve Z/RZ/HAT/botões
    inj.gamepad_state([0.0], [])
    gp = inj.gamepad
    assert gp.syns >= 1
    all_codes = {w[1] for w in gp.writes}
    assert e.ABS_Z in all_codes
    assert e.ABS_RZ in all_codes
    # ABS_X must NOT have been written (axes block skipped)
    assert e.ABS_X not in all_codes


def test_handle_todos_os_t(inj, monkeypatch):
    from evdev import ecodes as e
    handlers = {
        "m": {"t": "m", "x": 0.5, "y": 0.5},
        "mr": {"t": "mr", "dx": 0.01, "dy": 0.01},
        "md": {"t": "md", "b": 0},
        "mu": {"t": "mu", "b": 0},
        "w": {"t": "w", "x": 0, "y": 1},
        "k": {"t": "k", "code": "KeyA", "down": True},
        "g": {"t": "g", "a": [0.0, 0.0, 0.0, 0.0], "b": []},
    }
    for t, msg in handlers.items():
        inj.handle(msg)  # must not raise
    inj.handle({"t": "zzz"})  # no-op, must not raise


def test_close_ok(inj):
    inj.close()
    for dev in (inj.pointer, inj.keyboard, inj.gamepad):
        assert dev.closed


def test_close_swallows(monkeypatch):
    FakeUInput.instances.clear()
    monkeypatch.setattr(inject, "UInput", FakeUInput)
    inj2 = inject.Injector()
    inj2.pointer = FakeUInput(close_raises=True)
    inj2.close()  # não deve levantar
