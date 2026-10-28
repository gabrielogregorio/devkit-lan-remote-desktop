"""
Deterministic fuzzing of the input path and of the quality sanitizers.

The point is not to find "the" bug but to guarantee INVARIANTS under hostile
input - the input channel is an open LAN, anything can arrive on it:

  1. what goes out to /dev/uinput ALWAYS stays inside the range declared on the
     device (otherwise evdev/the game sees a crazy axis, a stuck button, a
     trigger floored);
  2. a broken gamepad frame becomes a neutral state, never half-written;
  3. the video sanitizers never raise and never leave their range.

Fixed seeds: if it failed once, it fails every time (so it can be debugged).
"""
import math
import random

import pytest
from evdev import ecodes as e

from server import inject, video
from tests.helpers import FakeUInput

SEEDS = [1, 7, 42, 1337, 2024]

# ranges declared in the Injector (they must match the AbsInfo in inject.py)
ABS_STICK = (-32768, 32767)
ABS_TRIGGER = (0, 255)
ABS_HAT = (-1, 1)

# "hostile" values a client (or a buggy controller) may send
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
        return rnd.choice([None, 1, "x", [], {"nothing": 1}])
    return {"p": rnd.choice([True, False, 1, 0, None, "yes"]),
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
        msg["a"] = rnd.choice(["not-a-list", 5, None, {}])
    if rnd.random() < 0.1:
        msg["b"] = rnd.choice(["not-a-list", 5, None, {}])
    return msg


def _check_ranges(dev: FakeUInput):
    """Every written value fits the range declared for its ABS/EV_KEY code."""
    for etype, code, val in dev.writes:
        assert isinstance(val, int), f"non-integer value at {code}: {val!r}"
        if etype == e.EV_ABS:
            if code in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY):
                lo, hi = ABS_STICK
            elif code in (e.ABS_Z, e.ABS_RZ):
                lo, hi = ABS_TRIGGER
            else:
                lo, hi = ABS_HAT
            assert lo <= val <= hi, f"ABS {code}={val} outside [{lo},{hi}]"
        elif etype == e.EV_KEY:
            assert val in (0, 1), f"button {code} with value {val}"


@pytest.mark.parametrize("seed", SEEDS)
def test_fuzz_gamepad_never_leaves_the_range(inj, seed):
    rnd = random.Random(seed)
    for _ in range(300):
        msg = _rand_gamepad_msg(rnd)
        inj.handle(msg)          # must not raise
        _check_ranges(inj.gamepad)


@pytest.mark.parametrize("seed", SEEDS)
def test_fuzz_gamepad_always_ends_with_syn(inj, seed):
    """A half-written state is what leaves a button/trigger 'stuck' in the game."""
    rnd = random.Random(seed)
    for i in range(1, 200):
        inj.handle(_rand_gamepad_msg(rnd))
        assert inj.gamepad.syns == i, "a gamepad frame must end with syn()"


@pytest.mark.parametrize("value", CURSED)
def test_hostile_trigger_becomes_a_valid_value(inj, value):
    inj.gamepad_state([0, 0, 0, 0], [], lt=value, rt=value)
    z = [v for et, c, v in inj.gamepad.writes if c in (e.ABS_Z, e.ABS_RZ)]
    assert z and all(0 <= v <= 255 for v in z)


def test_nan_trigger_is_released_not_floored(inj):
    """NaN must become 0 (released). Becoming 255 would accelerate on its own."""
    inj.gamepad_state([0, 0, 0, 0], [], lt=float("nan"), rt=float("nan"))
    z = {c: v for et, c, v in inj.gamepad.writes if c in (e.ABS_Z, e.ABS_RZ)}
    assert z[e.ABS_Z] == 0 and z[e.ABS_RZ] == 0


def test_nan_stick_is_centered(inj):
    inj.gamepad_state([float("nan")] * 4, [])
    sticks = [v for et, c, v in inj.gamepad.writes
              if c in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY)]
    assert sticks == [0, 0, 0, 0]


def test_infinite_stick_saturates_at_the_edge(inj):
    inj.gamepad_state([float("inf"), float("-inf"), 5.0, -5.0], [])
    sticks = [v for et, c, v in inj.gamepad.writes
              if c in (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY)]
    assert sticks == [32767, -32767, 32767, -32767]


@pytest.mark.parametrize("seed", SEEDS)
def test_fuzz_assorted_messages_do_not_corrupt_the_pointer(inj, seed):
    """Random mouse/keyboard messages: they either apply or fail cleanly."""
    rnd = random.Random(seed)
    kinds = ["m", "mr", "md", "mu", "w", "k", "g", "zzz", None]
    for _ in range(400):
        t = rnd.choice(kinds)
        msg = {"t": t}
        for field in ("x", "y", "dx", "dy", "b", "code", "down"):
            if rnd.random() < 0.6:
                msg[field] = _rand_value(rnd)
        try:
            inj.handle(msg)
        except Exception as ex:
            # ws_input catches it and moves on; what is forbidden is writing out of range
            assert isinstance(ex, (TypeError, ValueError, KeyError, OverflowError))
        for etype, _code, val in inj.pointer.writes:
            if etype == e.EV_ABS:
                assert 0 <= val <= inject.ABS_MAX


def test_absolute_mouse_always_inside_the_screen(inj):
    inj.set_geometry((3840, 2160), {"x": 1920, "y": 0, "w": 1920, "h": 1080})
    for nx, ny in [(-5, -5), (0, 0), (0.5, 0.5), (1, 1), (9, 9)]:
        inj.move_abs(nx, ny)
    for etype, _code, val in inj.pointer.writes:
        if etype == e.EV_ABS:
            assert 0 <= val <= inject.ABS_MAX


def test_accumulated_move_rel_does_not_escape(inj):
    """Pointer lock sends deltas nonstop; the accumulated position must stay clamped."""
    for _ in range(500):
        inj.move_rel(0.05, -0.05)
    for etype, _code, val in inj.pointer.writes:
        if etype == e.EV_ABS:
            assert 0 <= val <= inject.ABS_MAX
    assert inj._cx == 1.0 and inj._cy == 0.0


# ─────────────────── video quality sanitizing ───────────────────

@pytest.mark.parametrize("value", CURSED + ["720p", "-30", "1e5"])
def test_clamp_res_never_raises_and_respects_the_range(value):
    out = video.clamp_res(value, 900)
    assert isinstance(out, int)
    assert out == 900 or video.RES_MIN <= out <= video.RES_MAX


@pytest.mark.parametrize("value", CURSED + ["60fps", "0"])
def test_clamp_fps_never_raises_and_respects_the_range(value):
    out = video.clamp_fps(value, 60)
    assert isinstance(out, int)
    assert out == 60 or video.FPS_MIN <= out <= video.FPS_MAX


@pytest.mark.parametrize("value", CURSED + ["12M; rm -rf /", "$(id)", "--x", "12 M",
                                            "1.55", "0", "999M", "1k"])
def test_clean_bitrate_only_lets_usable_rates_through(value):
    out = video.clean_bitrate(value, "7M")
    assert isinstance(out, str)
    assert video._BITRATE_RE.match(out), f"{value!r} became an invalid bitrate {out!r}"
    kbps = video._kbps(out)
    assert video.BITRATE_MIN_KBPS <= kbps <= video.BITRATE_MAX_KBPS


@pytest.mark.parametrize("good", ["12M", "3500k", "1200K", "0.5M", "800000", "25m"])
def test_clean_bitrate_accepts_the_real_formats(good):
    assert video.clean_bitrate(good, "7M") == good


@pytest.mark.parametrize("seed", SEEDS)
def test_fuzz_sanitizers_always_build_a_valid_ffmpeg_command(seed):
    """Whatever the garbage, the built cmd has usable numbers/bitrate."""
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


def test_kbps_never_raises_on_garbage():
    for v in CURSED:
        assert isinstance(video._kbps(v), int)


def test_gop_minimum_even_with_very_low_fps():
    assert video.VideoSettings(max_h=360, fps=1, bitrate="1M").gop == 2
    assert not math.isnan(video.VideoSettings(max_h=360, fps=60, bitrate="1M").gop)
