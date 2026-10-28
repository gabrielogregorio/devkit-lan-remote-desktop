"""
Input injection on Ubuntu through /dev/uinput (evdev):

  - pointer  : absolute mouse (ABS_X/Y over the whole screen space) + buttons + scroll
  - keyboard : full keyboard
  - gamepad  : virtual Xbox 360 (VID 045e:028e) -> games/Steam detect it as real

The client sends normalized coordinates (0..1) inside the captured region;
here we convert them to the X bounding box and write absolute ABS.

NullInjector is the same interface doing nothing: view-only mode (LRD_INPUT=off,
e.g. the e2e suite, where garbage sent to /ws/input must not move the developer's
mouse) and the fallback when /dev/uinput cannot be opened (no permission, CI
runners without the device), so the screen can still be watched.
"""

from evdev import AbsInfo, UInput, UInputError  # noqa: F401 - UInputError is re-exported for main
from evdev import ecodes as e

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


def _axis(v) -> float:
    """Safe -1..1 axis. NaN/inf/wrong type become 0 (center)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:                       # NaN
        return 0.0
    return -1.0 if f < -1 else (1.0 if f > 1 else f)


def _unit(v) -> float:
    """Safe 0..1 trigger. NaN/inf/wrong type become 0 (released)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:
        return 0.0
    return 0.0 if f < 0 else (1.0 if f > 1 else f)


class Injector:
    def __init__(self):
        self.pointer = UInput({
            e.EV_KEY: list(_MOUSE_BTN.values()),
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(0, 0, ABS_MAX, 0, 0, 0)),
                (e.ABS_Y, AbsInfo(0, 0, ABS_MAX, 0, 0, 0)),
            ],
            e.EV_REL: [e.REL_WHEEL, e.REL_HWHEEL],
        }, name="lan-remote-desktop-pointer", vendor=0x1d6b, product=0x0101, version=1)

        self.keyboard = UInput(
            {e.EV_KEY: list(range(1, 249))},
            name="lan-remote-desktop-keyboard", vendor=0x1d6b, product=0x0102, version=1,
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

    # ---------- keyboard ----------
    def key(self, code: str, down: bool):
        kc = KEYMAP.get(code)
        if kc is None:
            return
        self.keyboard.write(e.EV_KEY, kc, 1 if down else 0)
        self.keyboard.syn()

    # ---------- gamepad ----------
    def gamepad_state(self, axes: list, buttons: list, lt=None, rt=None):
        # A malformed frame (wrong type, NaN, missing field) must not write half
        # of the state and abort midway: the rest would go out with the next syn
        # and the game would see a "stuck" button/trigger. So everything here is
        # total - whatever cannot be interpreted becomes neutral (released/center).
        gp = self.gamepad
        if not isinstance(axes, (list, tuple)):
            axes = []
        if not isinstance(buttons, (list, tuple)):
            buttons = []
        if len(axes) >= 4:
            gp.write(e.EV_ABS, e.ABS_X,  _i16(_axis(axes[0]) * 32767))
            gp.write(e.EV_ABS, e.ABS_Y,  _i16(_axis(axes[1]) * 32767))
            gp.write(e.EV_ABS, e.ABS_RX, _i16(_axis(axes[2]) * 32767))
            gp.write(e.EV_ABS, e.ABS_RY, _i16(_axis(axes[3]) * 32767))

        def bval(i):
            b = buttons[i] if i < len(buttons) else None
            return b.get("v", 0.0) if isinstance(b, dict) else 0.0

        def bpr(i):
            b = buttons[i] if i < len(buttons) else None
            return 1 if (isinstance(b, dict) and b.get("p")) else 0

        # Triggers: the client sends explicit lt/rt (0..1) when it can detect
        # them (a non-standard mapping uses axes). Falls back to buttons[6]/[7]
        # only when the client sent nothing.
        lz = _unit(lt if lt is not None else bval(6))
        rz = _unit(rt if rt is not None else bval(7))
        # round (not int): maps 0..1 -> 0..255 linearly without biasing it
        # downwards. evdev deduplicates equal values, so rewriting the same ABS
        # produces no event -> no flicker.
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


class NullInjector:
    """Same interface as Injector, does nothing: no uinput device is created."""

    def set_geometry(self, total: tuple[int, int], region: dict):
        pass

    def handle(self, msg: dict):
        pass

    def close(self):
        pass
