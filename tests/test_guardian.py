import asyncio
import ctypes
import types
import pytest
from unittest import mock

from server import guardian
from tests.helpers import fake_proc


# ───────────────────────── _norm / índice ─────────────────────────

def test_norm():
    assert guardian._norm("/A/B/Vgc.EXE") == "vgc.exe"
    assert guardian._norm('"x"') == "x"


# ───────────────────────── Match.reason ─────────────────────────

def test_match_reason_anticheat():
    m = guardian.Match("X", "anticheat", "vgc.exe", "42")
    r = m.reason()
    assert "Anticheat" in r
    assert "vgc.exe" in r


def test_match_reason_jogo():
    m = guardian.Match("CS", "jogo", "cs2", "99")
    r = m.reason()
    assert "Jogo online" in r
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


def test_candidates_linux_oserror_por_arquivo(monkeypatch):
    procs = {
        "1": {"comm": None, "exe": "/bin/bash", "cmdline": ["bash"]},
        "2": {"comm": "python3", "exe": None, "cmdline": ["python3"]},
        "3": {"comm": "systemd", "exe": "/lib/systemd/systemd", "cmdline": None},
    }
    fake_proc(monkeypatch, guardian, procs)
    result = list(guardian._candidates_linux())
    assert len(result) == 3


def test_candidates_linux_listdir_falha(monkeypatch):
    def raise_oserror(p):
        raise OSError("no proc")
    monkeypatch.setattr(guardian.os, "listdir", raise_oserror)
    result = list(guardian._candidates_linux())
    assert result == []


def test_candidates_linux_filtra_nao_numerico(monkeypatch):
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


def test_candidates_windows_erro(monkeypatch):
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


def test_candidates_generic_erro(monkeypatch):
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

def test_scan_anticheat_prioridade(monkeypatch):
    # Ambos anticheat e jogo presentes; anticheat deve vencer
    def fake_iter():
        yield "1", {"cs2"}        # jogo
        yield "2", {"vgc.exe"}    # anticheat
    monkeypatch.setattr(guardian, "_iter_processes", fake_iter)
    m = guardian.scan()
    assert m is not None
    assert m.kind == "anticheat"
    assert m.hit == "vgc.exe"


def test_scan_so_jogo(monkeypatch):
    def fake_iter():
        yield "1", {"cs2"}
    monkeypatch.setattr(guardian, "_iter_processes", fake_iter)
    m = guardian.scan()
    assert m is not None
    assert m.kind == "jogo"


def test_scan_limpo(monkeypatch):
    def fake_iter():
        yield "1", {"notepad.exe"}
    monkeypatch.setattr(guardian, "_iter_processes", fake_iter)
    assert guardian.scan() is None


def test_scan_match_real_basename(monkeypatch):
    # rustc/cargo NÃO devem casar com o jogo Rust
    def fake_iter():
        yield "1", {"rustc", "cargo"}
    monkeypatch.setattr(guardian, "_iter_processes", fake_iter)
    assert guardian.scan() is None


# ───────────────────────── _banner ─────────────────────────

def test_banner_preflight():
    m = guardian.Match("X", "anticheat", "vgc.exe", "1")
    b = guardian._banner(m, "preflight")
    assert "INICIALIZACAO BLOQUEADA" in b


def test_banner_runtime():
    m = guardian.Match("X", "anticheat", "vgc.exe", "1")
    b = guardian._banner(m, "runtime")
    assert "ENCERRAMENTO DE EMERGENCIA" in b


# ───────────────────────── _gui_notice ─────────────────────────

class DummyPopen:
    pass


def test_gui_linux_primeiro_falha_segundo_ok(monkeypatch):
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
    guardian._gui_notice(m, "preflight")  # não deve levantar


def test_gui_windows_excecao_cai_no_loop(monkeypatch):
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
    assert "INICIALIZACAO" in err or "remote-link" in err
    assert len(gui_calls) == 1


# ───────────────────────── disabled ─────────────────────────

@pytest.mark.parametrize("val,expected", [
    ("off", True), ("0", True), ("false", True), ("nao", True),
    ("on", False), ("", False),
])
def test_disabled_parametrizado(monkeypatch, val, expected):
    monkeypatch.setenv("RL_GUARDIAN", val)
    assert guardian.disabled() == expected


# ───────────────────────── enforce_preflight ─────────────────────────

def test_preflight_desativado(monkeypatch, capfd):
    monkeypatch.setenv("RL_GUARDIAN", "off")
    result = guardian.enforce_preflight()
    assert result is None
    _, err = capfd.readouterr()
    assert "guardian" in err.lower() or "DESATIVADO" in err or "guardian" in err


def test_preflight_bloqueado(monkeypatch):
    monkeypatch.setenv("RL_GUARDIAN", "on")
    m = guardian.Match("V", "anticheat", "vgc.exe", "1")
    monkeypatch.setattr(guardian, "scan", lambda: m)
    block_calls = []
    monkeypatch.setattr(guardian, "show_block_notice",
                        lambda m2, p: block_calls.append((m2, p)))
    with pytest.raises(SystemExit) as ei:
        guardian.enforce_preflight()
    assert ei.value.code == 3
    assert len(block_calls) == 1


def test_preflight_limpo(monkeypatch):
    monkeypatch.setenv("RL_GUARDIAN", "on")
    monkeypatch.setattr(guardian, "scan", lambda: None)
    result = guardian.enforce_preflight()
    assert result is None


# ───────────────────────── monitor ─────────────────────────

async def test_monitor_desativado(monkeypatch):
    monkeypatch.setenv("RL_GUARDIAN", "off")
    scan_calls = []
    monkeypatch.setattr(guardian, "scan", lambda: scan_calls.append(1) or None)

    async def on_trip(m):
        pass

    await guardian.monitor(on_trip)
    assert len(scan_calls) == 0


async def test_monitor_trip_apos_limpo(monkeypatch):
    monkeypatch.setenv("RL_GUARDIAN", "on")

    results = iter([None, guardian.Match("Y", "jogo", "cs2", "9")])
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


async def test_monitor_intervalo_env_valido(monkeypatch):
    monkeypatch.setenv("RL_GUARDIAN", "on")
    monkeypatch.setenv("RL_GUARDIAN_INTERVAL", "0.5")

    results = iter([None, guardian.Match("Y", "jogo", "cs2", "9")])
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


async def test_monitor_intervalo_env_invalido(monkeypatch):
    monkeypatch.setenv("RL_GUARDIAN", "on")
    monkeypatch.setenv("RL_GUARDIAN_INTERVAL", "abc")

    results = iter([None, guardian.Match("Y", "jogo", "cs2", "9")])
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


# ───────────────────────── branches extras ─────────────────────────

def test_candidates_linux_nomes_vazios(monkeypatch):
    # pid com comm=" " (→ "") + exe/cmdline falhando → names vazio → não yield
    procs = {
        "111": {"comm": " ", "exe": None, "cmdline": None},
        "222": {"comm": "ok_proc", "exe": "/bin/ok", "cmdline": ["ok"]},
    }
    fake_proc(monkeypatch, guardian, procs)
    result = list(guardian._candidates_linux())
    pids = [r[0] for r in result]
    assert "111" not in pids  # nomes vazios → não é gerado
    assert "222" in pids


def test_candidates_windows_csv_vazio(monkeypatch):
    fake = mock.Mock()
    fake.stdout = ""
    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **kw: fake)
    result = list(guardian._candidates_windows())
    assert result == []


def test_candidates_windows_csv_linha_curta(monkeypatch):
    # len(row) < 2: branch 223->222 (volta ao for sem yield)
    fake = mock.Mock()
    fake.stdout = '"onecol"\r\n'
    monkeypatch.setattr(guardian.subprocess, "run", lambda *a, **kw: fake)
    result = list(guardian._candidates_windows())
    assert result == []


def test_scan_dois_jogos_mantem_primeiro(monkeypatch):
    def fake_iter():
        yield "1", {"cs2"}
        yield "2", {"dota2"}
    monkeypatch.setattr(guardian, "_iter_processes", fake_iter)
    m = guardian.scan()
    assert m is not None
    assert m.hit == "cs2"


def test_gui_linux_todos_falham(monkeypatch):
    monkeypatch.setattr(guardian.sys, "platform", "linux")
    monkeypatch.setattr(guardian.subprocess, "Popen",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("all fail")))
    m = guardian.Match("V", "anticheat", "vgc.exe", "1")
    guardian._gui_notice(m, "preflight")  # loop esgota sem return


async def test_monitor_interval_explicito(monkeypatch):
    monkeypatch.setenv("RL_GUARDIAN", "on")
    results = iter([None, guardian.Match("Y", "jogo", "cs2", "9")])
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
    assert "Anticheats:" in out
    assert "Jogos:" in out


def test_main_desativado(monkeypatch, capfd):
    monkeypatch.setenv("RL_GUARDIAN", "off")
    result = guardian._main([])
    assert result == 0


def test_main_bloqueado(monkeypatch):
    monkeypatch.setenv("RL_GUARDIAN", "on")
    m = guardian.Match("V", "anticheat", "vgc.exe", "1")
    monkeypatch.setattr(guardian, "scan", lambda: m)
    monkeypatch.setattr(guardian, "show_block_notice", lambda *a: None)
    result = guardian._main([])
    assert result == 3


def test_main_limpo(monkeypatch):
    monkeypatch.setenv("RL_GUARDIAN", "on")
    monkeypatch.setattr(guardian, "scan", lambda: None)
    result = guardian._main([])
    assert result == 0
