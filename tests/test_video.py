import asyncio
import pytest
from unittest import mock
from server import video
from tests.helpers import FakeProcess, FakeStream, make_create_subprocess


# ───────────────────────── detect_encoder ─────────────────────────

def test_detect_forcado(monkeypatch):
    monkeypatch.setenv("RL_ENCODER", "vaapi")
    assert video.detect_encoder() == "vaapi"


def test_detect_excecao(monkeypatch):
    monkeypatch.delenv("RL_ENCODER", raising=False)

    def raise_exc(*a, **kw):
        raise OSError("no ffmpeg")

    monkeypatch.setattr(video.subprocess, "run", raise_exc)
    monkeypatch.setattr(video.shutil, "which", lambda x: None)
    monkeypatch.setattr(video.os.path, "exists", lambda p: False)
    assert video.detect_encoder() == "x264"


def test_detect_nvenc(monkeypatch):
    monkeypatch.delenv("RL_ENCODER", raising=False)
    fake = type("R", (), {"stdout": "h264_nvenc h264_vaapi"})()
    monkeypatch.setattr(video.subprocess, "run", lambda *a, **kw: fake)
    monkeypatch.setattr(video.shutil, "which", lambda x: "/usr/bin/nvidia-smi")
    assert video.detect_encoder() == "nvenc"


def test_detect_vaapi(monkeypatch):
    monkeypatch.delenv("RL_ENCODER", raising=False)
    fake = type("R", (), {"stdout": "h264_vaapi"})()
    monkeypatch.setattr(video.subprocess, "run", lambda *a, **kw: fake)
    monkeypatch.setattr(video.shutil, "which", lambda x: None)
    monkeypatch.setattr(video.os.path, "exists", lambda p: True)
    assert video.detect_encoder() == "vaapi"


def test_detect_x264(monkeypatch):
    monkeypatch.delenv("RL_ENCODER", raising=False)
    fake = type("R", (), {"stdout": ""})()
    monkeypatch.setattr(video.subprocess, "run", lambda *a, **kw: fake)
    monkeypatch.setattr(video.shutil, "which", lambda x: None)
    monkeypatch.setattr(video.os.path, "exists", lambda p: False)
    assert video.detect_encoder() == "x264"


# ───────────────────────── Preset.gop ─────────────────────────

def test_preset_gop():
    assert video.PRESETS["high"].gop == 60


# ───────────────────────── _x_env ─────────────────────────

def test_x_env_com_xauth(monkeypatch):
    monkeypatch.setenv("RL_XAUTHORITY", "/tmp/xauth")
    env = video._x_env()
    assert env["XAUTHORITY"] == "/tmp/xauth"
    assert "DISPLAY" in env


def test_x_env_sem_xauth(monkeypatch):
    monkeypatch.delenv("RL_XAUTHORITY", raising=False)
    env = video._x_env()
    assert "XAUTHORITY" not in env or env.get("XAUTHORITY") != "/tmp/xauth"
    assert "DISPLAY" in env


# ───────────────────────── get_monitors ─────────────────────────

def test_get_monitors_parse(monkeypatch):
    xrandr_output = (
        "Monitors: 2\n"
        " 0: +*HDMI-0 1920/600x1080/340+0+0  HDMI-0\n"
        " 1: +DP-1 1920/600x1080/340+1920+0  DP-1\n"
    )
    fake = type("R", (), {"stdout": xrandr_output})()
    monkeypatch.setattr(video.subprocess, "run", lambda *a, **kw: fake)
    mons = video.get_monitors()
    assert len(mons) == 2
    assert mons[0]["primary"] is True
    assert mons[1]["primary"] is False
    assert mons[0]["w"] == 1920
    assert mons[0]["x"] == 0
    assert mons[1]["x"] == 1920


def test_get_monitors_excecao(monkeypatch):
    monkeypatch.setattr(video.subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    assert video.get_monitors() == []


# ───────────────────────── virtual_screen ─────────────────────────

def test_virtual_screen_com_mons(monkeypatch):
    mons = [
        {"x": 0, "y": 0, "w": 1920, "h": 1080},
        {"x": 1920, "y": 0, "w": 1920, "h": 1080},
    ]
    monkeypatch.setattr(video, "get_monitors", lambda: mons)
    assert video.virtual_screen() == (3840, 1080)


def test_virtual_screen_vazio(monkeypatch):
    monkeypatch.setattr(video, "get_monitors", lambda: [])
    assert video.virtual_screen() == (1920, 1080)


# ───────────────────────── pick_region ─────────────────────────

def test_pick_region_vazio(monkeypatch):
    monkeypatch.setattr(video, "get_monitors", lambda: [])
    r = video.pick_region(None)
    assert r["name"] == "screen"


def test_pick_region_por_indice(monkeypatch):
    mons = [
        {"index": 0, "name": "HDMI-0", "primary": True, "w": 1920, "h": 1080, "x": 0, "y": 0},
        {"index": 1, "name": "DP-1", "primary": False, "w": 1920, "h": 1080, "x": 1920, "y": 0},
    ]
    monkeypatch.setattr(video, "get_monitors", lambda: mons)
    r = video.pick_region(1)
    assert r["name"] == "DP-1"


def test_pick_region_indice_inexistente_cai_no_primario(monkeypatch):
    mons = [
        {"index": 0, "name": "HDMI-0", "primary": True, "w": 1920, "h": 1080, "x": 0, "y": 0},
    ]
    monkeypatch.setattr(video, "get_monitors", lambda: mons)
    r = video.pick_region(99)
    assert r["name"] == "HDMI-0"


def test_pick_region_primario(monkeypatch):
    mons = [
        {"index": 0, "name": "HDMI-0", "primary": True, "w": 1920, "h": 1080, "x": 0, "y": 0},
        {"index": 1, "name": "DP-1", "primary": False, "w": 1920, "h": 1080, "x": 1920, "y": 0},
    ]
    monkeypatch.setattr(video, "get_monitors", lambda: mons)
    r = video.pick_region(None)
    assert r["name"] == "HDMI-0"


def test_pick_region_sem_primario(monkeypatch):
    mons = [
        {"index": 0, "name": "HDMI-0", "primary": False, "w": 1920, "h": 1080, "x": 0, "y": 0},
        {"index": 1, "name": "DP-1", "primary": False, "w": 1920, "h": 1080, "x": 1920, "y": 0},
    ]
    monkeypatch.setattr(video, "get_monitors", lambda: mons)
    r = video.pick_region(None)
    assert r["name"] == "HDMI-0"  # mons[0]


# ───────────────────────── _is_keyframe ─────────────────────────

def test_keyframe_idr():
    au = b"\x00\x00\x01\x05" + b"\xff" * 10
    assert video._is_keyframe(au) is True


def test_keyframe_sps():
    au = b"\x00\x00\x01\x67" + b"\xff" * 10
    assert video._is_keyframe(au) is True


def test_keyframe_nao():
    au = b"\x00\x00\x01\x41" + b"\xff" * 10
    assert video._is_keyframe(au) is False


def test_keyframe_curto():
    au = b"\x00\x00\x01"  # sem byte após o start code
    assert video._is_keyframe(au) is False


# ───────────────────────── ScreenEncoder._build_cmd ─────────────────────────

def _make_enc():
    async def noop(*a):
        pass
    return video.ScreenEncoder(noop)


def test_build_nvenc_scale(monkeypatch):
    monkeypatch.setattr(video, "ENCODER", "nvenc")
    enc = _make_enc()
    region = {"x": 0, "y": 0, "w": 1920, "h": 2160, "name": "s"}
    cmd = enc._build_cmd(video.PRESETS["high"], region)
    assert "h264_nvenc" in cmd
    assert "-vf" in cmd
    # scale is present when h > max_h
    scale_idx = cmd.index("-vf")
    assert "scale" in cmd[scale_idx + 1]


def test_build_nvenc_sem_scale(monkeypatch):
    monkeypatch.setattr(video, "ENCODER", "nvenc")
    enc = _make_enc()
    region = {"x": 0, "y": 0, "w": 1920, "h": 720, "name": "s"}
    cmd = enc._build_cmd(video.PRESETS["high"], region)
    assert "h264_nvenc" in cmd
    # no scale when h <= max_h
    assert "-vf" not in cmd


def test_build_vaapi_lowpower(monkeypatch):
    monkeypatch.setattr(video, "ENCODER", "vaapi")
    enc = _make_enc()
    enc._low_power = True
    region = {"x": 0, "y": 0, "w": 1920, "h": 2160, "name": "s"}
    cmd = enc._build_cmd(video.PRESETS["high"], region)
    assert "h264_vaapi" in cmd
    assert "-low_power" in cmd
    assert "scale_vaapi" in " ".join(cmd)


def test_build_vaapi_sem_lowpower(monkeypatch):
    monkeypatch.setattr(video, "ENCODER", "vaapi")
    enc = _make_enc()
    enc._low_power = False
    region = {"x": 0, "y": 0, "w": 1920, "h": 720, "name": "s"}
    cmd = enc._build_cmd(video.PRESETS["high"], region)
    assert "h264_vaapi" in cmd
    assert "-low_power" not in cmd
    assert "scale_vaapi" not in " ".join(cmd)


def test_build_x264_scale(monkeypatch):
    monkeypatch.setattr(video, "ENCODER", "x264")
    enc = _make_enc()
    region = {"x": 0, "y": 0, "w": 1920, "h": 2160, "name": "s"}
    cmd = enc._build_cmd(video.PRESETS["high"], region)
    assert "libx264" in cmd
    assert "-vf" in cmd


def test_build_x264_sem_scale(monkeypatch):
    monkeypatch.setattr(video, "ENCODER", "x264")
    enc = _make_enc()
    region = {"x": 0, "y": 0, "w": 1920, "h": 720, "name": "s"}
    cmd = enc._build_cmd(video.PRESETS["high"], region)
    assert "libx264" in cmd
    assert "-vf" not in cmd


# ───────────────────────── ScreenEncoder._read_stream ─────────────────────────

async def test_read_stream_fatia_aus():
    AUD = b"\x00\x00\x01\x09"
    data = AUD + b"\x00\x00\x01\x05frame1" + AUD + b"\x00\x00\x01\x01frame2" + AUD

    received = []

    async def on_au(is_key, au):
        received.append((is_key, au))

    enc = video.ScreenEncoder(on_au)
    enc.proc = FakeProcess(stdout_chunks=[data])
    enc._stop = False
    await enc._read_stream()

    assert len(received) == 2
    assert received[0][0] is True  # keyframe (has NAL 5)
    assert received[1][0] is False


async def test_read_stream_au_incompleto():
    AUD = b"\x00\x00\x01\x09"
    # single AUD start but no second AUD to delimit the AU
    data = AUD + b"\x00\x00\x01\x05parcial"

    received = []

    async def on_au(is_key, au):
        received.append((is_key, au))

    enc = video.ScreenEncoder(on_au)
    enc.proc = FakeProcess(stdout_chunks=[data])
    enc._stop = False
    await enc._read_stream()

    assert len(received) == 0


# ───────────────────────── _drain_stderr ─────────────────────────

async def test_drain_stderr(capsys):
    enc = _make_enc()
    enc.proc = FakeProcess(stderr_lines=[b"erro x\n", b"  \n"])
    await enc._drain_stderr()
    out = capsys.readouterr().out
    assert "erro x" in out


async def test_drain_stderr_sem_proc():
    enc = _make_enc()
    enc.proc = None
    await enc._drain_stderr()  # não deve levantar


# ───────────────────────── stop ─────────────────────────

async def test_stop_sem_proc():
    enc = _make_enc()
    enc.proc = None
    enc._task = None
    await enc.stop()  # não deve levantar


async def test_stop_termina():
    enc = _make_enc()
    proc = FakeProcess()
    proc.returncode = None
    enc.proc = proc

    async def noop():
        pass

    task = asyncio.create_task(noop())
    await task
    enc._task = task

    await enc.stop()
    assert proc.terminated


async def test_stop_terminate_falha_mata():
    enc = _make_enc()
    proc = FakeProcess(terminate_raises=True)
    proc.returncode = None
    enc.proc = proc
    enc._task = None
    await enc.stop()
    assert proc.killed


# ───────────────────────── _supervise ─────────────────────────

async def test_supervise_x264_limite_tentativas(monkeypatch):
    monkeypatch.setattr(video, "ENCODER", "x264")
    monkeypatch.setattr(video, "get_monitors", lambda: [])

    fake_cse = make_create_subprocess([FakeProcess()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_cse)

    async def fake_sleep(d):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    enc = _make_enc()
    preset = video.PRESETS["high"]
    region = {"x": 0, "y": 0, "w": 1920, "h": 1080, "name": "s"}

    enc.start(preset, region)
    await enc._task

    assert fake_cse.calls["n"] >= 6


async def test_supervise_vaapi_downgrade(monkeypatch):
    monkeypatch.setattr(video, "ENCODER", "vaapi")
    monkeypatch.setattr(video, "get_monitors", lambda: [])

    fake_cse = make_create_subprocess([FakeProcess()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_cse)

    async def fake_sleep(d):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    enc = _make_enc()
    enc._low_power = True
    preset = video.PRESETS["high"]
    region = {"x": 0, "y": 0, "w": 1920, "h": 1080, "name": "s"}

    enc.start(preset, region)
    await enc._task

    assert enc._low_power is False
    assert fake_cse.calls["n"] >= 6


async def test_stop_terminate_e_kill_falham():
    enc = _make_enc()
    proc = FakeProcess(terminate_raises=True)
    proc.returncode = None

    def kill_raises():
        raise OSError("kill boom")

    proc.kill = kill_raises
    enc.proc = proc
    enc._task = None
    await enc.stop()  # except Exception: pass no inner try cobre 208-209


async def test_stop_task_levanta():
    enc = _make_enc()
    enc.proc = None
    enc._stop = False

    async def bad_coro():
        raise RuntimeError("task error")

    task = asyncio.create_task(bad_coro())
    await asyncio.sleep(0)  # task completa com RuntimeError
    enc._task = task
    await enc.stop()  # except (CancelledError, Exception): pass cobre 214-215


async def test_drain_stderr_exception():
    enc = _make_enc()

    class ErrorStream:
        def __aiter__(self): return self
        async def __anext__(self): raise RuntimeError("stderr boom")

    proc_mock = mock.MagicMock()
    proc_mock.stderr = ErrorStream()
    enc.proc = proc_mock
    await enc._drain_stderr()  # except Exception: pass cobre 251-252


async def test_read_stream_stop_imediato():
    # _stop=True antes de entrar no loop → 258->exit
    enc = _make_enc()
    enc.proc = FakeProcess(stdout_chunks=[b"data"])
    enc._stop = True
    await enc._read_stream()


async def test_read_stream_sem_aud():
    # i < 0: break (linha 266)
    received = []

    async def on_au(is_key, au):
        received.append(au)

    enc = video.ScreenEncoder(on_au)
    enc.proc = FakeProcess(stdout_chunks=[b"no_aud_data_here"])
    enc._stop = False
    await enc._read_stream()
    assert received == []


async def test_read_stream_aud_nao_zero():
    # j < 0 e i > 0: del buf[:i] (linha 270)
    AUD = b"\x00\x00\x01\x09"
    received = []

    async def on_au(is_key, au):
        received.append(au)

    enc = video.ScreenEncoder(on_au)
    # AUD está no meio, sem segundo AUD → j<0, i>0 → del buf[:i]
    enc.proc = FakeProcess(stdout_chunks=[b"garbage" + AUD + b"payload"])
    enc._stop = False
    await enc._read_stream()
    # sem AU completo (precisa de segundo AUD pra delimitar)
    assert received == []


async def test_supervise_stop_durante_sleep(monkeypatch):
    # _stop setado durante sleep → while condition False → 219->exit
    monkeypatch.setattr(video, "ENCODER", "x264")
    monkeypatch.setattr(video, "get_monitors", lambda: [])

    fake_cse = make_create_subprocess([FakeProcess()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_cse)

    enc = _make_enc()

    async def stop_during_sleep(d):
        enc._stop = True

    monkeypatch.setattr(asyncio, "sleep", stop_during_sleep)

    preset = video.PRESETS["high"]
    region = {"x": 0, "y": 0, "w": 1920, "h": 1080, "name": "s"}
    enc.start(preset, region)
    await enc._task
    assert enc._stop is True


async def test_supervise_stop_no_meio(monkeypatch):
    monkeypatch.setattr(video, "ENCODER", "x264")
    monkeypatch.setattr(video, "get_monitors", lambda: [])

    fake_cse = make_create_subprocess([FakeProcess()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_cse)

    async def fake_sleep(d):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    enc = _make_enc()
    preset = video.PRESETS["high"]
    region = {"x": 0, "y": 0, "w": 1920, "h": 1080, "name": "s"}

    call_count = [0]
    original_build = enc._build_cmd

    async def stop_after_first():
        enc._stop = True

    enc._read_stream = stop_after_first
    enc.start(preset, region)
    await enc._task

    assert enc._stop is True
    assert fake_cse.calls["n"] == 1
