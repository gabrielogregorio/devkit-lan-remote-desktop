import asyncio
from unittest import mock

from server import video
from tests.helpers import FakeProcess, make_create_subprocess

# ───────────────────────── detect_encoder ─────────────────────────

def test_detect_forced(monkeypatch):
    monkeypatch.setenv("LRD_ENCODER", "vaapi")
    assert video.detect_encoder() == "vaapi"


def test_detect_exception(monkeypatch):
    monkeypatch.delenv("LRD_ENCODER", raising=False)

    def raise_exc(*a, **kw):
        raise OSError("no ffmpeg")

    monkeypatch.setattr(video.subprocess, "run", raise_exc)
    monkeypatch.setattr(video.shutil, "which", lambda x: None)
    monkeypatch.setattr(video.os.path, "exists", lambda p: False)
    assert video.detect_encoder() == "x264"


def test_detect_nvenc(monkeypatch):
    monkeypatch.delenv("LRD_ENCODER", raising=False)
    fake = type("R", (), {"stdout": "h264_nvenc h264_vaapi"})()
    monkeypatch.setattr(video.subprocess, "run", lambda *a, **kw: fake)
    monkeypatch.setattr(video.shutil, "which", lambda x: "/usr/bin/nvidia-smi")
    assert video.detect_encoder() == "nvenc"


def test_detect_vaapi(monkeypatch):
    monkeypatch.delenv("LRD_ENCODER", raising=False)
    fake = type("R", (), {"stdout": "h264_vaapi"})()
    monkeypatch.setattr(video.subprocess, "run", lambda *a, **kw: fake)
    monkeypatch.setattr(video.shutil, "which", lambda x: None)
    monkeypatch.setattr(video.os.path, "exists", lambda p: True)
    assert video.detect_encoder() == "vaapi"


def test_detect_x264(monkeypatch):
    monkeypatch.delenv("LRD_ENCODER", raising=False)
    fake = type("R", (), {"stdout": ""})()
    monkeypatch.setattr(video.subprocess, "run", lambda *a, **kw: fake)
    monkeypatch.setattr(video.shutil, "which", lambda x: None)
    monkeypatch.setattr(video.os.path, "exists", lambda p: False)
    assert video.detect_encoder() == "x264"


# ───────────────────────── Preset.gop ─────────────────────────

def test_preset_gop():
    # GOP ~1s (fast resync for a new client, no long black screen)
    assert video.PRESETS["high"].gop == 60


def test_video_settings_gop_and_from_preset():
    vs = video.VideoSettings.from_preset(video.PRESETS["low"])
    assert (vs.max_h, vs.fps, vs.bitrate) == (540, 60, "3500k")
    assert vs.gop == 60
    assert video.VideoSettings(max_h=720, fps=1, bitrate="7M").gop == 2  # floor of 2


def test_kbps_parse():
    assert video._kbps("12M") == 12000
    assert video._kbps("3500k") == 3500
    assert video._kbps("8000") == 8  # bits -> kbps
    assert video._kbps("garbage") == 8000  # fallback on ValueError


def test_lowlat_bufsize():
    assert video._lowlat_bufsize("12M") == "4000k"   # 12000/3
    assert video._lowlat_bufsize("300k") == "500k"   # floor of 500


# ───────────────────────── _x_env ─────────────────────────

def test_x_env_with_xauth(monkeypatch):
    monkeypatch.setenv("LRD_XAUTHORITY", "/tmp/xauth")
    env = video._x_env()
    assert env["XAUTHORITY"] == "/tmp/xauth"
    assert "DISPLAY" in env


def test_x_env_without_xauth(monkeypatch):
    monkeypatch.delenv("LRD_XAUTHORITY", raising=False)
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


def test_get_monitors_exception(monkeypatch):
    monkeypatch.setattr(video.subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    assert video.get_monitors() == []


# ───────────────────────── virtual_screen ─────────────────────────

def test_virtual_screen_with_monitors(monkeypatch):
    mons = [
        {"x": 0, "y": 0, "w": 1920, "h": 1080},
        {"x": 1920, "y": 0, "w": 1920, "h": 1080},
    ]
    monkeypatch.setattr(video, "get_monitors", lambda: mons)
    assert video.virtual_screen() == (3840, 1080)


def test_virtual_screen_empty(monkeypatch):
    monkeypatch.setattr(video, "get_monitors", lambda: [])
    assert video.virtual_screen() == (1920, 1080)


# ───────────────────────── pick_region ─────────────────────────

def test_pick_region_empty(monkeypatch):
    monkeypatch.setattr(video, "get_monitors", lambda: [])
    r = video.pick_region(None)
    assert r["name"] == "screen"


def test_pick_region_by_index(monkeypatch):
    mons = [
        {"index": 0, "name": "HDMI-0", "primary": True, "w": 1920, "h": 1080, "x": 0, "y": 0},
        {"index": 1, "name": "DP-1", "primary": False, "w": 1920, "h": 1080, "x": 1920, "y": 0},
    ]
    monkeypatch.setattr(video, "get_monitors", lambda: mons)
    r = video.pick_region(1)
    assert r["name"] == "DP-1"


def test_pick_region_missing_index_falls_back_to_primary(monkeypatch):
    mons = [
        {"index": 0, "name": "HDMI-0", "primary": True, "w": 1920, "h": 1080, "x": 0, "y": 0},
    ]
    monkeypatch.setattr(video, "get_monitors", lambda: mons)
    r = video.pick_region(99)
    assert r["name"] == "HDMI-0"


def test_pick_region_primary(monkeypatch):
    mons = [
        {"index": 0, "name": "HDMI-0", "primary": True, "w": 1920, "h": 1080, "x": 0, "y": 0},
        {"index": 1, "name": "DP-1", "primary": False, "w": 1920, "h": 1080, "x": 1920, "y": 0},
    ]
    monkeypatch.setattr(video, "get_monitors", lambda: mons)
    r = video.pick_region(None)
    assert r["name"] == "HDMI-0"


def test_pick_region_without_primary(monkeypatch):
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


def test_keyframe_no():
    au = b"\x00\x00\x01\x41" + b"\xff" * 10
    assert video._is_keyframe(au) is False


def test_keyframe_short():
    au = b"\x00\x00\x01"  # no byte after the start code
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


def test_build_nvenc_without_scale(monkeypatch):
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


def test_build_vaapi_without_lowpower(monkeypatch):
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


def test_build_x264_without_scale(monkeypatch):
    monkeypatch.setattr(video, "ENCODER", "x264")
    enc = _make_enc()
    region = {"x": 0, "y": 0, "w": 1920, "h": 720, "name": "s"}
    cmd = enc._build_cmd(video.PRESETS["high"], region)
    assert "libx264" in cmd
    assert "-vf" not in cmd


# ───────────────────────── ScreenEncoder._read_stream ─────────────────────────

async def test_read_stream_splits_aus():
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


async def test_read_stream_incomplete_au():
    AUD = b"\x00\x00\x01\x09"
    # single AUD start but no second AUD to delimit the AU
    data = AUD + b"\x00\x00\x01\x05partial"

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
    enc.proc = FakeProcess(stderr_lines=[b"error x\n", b"  \n"])
    await enc._drain_stderr()
    out = capsys.readouterr().out
    assert "error x" in out


async def test_drain_stderr_without_proc():
    enc = _make_enc()
    enc.proc = None
    await enc._drain_stderr()  # must not raise


# ───────────────────────── stop ─────────────────────────

async def test_stop_without_proc():
    enc = _make_enc()
    enc.proc = None
    enc._task = None
    await enc.stop()  # must not raise


async def test_stop_terminates():
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


async def test_stop_terminate_fails_kills():
    enc = _make_enc()
    proc = FakeProcess(terminate_raises=True)
    proc.returncode = None
    enc.proc = proc
    enc._task = None
    await enc.stop()
    assert proc.killed


# ───────────────────────── _supervise ─────────────────────────

async def test_supervise_x264_attempt_limit(monkeypatch):
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


async def test_stop_terminate_and_kill_fail():
    enc = _make_enc()
    proc = FakeProcess(terminate_raises=True)
    proc.returncode = None

    def kill_raises():
        raise OSError("kill boom")

    proc.kill = kill_raises
    enc.proc = proc
    enc._task = None
    await enc.stop()  # the except Exception: pass in the inner try


async def test_stop_task_raises():
    enc = _make_enc()
    enc.proc = None
    enc._stop = False

    async def bad_coro():
        raise RuntimeError("task error")

    task = asyncio.create_task(bad_coro())
    await asyncio.sleep(0)  # the task finishes with RuntimeError
    enc._task = task
    await enc.stop()  # covers except (CancelledError, Exception): pass


async def test_drain_stderr_exception():
    enc = _make_enc()

    class ErrorStream:
        def __aiter__(self): return self
        async def __anext__(self): raise RuntimeError("stderr boom")

    proc_mock = mock.MagicMock()
    proc_mock.stderr = ErrorStream()
    enc.proc = proc_mock
    await enc._drain_stderr()  # covers except Exception: pass


async def test_read_stream_immediate_stop():
    # _stop=True before entering the loop -> the while exits right away
    enc = _make_enc()
    enc.proc = FakeProcess(stdout_chunks=[b"data"])
    enc._stop = True
    await enc._read_stream()


async def test_read_stream_without_aud():
    # i < 0: break
    received = []

    async def on_au(is_key, au):
        received.append(au)

    enc = video.ScreenEncoder(on_au)
    enc.proc = FakeProcess(stdout_chunks=[b"no_aud_data_here"])
    enc._stop = False
    await enc._read_stream()
    assert received == []


async def test_read_stream_aud_not_at_zero():
    # j < 0 and i > 0: del buf[:i]
    AUD = b"\x00\x00\x01\x09"
    received = []

    async def on_au(is_key, au):
        received.append(au)

    enc = video.ScreenEncoder(on_au)
    # AUD in the middle, no second AUD -> j<0, i>0 -> del buf[:i]
    enc.proc = FakeProcess(stdout_chunks=[b"garbage" + AUD + b"payload"])
    enc._stop = False
    await enc._read_stream()
    # no complete AU (it needs a second AUD to delimit it)
    assert received == []


async def test_supervise_stop_during_sleep(monkeypatch):
    # _stop set during the sleep -> while condition False -> exit
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


async def test_supervise_stop_midway(monkeypatch):
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


    async def stop_after_first():
        enc._stop = True

    enc._read_stream = stop_after_first
    enc.start(preset, region)
    await enc._task

    assert enc._stop is True
    assert fake_cse.calls["n"] == 1
