"""
Screen capture + hardware H.264 encoding and slicing into Access Units (AU)
ready for the browser's WebCodecs.

Encoder picked automatically: NVENC (NVIDIA) > VAAPI (Intel/AMD) > x264 (CPU).

Pipeline (NVENC):
    x11grab -> [scale CPU] -> h264_nvenc (ull) -> bsf aud=insert -> pipe:1 (Annex-B)

Each AU starts with an AUD delimiter (NAL type 9), so slicing is trivial and
each AU becomes an EncodedVideoChunk on the client.
"""

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

RENDER_NODE = os.environ.get("LRD_RENDER_NODE", "/dev/dri/renderD128")


def detect_encoder() -> str:
    """Best available H.264 encoder: nvenc > vaapi > x264."""
    forced = os.environ.get("LRD_ENCODER")
    if forced:
        return forced
    try:
        enc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        enc = ""
    if "h264_nvenc" in enc and shutil.which("nvidia-smi"):
        return "nvenc"
    if "h264_vaapi" in enc and os.path.exists(RENDER_NODE):
        return "vaapi"
    return "x264"


ENCODER = detect_encoder()


@dataclass
class Preset:
    name: str
    max_h: int      # maximum output height (scales down, never up)
    fps: int
    bitrate: str    # e.g. "15M"

    @property
    def gop(self) -> int:
        # GOP ~1s: the keyframe is the heaviest frame, but on the LAN bandwidth is
        # plentiful and what matters is that a new client (and every resync after
        # an error/reset) does NOT sit on a black screen waiting for the next IDR.
        # 1s is the balance: fast resync without exaggerated spikes.
        return max(2, self.fps)


@dataclass
class VideoSettings:
    """INDEPENDENT video parameters (resolution, fps, bitrate). The UI can change
    each one alone; the presets are only shortcuts that fill in all three."""
    max_h: int
    fps: int
    bitrate: str
    name: str = "custom"

    @property
    def gop(self) -> int:
        return max(2, self.fps)

    @classmethod
    def from_preset(cls, p: "Preset") -> "VideoSettings":
        return cls(max_h=p.max_h, fps=p.fps, bitrate=p.bitrate, name=p.name)


def _kbps(bitrate: str) -> int:
    """'12M'/'3500k'/'1200' -> integer kbps (to compute the bufsize)."""
    s = str(bitrate).strip().lower()
    try:
        if s.endswith("m"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("k"):
            return int(float(s[:-1]))
        return int(float(s) / 1000)
    except (ValueError, OverflowError):
        # inf/NaN/garbage -> default bitrate (the VBV only needs a sane number)
        return 8000


def _lowlat_bufsize(bitrate: str) -> str:
    """Small VBV (~1 frame of headroom) for low-latency CBR: the encoder does not
    let the bitrate "burst" in a single frame and holds back the spikes that stall
    the decoder. We use ~1/3 of the bitrate/s (above 1 frame, below 1s)."""
    return f"{max(500, _kbps(bitrate) // 3)}k"


# Ranges accepted on the 'vq' channel (video quality coming from the client). The
# client is an open LAN: an absurd value or a wrong type must not turn into an
# impossible ffmpeg command line (the encoder dies in a loop and the screen goes
# black). The rule here is simple: clamp what can be clamped, ignore garbage.
RES_MIN, RES_MAX = 144, 2160
FPS_MIN, FPS_MAX = 5, 240
BITRATE_MIN_KBPS, BITRATE_MAX_KBPS = 100, 100_000
_BITRATE_RE = re.compile(r"^\d+(?:\.\d+)?[kKmM]?$")


def clamp_res(value, fallback: int) -> int:
    """Valid output height (or the current value, if the client sent garbage).
    OverflowError covers inf; ValueError covers NaN and strings with no number."""
    try:
        v = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return max(RES_MIN, min(RES_MAX, v))


def clamp_fps(value, fallback: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return max(FPS_MIN, min(FPS_MAX, v))


def clean_bitrate(value, fallback: str) -> str:
    """Accepts only the format ffmpeg understands ('12M', '3500k', '800000') AND
    that makes sense as a rate. The shape alone is not enough: '1.55' matches the
    regex but means 1.5 bit/s - the encoder would accept it and spit garbage.
    Outside the useful range (100 kbps to 100 Mbps) the current value is kept."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return fallback
    s = str(value).strip()
    if not _BITRATE_RE.match(s):
        return fallback
    return s if BITRATE_MIN_KBPS <= _kbps(s) <= BITRATE_MAX_KBPS else fallback


# SINGLE quality scale (the UI sends 1 level; the audio follows along - see
# main.AUDIO_FOR_QUALITY). max_h caps the output HEIGHT: since the source is
# usually 1080p, asking for 2160 does not "raise" anything (it only makes
# ultra == high). That is why the top stays at native 1080p and what really
# widens the range is lowering resolution + bitrate down to the floor (360p/1.2M) -
# a difference you can NOTICE on the phone, decodes easily (less lag) and holds
# 60fps on a weak device. fps stays at 60 down to "low" (light BUT smooth); only
# "potato" drops to 30 (the real floor for a bad network, half the packets).
PRESETS = {
    "ultra":  Preset("ultra",  1080, 60, "25M"),    # sharp 1080p (the source ceiling)
    "high":   Preset("high",    900, 60, "12M"),
    "medium": Preset("medium",  720, 60, "7M"),
    "low":    Preset("low",     540, 60, "3500k"),   # light and still 60fps
    "potato": Preset("potato",  360, 30, "1200k"),   # floor: phone/weak network
}
DEFAULT_PRESET = os.environ.get("LRD_PRESET", "high")

_MON_RE = re.compile(
    r"^\s*(\d+):\s+\+(\*)?(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)"
)


def _x_env() -> dict:
    env = dict(os.environ)
    env.setdefault("DISPLAY", os.environ.get("LRD_DISPLAY", ":0"))
    if "LRD_XAUTHORITY" in os.environ:
        env["XAUTHORITY"] = os.environ["LRD_XAUTHORITY"]
    return env


def get_monitors() -> list[dict]:
    """Lists the monitors through `xrandr --listmonitors`."""
    try:
        out = subprocess.run(
            ["xrandr", "--listmonitors"],
            capture_output=True, text=True, env=_x_env(), timeout=5,
        ).stdout
    except Exception:
        return []
    mons = []
    for line in out.splitlines():
        m = _MON_RE.match(line)
        if not m:
            continue
        idx, primary, name, w, h, x, y = m.groups()
        mons.append({
            "index": int(idx), "name": name, "primary": bool(primary),
            "w": int(w), "h": int(h), "x": int(x), "y": int(y),
        })
    return mons


def virtual_screen() -> tuple[int, int]:
    """Bounding box of the whole X desktop (all monitors)."""
    mons = get_monitors()
    if not mons:
        return (1920, 1080)
    tw = max(m["x"] + m["w"] for m in mons)
    th = max(m["y"] + m["h"] for m in mons)
    return (tw, th)


def pick_region(monitor_index: int | None) -> dict:
    """Capture region: a specific monitor, or the primary one by default."""
    mons = get_monitors()
    if not mons:
        return {"name": "screen", "w": 1920, "h": 1080, "x": 0, "y": 0}
    if monitor_index is not None:
        for m in mons:
            if m["index"] == monitor_index:
                return m
    for m in mons:
        if m["primary"]:
            return m
    return mons[0]


class ScreenEncoder:
    """Runs ffmpeg and delivers AUs (bytes) through an async callback."""

    def __init__(self, on_au):
        self._on_au = on_au          # callback(is_key: bool, au: bytes)
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stop = False
        self._low_power = True       # VAAPI low-power = lower latency

    def _build_cmd(self, preset: Preset, region: dict) -> list[str]:
        display = _x_env().get("DISPLAY", ":0")
        scale_down = region["h"] > preset.max_h

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "x11grab",
            "-framerate", str(preset.fps),
            "-video_size", f"{region['w']}x{region['h']}",
            "-draw_mouse", "1",
            "-thread_queue_size", "128",
            "-i", f"{display}+{region['x']},{region['y']}",
        ]

        bufsize = _lowlat_bufsize(preset.bitrate)

        if ENCODER == "nvenc":
            if scale_down:
                cmd += ["-vf", f"scale=-2:{preset.max_h}"]
            cmd += [
                "-c:v", "h264_nvenc",
                "-preset", "p1", "-tune", "ull",
                "-rc", "cbr", "-b:v", preset.bitrate, "-maxrate", preset.bitrate,
                "-bufsize", bufsize,
                "-g", str(preset.gop), "-bf", "0",
                # spatial AQ: improves perceived sharpness for the SAME bitrate (helps
                # run at a lower bitrate, which is what the browser decoder asks for).
                "-spatial-aq", "1", "-aq-strength", "8",
                # fixed GOP (no scenecut) -> predictable keyframes; forced-idr so the
                # client's reset/resync always lands on a real IDR.
                "-no-scenecut", "1", "-forced-idr", "1",
                "-profile:v", "high", "-delay", "0", "-zerolatency", "1",
                "-pix_fmt", "yuv420p",
            ]
        elif ENCODER == "vaapi":
            vf = "format=nv12,hwupload"
            if scale_down:
                vf += f",scale_vaapi=w=-2:h={preset.max_h}"
            cmd += [
                "-vaapi_device", RENDER_NODE, "-vf", vf,
                "-c:v", "h264_vaapi", "-rc_mode", "CBR",
                "-b:v", preset.bitrate, "-maxrate", preset.bitrate,
                "-bufsize", bufsize,
                "-g", str(preset.gop), "-bf", "0",
                "-profile:v", "high", "-async_depth", "1",
            ]
            if self._low_power:
                cmd += ["-low_power", "1"]
        else:  # x264 software (universal fallback)
            if scale_down:
                cmd += ["-vf", f"scale=-2:{preset.max_h}"]
            cmd += [
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-b:v", preset.bitrate, "-maxrate", preset.bitrate,
                "-bufsize", bufsize,
                "-g", str(preset.gop), "-bf", "0",
                # nal-hrd cbr + sliced threads: lower latency in software too.
                "-x264-params", "nal-hrd=cbr:sliced-threads=1:sync-lookahead=0",
                "-profile:v", "high", "-pix_fmt", "yuv420p",
            ]

        cmd += [
            "-bsf:v", "h264_metadata=aud=insert",
            "-flush_packets", "1",
            "-f", "h264", "pipe:1",
        ]
        return cmd

    def start(self, preset: Preset, region: dict):
        self._stop = False
        self._task = asyncio.create_task(self._supervise(preset, region))

    async def stop(self):
        self._stop = True
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _supervise(self, preset: Preset, region: dict):
        attempts = 0
        while not self._stop:
            attempts += 1
            cmd = self._build_cmd(preset, region)
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stderr_task = asyncio.create_task(self._drain_stderr())
            started = asyncio.get_event_loop().time()
            try:
                await self._read_stream()
            finally:
                stderr_task.cancel()
            if self._stop:
                break
            ran = asyncio.get_event_loop().time() - started
            if ran < 2.0 and ENCODER == "vaapi" and self._low_power:
                self._low_power = False
                continue
            if attempts > 5:
                break
            await asyncio.sleep(0.5)

    async def _drain_stderr(self):
        if not self.proc or not self.proc.stderr:
            return
        try:
            async for line in self.proc.stderr:
                txt = line.decode(errors="replace").strip()
                if txt:
                    print(f"[ffmpeg] {txt}")
        except Exception:
            pass

    async def _read_stream(self):
        AUD = b"\x00\x00\x01\x09"
        buf = bytearray()
        reader = self.proc.stdout
        while not self._stop:
            chunk = await reader.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                i = buf.find(AUD)
                if i < 0:
                    break
                j = buf.find(AUD, i + 4)
                if j < 0:
                    if i > 0:
                        del buf[:i]
                    break
                au = bytes(buf[i:j])
                del buf[:j]
                await self._on_au(_is_keyframe(au), au)


def _is_keyframe(au: bytes) -> bool:
    """Does the AU contain an IDR (NAL 5) or SPS (NAL 7)?"""
    i = 0
    while True:
        sc = au.find(b"\x00\x00\x01", i)
        if sc < 0:
            return False
        nt = au[sc + 3] & 0x1F if sc + 3 < len(au) else 0
        if nt in (5, 7):
            return True
        i = sc + 3
