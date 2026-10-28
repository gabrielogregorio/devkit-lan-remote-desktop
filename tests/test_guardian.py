import asyncio
import ctypes
import types
from unittest import mock

import pytest

from server import guardian, i18n
from tests.helpers import fake_proc


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    # The notice text follows the desktop language; pin English so the
    # assertions below do not depend on the developer's LANG.
    monkeypatch.setattr(i18n, "LANG", "en")


# ───────────────────────── _norm / index ─────────────────────────

def test_norm():
    assert guardian._norm("/A/B/Vgc.EXE") == "vgc.exe"
    assert guardian._norm('"x"') == "x"


# ───────────────────────── Match.reason ─────────────────────────

def test_match_reason_anticheat():
    m = guardian.Match("X", "anticheat", "vgc.exe", "42")
    r = m.reason()
    assert "Anti-cheat" in r
    assert "vgc.exe" in r


def test_match_reason_anticheat_pt_br(monkeypatch):
    monkeypatch.setattr(i18n, "LANG", "pt-BR")
    r = guardian.Match("X", "anticheat", "vgc.exe", "42").reason()
    assert r == "Anticheat ativo: X (processo 'vgc.exe', PID 42)."


def test_match_reason_game():
    m = guardian.Match("CS", "game", "cs2", "99")
    r = m.reason()
    assert "Online game" in r
    assert "cs2" in r


# ───────────────────────── _candidates_linux ─────────────────────────

def test_candidates_linux_ok(monkeypatch):
    procs = {
        "123": {"comm": "python3", "exe": "/usr/bin/python3", "cmdline": ["python3", "y.exe"]},
        "self": {},
    }
    fake_proc(monkeypatch, guardian, procs)
    result = list(guardian._candidates_linux())
    assert len(result) == 1
    pid, names = result[0]
    assert pid == "123"
    assert "y.exe" in names
    assert "python3" in names


def test_candidates_linux_oserror_per_file(monkeypatch):
    procs = {
        "1": {"comm": None, "exe": "/bin/bash", "cmdline": ["bash"]},
        "2": {"comm": "python3", "exe": None, "cmdline": ["python3"]},
        "3": {"comm": "systemd", "exe": "/lib/systemd/systemd", "cmdline": None},
    }
    fake_proc(monkeypatch, guardian, procs)
    result = list(guardian._candidates_linux())
    assert len(result) == 3


def test_candidates_linux_listdir_fails(monkeypatch):
    def raise_oserror(p):
        raise OSError("no proc")
    monkeypatch.setattr(guardian.os, "listdir", raise_oserror)
    result = list(guardian._candidates_linux())
    assert result == []


def test_candidates_linux_skips_non_numeric(monkeypatch):
    procs = {
        "self": {},
        "net": {},
        "456": {"comm": "bash", "exe": "/bin/bash", "cmdline": ["bash"]},
    }
    fake_proc(monkeypatch, guardian, procs)
    result = list(guardian._candidates_linux())
    assert len(result) == 1
    assert result[0][0] == "456"


# ───────────────────────── _candidates_windows ─────────────────────────

def test_candidates_windows_ok(monkeypatch):
    fake_result = mock.Mock()
    fake_result.stdout = '"vgc.exe","42","Console","1","10 K"\r\n'
    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **kw: fake_result)
    result = list(guardian._candidates_windows())
    assert len(result) == 1
    pid, names = result[0]
    assert pid == "42"
    assert "vgc.exe" in names


def test_candidates_windows_error(monkeypatch):
    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    result = list(guardian._candidates_windows())
    assert result == []


# ───────────────────────── _candidates_generic ─────────────────────────

def test_candidates_generic_ok(monkeypatch):
    fake_result = mock.Mock()
    fake_result.stdout = "42 cs2\n  \n"
    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **kw: fake_result)
    result = list(guardian._candidates_generic())
    assert len(result) == 1
    pid, names = result[0]
    assert pid == "42"
    assert "cs2" in names


def test_candidates_generic_error(monkeypatch):
    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    result = list(guardian._candidates_generic())
    assert result == []


# ───────────────────────── _iter_processes ─────────────────────────

def test_iter_linux(monkeypatch):
    sentinel = [("1", {"linux_proc"})]
    monkeypatch.setattr(guardian.sys, "platform", "linux")
    monkeypatch.setattr(guardian, "_candidates_linux", lambda: iter(sentinel))
    assert list(guardian._iter_processes()) == sentinel


def test_iter_win32(monkeypatch):
    sentinel = [("2", {"win_proc"})]
    monkeypatch.setattr(guardian.sys, "platform", "win32")
    monkeypatch.setattr(guardian, "_candidates_windows", lambda: iter(sentinel))
    assert list(guardian._iter_processes()) == sentinel


def test_iter_darwin(monkeypatch):
    sentinel = [("3", {"mac_proc"})]
    monkeypatch.setattr(guardian.sys, "platform", "darwin")
    monkeypatch.setattr(guardian, "_candidates_generic", lambda: iter(sentinel))
    assert list(guardian._iter_processes()) == sentinel


# ───────────────────────── scan ─────────────────────────

def test_scan_anticheat_priority(monkeypatch):
    # Both an anti-cheat and a game are present; the anti-cheat must win
    def fake_iter():
        yield "1", {"cs2"}        # game
        yield "2", {"vgc.exe"}    # anticheat
    monkeypatch.setattr(guardian, "_iter_processes", fake_iter)
    m = guardian.scan()
    assert m is not None
    assert m.kind == "anticheat"
    assert m.hit == "vgc.exe"


def test_scan_game_only(monkeypatch):
    def fake_iter():
        yield "1", {"cs2"}
    monkeypatch.setattr(guardian, "_iter_processes", fake_iter)
    m = guardian.scan()
    assert m is not None
    assert m.kind == "game"


def test_scan_clean(monkeypatch):
    def fake_iter():
        yield "1", {"notepad.exe"}
    monkeypatch.setattr(guardian, "_iter_processes", fake_iter)
    assert guardian.scan() is None


def test_scan_match_real_basename(monkeypatch):
    # rustc/cargo must NOT match the game Rust
    def fake_iter():
        yield "1", {"rustc", "cargo"}
    monkeypatch.setattr(guardian, "_iter_processes", fake_iter)
    assert guardian.scan() is None


# ───────────────────────── _banner ─────────────────────────

def test_banner_preflight():
    m = guardian.Match("X", "anticheat", "vgc.exe", "1")
    b = guardian._banner(m, "preflight")
    assert "STARTUP BLOCKED" in b
    assert "LAN Remote Desktop" in b


def test_banner_runtime():
    m = guardian.Match("X", "anticheat", "vgc.exe", "1")
    b = guardian._banner(m, "runtime")
    assert "EMERGENCY SHUTDOWN" in b


# ───────────────────────── _gui_notice ─────────────────────────

class DummyPopen:
    pass


def test_gui_linux_first_fails_second_ok(monkeypatch):
    monkeypatch.setattr(guardian.sys, "platform", "linux")
    call_count = [0]

    def fake_popen(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("first fails")
        return DummyPopen()

    monkeypatch.setattr(guardian.subprocess, "Popen", fake_popen)
    m = guardian.Match("V", "anticheat", "vgc.exe", "1")
    guardian._gui_notice(m, "preflight")
    assert call_count[0] == 2


def test_gui_windows(monkeypatch):
    monkeypatch.setattr(guardian.sys, "platform", "win32")
    fake = types.SimpleNamespace(user32=types.SimpleNamespace(
        MessageBoxW=lambda *a: 0))
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)
    m = guardian.Match("V", "anticheat", "vgc.exe", "1")
    guardian._gui_notice(m, "preflight")  # must not raise


def test_gui_windows_exception_falls_back_to_loop(monkeypatch):
    monkeypatch.setattr(guardian.sys, "platform", "win32")

    def raise_msgbox(*a):
        raise RuntimeError("oops")

    fake = types.SimpleNamespace(user32=types.SimpleNamespace(
        MessageBoxW=raise_msgbox))
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)

    popen_called = []

    def fake_popen(*a, **kw):
        popen_called.append(True)
        return DummyPopen()

    monkeypatch.setattr(guardian.subprocess, "Popen", fake_popen)
    m = guardian.Match("V", "anticheat", "vgc.exe", "1")
    guardian._gui_notice(m, "preflight")
    assert len(popen_called) > 0


# ───────────────────────── show_block_notice ─────────────────────────

def test_show_block_notice(monkeypatch, capfd):
    gui_calls = []
    monkeypatch.setattr(guardian, "_gui_notice", lambda m, p: gui_calls.append((m, p)))
    m = guardian.Match("X", "anticheat", "vgc.exe", "1")
    guardian.show_block_notice(m, "preflight")
    _, err = capfd.readouterr()
    assert "STARTUP BLOCKED" in err
    assert len(gui_calls) == 1


# ───────────────────────── disabled ─────────────────────────

@pytest.mark.parametrize("val,expected", [
    ("off", True), ("0", True), ("false", True), ("no", True),
    ("disable", True), ("disabled", True), ("nao", False),
    ("on", False), ("", False),
])
def test_disabled_parametrized(monkeypatch, val, expected):
    monkeypatch.setenv("LRD_GUARDIAN", val)
    assert guardian.disabled() == expected


# ───────────────────────── enforce_preflight ─────────────────────────

def test_preflight_disabled(monkeypatch, capfd):
    monkeypatch.setenv("LRD_GUARDIAN", "off")
    result = guardian.enforce_preflight()
    assert result is None
    _, err = capfd.readouterr()
    assert "guardian DISABLED (LRD_GUARDIAN=off)" in err


def test_preflight_blocked(monkeypatch):
    monkeypatch.setenv("LRD_GUARDIAN", "on")
    m = guardian.Match("V", "anticheat", "vgc.exe", "1")
    monkeypatch.setattr(guardian, "scan", lambda: m)
    block_calls = []
    monkeypatch.setattr(guardian, "show_block_notice",
                        lambda m2, p: block_calls.append((m2, p)))
    with pytest.raises(SystemExit) as ei:
        guardian.enforce_preflight()
    assert ei.value.code == 3
    assert len(block_calls) == 1


def test_preflight_clean(monkeypatch):
    monkeypatch.setenv("LRD_GUARDIAN", "on")
    monkeypatch.setattr(guardian, "scan", lambda: None)
    result = guardian.enforce_preflight()
    assert result is None


# ───────────────────────── monitor ─────────────────────────

async def test_monitor_disabled(monkeypatch):
    monkeypatch.setenv("LRD_GUARDIAN", "off")
    scan_calls = []
    monkeypatch.setattr(guardian, "scan", lambda: scan_calls.append(1) or None)

    async def on_trip(m):
        pass

    await guardian.monitor(on_trip)
    assert len(scan_calls) == 0


async def test_monitor_trip_after_clean(monkeypatch):
    monkeypatch.setenv("LRD_GUARDIAN", "on")

    results = iter([None, guardian.Match("Y", "game", "cs2", "9")])
    monkeypatch.setattr(guardian, "scan", lambda: next(results))

    block_calls = []
    monkeypatch.setattr(guardian, "show_block_notice",
                        lambda m, p: block_calls.append((m, p)))

    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    trip_calls = []

    async def on_trip(m):
        trip_calls.append(m)

    await guardian.monitor(on_trip)

    assert len(sleeps) == 1
    assert sleeps[0] == 2.0
    assert len(block_calls) == 1
    assert len(trip_calls) == 1
    assert trip_calls[0].hit == "cs2"


async def test_monitor_interval_env_valid(monkeypatch):
    monkeypatch.setenv("LRD_GUARDIAN", "on")
    monkeypatch.setenv("LRD_GUARDIAN_INTERVAL", "0.5")

    results = iter([None, guardian.Match("Y", "game", "cs2", "9")])
    monkeypatch.setattr(guardian, "scan", lambda: next(results))
    monkeypatch.setattr(guardian, "show_block_notice", lambda *a: None)

    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def on_trip(m):
        pass

    await guardian.monitor(on_trip)
    assert len(sleeps) == 1
    assert sleeps[0] == 0.5


async def test_monitor_interval_env_invalid(monkeypatch):
    monkeypatch.setenv("LRD_GUARDIAN", "on")
    monkeypatch.setenv("LRD_GUARDIAN_INTERVAL", "abc")

    results = iter([None, guardian.Match("Y", "game", "cs2", "9")])
    monkeypatch.setattr(guardian, "scan", lambda: next(results))
    monkeypatch.setattr(guardian, "show_block_notice", lambda *a: None)

    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def on_trip(m):
        pass

    await guardian.monitor(on_trip)
    assert len(sleeps) == 1
    assert sleeps[0] == 2.0


# ───────────────────────── extra branches ─────────────────────────

def test_candidates_linux_empty_names(monkeypatch):
    # pid with comm=" " (→ "") + exe/cmdline failing → empty names → no yield
    procs = {
        "111": {"comm": " ", "exe": None, "cmdline": None},
        "222": {"comm": "ok_proc", "exe": "/bin/ok", "cmdline": ["ok"]},
    }
    fake_proc(monkeypatch, guardian, procs)
    result = list(guardian._candidates_linux())
    pids = [r[0] for r in result]
    assert "111" not in pids  # empty names → not yielded
    assert "222" in pids


def test_candidates_windows_empty_csv(monkeypatch):
    fake = mock.Mock()
    fake.stdout = ""
    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **kw: fake)
    result = list(guardian._candidates_windows())
    assert result == []


def test_candidates_windows_short_csv_row(monkeypatch):
    # len(row) < 2: the branch that goes back to the for without yielding
    fake = mock.Mock()
    fake.stdout = '"onecol"\r\n'
    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **kw: fake)
    result = list(guardian._candidates_windows())
    assert result == []


def test_scan_two_games_keeps_first(monkeypatch):
    def fake_iter():
        yield "1", {"cs2"}
        yield "2", {"dota2"}
    monkeypatch.setattr(guardian, "_iter_processes", fake_iter)
    m = guardian.scan()
    assert m is not None
    assert m.hit == "cs2"


def test_gui_linux_all_fail(monkeypatch):
    monkeypatch.setattr(guardian.sys, "platform", "linux")
    monkeypatch.setattr(guardian.subprocess, "Popen",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("all fail")))
    m = guardian.Match("V", "anticheat", "vgc.exe", "1")
    guardian._gui_notice(m, "preflight")  # the loop runs out without returning


async def test_monitor_explicit_interval(monkeypatch):
    monkeypatch.setenv("LRD_GUARDIAN", "on")
    results = iter([None, guardian.Match("Y", "game", "cs2", "9")])
    monkeypatch.setattr(guardian, "scan", lambda: next(results))
    monkeypatch.setattr(guardian, "show_block_notice", lambda *a: None)

    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def on_trip(m):
        pass

    await guardian.monitor(on_trip, interval=0.5)
    assert sleeps[0] == 0.5


# ───────────────────────── _main (CLI) ─────────────────────────

def test_main_list(capsys):
    result = guardian._main(["--list"])
    assert result == 0
    out = capsys.readouterr().out
    assert "# Anti-cheats:" in out
    assert "# Games:" in out


def test_main_disabled(monkeypatch, capfd):
    monkeypatch.setenv("LRD_GUARDIAN", "off")
    result = guardian._main([])
    assert result == 0


def test_main_blocked(monkeypatch):
    monkeypatch.setenv("LRD_GUARDIAN", "on")
    m = guardian.Match("V", "anticheat", "vgc.exe", "1")
    monkeypatch.setattr(guardian, "scan", lambda: m)
    monkeypatch.setattr(guardian, "show_block_notice", lambda *a: None)
    result = guardian._main([])
    assert result == 3


def test_main_clean(monkeypatch):
    monkeypatch.setenv("LRD_GUARDIAN", "on")
    monkeypatch.setattr(guardian, "scan", lambda: None)
    result = guardian._main([])
    assert result == 0


def test_gui_dialog_text_runtime_pt_br(monkeypatch):
    monkeypatch.setattr(i18n, "LANG", "pt-BR")
    monkeypatch.setattr(guardian.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(guardian.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    guardian._gui_notice(guardian.Match("V", "anticheat", "vgc.exe", "1"), "runtime")
    title, body = calls[0][4], calls[0][6]
    assert title == "LAN Remote Desktop bloqueado - risco de banimento"
    assert "foi encerrado." in body
    assert "Feche o jogo/anticheat e tente novamente." in body
