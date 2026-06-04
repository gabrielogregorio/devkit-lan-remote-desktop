"""
Injecao de input no Ubuntu via /dev/uinput (evdev):

  - pointer  : mouse absoluto (ABS_X/Y no espaco da tela toda) + botoes + scroll
  - keyboard : teclado completo
  - gamepad  : Xbox 360 virtual (VID 045e:028e) -> jogos/Steam detectam como real

O cliente manda coordenadas normalizadas (0..1) dentro da regiao capturada;
aqui convertemos para o bounding-box X e escrevemos ABS absoluto.
"""

from evdev import UInput, AbsInfo, ecodes as e

from .keymap import KEYMAP

ABS_MAX = 32767

_PAD_BTN = {
    0: e.BTN_A, 1: e.BTN_B, 2: e.BTN_X, 3: e.BTN_Y,
    4: e.BTN_TL, 5: e.BTN_TR,
    8: e.BTN_SELECT, 9: e.BTN_START,
    10: e.BTN_THUMBL, 11: e.BTN_THUMBR,
    16: e.BTN_MODE,
}
_MOUSE_BTN = {0: e.BTN_LEFT, 1: e.BTN_MIDDLE, 2: e.BTN_RIGHT, 3: e.BTN_SIDE, 4: e.BTN_EXTRA}


def _i16(v: float) -> int:
    return max(-32768, min(32767, int(v)))


class Injector:
    def __init__(self):
        self.pointer = UInput({
            e.EV_KEY: list(_MOUSE_BTN.values()),
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(0, 0, ABS_MAX, 0, 0, 0)),
                (e.ABS_Y, AbsInfo(0, 0, ABS_MAX, 0, 0, 0)),
            ],
            e.EV_REL: [e.REL_WHEEL, e.REL_HWHEEL],
        }, name="remote-link-pointer", vendor=0x1d6b, product=0x0101, version=1)

        self.keyboard = UInput(
            {e.EV_KEY: list(range(1, 249))},
            name="remote-link-keyboard", vendor=0x1d6b, product=0x0102, version=1,
        )

        self.gamepad = UInput({
            e.EV_KEY: list(_PAD_BTN.values()),
            e.EV_ABS: [
                (e.ABS_X,  AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (e.ABS_Y,  AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (e.ABS_RX, AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (e.ABS_RY, AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (e.ABS_Z,  AbsInfo(0, 0, 255, 0, 0, 0)),
                (e.ABS_RZ, AbsInfo(0, 0, 255, 0, 0, 0)),
                (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
                (e.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
            ],
        }, name="Microsoft X-Box 360 pad", vendor=0x045e, product=0x028e, version=0x110)

        self._tw, self._th = 1920, 1080
        self._region = {"x": 0, "y": 0, "w": 1920, "h": 1080}
        self._cx, self._cy = 0.5, 0.5

    def set_geometry(self, total: tuple[int, int], region: dict):
        self._tw, self._th = total
        self._region = region

    # ---------- mouse ----------
    def _write_abs_from_region(self, nx: float, ny: float):
        nx = min(1.0, max(0.0, nx))
        ny = min(1.0, max(0.0, ny))
        self._cx, self._cy = nx, ny
        px = self._region["x"] + nx * self._region["w"]
        py = self._region["y"] + ny * self._region["h"]
        ax = int(px / self._tw * ABS_MAX)
        ay = int(py / self._th * ABS_MAX)
        self.pointer.write(e.EV_ABS, e.ABS_X, max(0, min(ABS_MAX, ax)))
        self.pointer.write(e.EV_ABS, e.ABS_Y, max(0, min(ABS_MAX, ay)))
        self.pointer.syn()

    def move_abs(self, nx: float, ny: float):
        self._write_abs_from_region(nx, ny)

    def move_rel(self, dnx: float, dny: float):
        self._write_abs_from_region(self._cx + dnx, self._cy + dny)

    def mouse_button(self, btn: int, down: bool):
        code = _MOUSE_BTN.get(btn)
        if code is None:
            return
        self.pointer.write(e.EV_KEY, code, 1 if down else 0)
        self.pointer.syn()

    def wheel(self, dx: int, dy: int):
        if dy:
            self.pointer.write(e.EV_REL, e.REL_WHEEL, -1 if dy > 0 else 1)
        if dx:
            self.pointer.write(e.EV_REL, e.REL_HWHEEL, 1 if dx > 0 else -1)
        self.pointer.syn()

    # ---------- teclado ----------
    def key(self, code: str, down: bool):
        kc = KEYMAP.get(code)
        if kc is None:
            return
        self.keyboard.write(e.EV_KEY, kc, 1 if down else 0)
        self.keyboard.syn()

    # ---------- gamepad ----------
    def gamepad_state(self, axes: list, buttons: list, lt=None, rt=None):
        gp = self.gamepad
        if len(axes) >= 4:
            gp.write(e.EV_ABS, e.ABS_X,  _i16(axes[0] * 32767))
            gp.write(e.EV_ABS, e.ABS_Y,  _i16(axes[1] * 32767))
            gp.write(e.EV_ABS, e.ABS_RX, _i16(axes[2] * 32767))
            gp.write(e.EV_ABS, e.ABS_RY, _i16(axes[3] * 32767))

        def bval(i):
            return buttons[i].get("v", 0.0) if i < len(buttons) else 0.0

        def bpr(i):
            return 1 if (i < len(buttons) and buttons[i].get("p")) else 0

        # Gatilhos: o cliente manda lt/rt explicitos (0..1) quando consegue
        # detectar (mapping nao-padrao usa eixos). Cai pros buttons[6]/[7]
        # so se o cliente nao mandou nada.
        lz = lt if lt is not None else bval(6)
        rz = rt if rt is not None else bval(7)
        # round (nao int): replica o valor 0..1 -> 0..255 linear sem enviesar
        # pra baixo. evdev deduplica valores iguais, entao reescrever o mesmo
        # ABS nao gera evento -> sem flicker.
        gp.write(e.EV_ABS, e.ABS_Z,  max(0, min(255, round(lz * 255))))
        gp.write(e.EV_ABS, e.ABS_RZ, max(0, min(255, round(rz * 255))))
        gp.write(e.EV_ABS, e.ABS_HAT0Y, bpr(13) - bpr(12))
        gp.write(e.EV_ABS, e.ABS_HAT0X, bpr(15) - bpr(14))
        for i, code in _PAD_BTN.items():
            gp.write(e.EV_KEY, code, bpr(i))
        gp.syn()

    # ---------- dispatch ----------
    def handle(self, msg: dict):
        t = msg.get("t")
        if t == "m":
            self.move_abs(msg["x"], msg["y"])
        elif t == "mr":
            self.move_rel(msg["dx"], msg["dy"])
        elif t == "md":
            self.mouse_button(msg["b"], True)
        elif t == "mu":
            self.mouse_button(msg["b"], False)
        elif t == "w":
            self.wheel(int(msg.get("x", 0)), int(msg.get("y", 0)))
        elif t == "k":
            self.key(msg["code"], msg["down"])
        elif t == "g":
            self.gamepad_state(
                msg.get("a", []), msg.get("b", []),
                msg.get("lt"), msg.get("rt"),
            )

    def close(self):
        for dev in (self.pointer, self.keyboard, self.gamepad):
            try:
                dev.close()
            except Exception:
                pass
