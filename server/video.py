"""
Captura de tela + encode H.264 por hardware e fatiamento em Access Units (AU)
prontos para o WebCodecs do navegador.

Encoder escolhido automaticamente: NVENC (NVIDIA) > VAAPI (Intel/AMD) > x264 (CPU).

Pipeline (NVENC):
    x11grab -> [scale CPU] -> h264_nvenc (ull) -> bsf aud=insert -> pipe:1 (Annex-B)

Cada AU comeca com um delimitador AUD (NAL type 9), entao o fatiamento e trivial
e cada AU vira um EncodedVideoChunk no cliente.
"""

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

RENDER_NODE = os.environ.get("RL_RENDER_NODE", "/dev/dri/renderD128")


def detect_encoder() -> str:
    """Melhor encoder H.264 disponivel: nvenc > vaapi > x264."""
    forced = os.environ.get("RL_ENCODER")
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
    max_h: int      # altura maxima de saida (escala pra baixo, nunca pra cima)
    fps: int
    bitrate: str    # ex.: "15M"

    @property
    def gop(self) -> int:
        # GOP ~2s: keyframe e o frame mais pesado (pico de banda que o decoder do
        # browser engasga). Espacar pra ~2s reduz pela metade esses picos; novos
        # clientes ainda ressincronizam rapido (<=2s) e a UI pede reset no connect.
        return max(2, self.fps * 2)


@dataclass
class VideoSettings:
    """Parametros de video INDEPENDENTES (resolucao, fps, bitrate). A UI pode
    mexer em cada um sozinho; os presets sao so atalhos que preenchem os tres."""
    max_h: int
    fps: int
    bitrate: str
    name: str = "custom"

    @property
    def gop(self) -> int:
        return max(2, self.fps * 2)

    @classmethod
    def from_preset(cls, p: "Preset") -> "VideoSettings":
        return cls(max_h=p.max_h, fps=p.fps, bitrate=p.bitrate, name=p.name)


def _kbps(bitrate: str) -> int:
    """'12M'/'3500k'/'1200' -> kbps inteiro (pra calcular bufsize)."""
    s = str(bitrate).strip().lower()
    try:
        if s.endswith("m"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("k"):
            return int(float(s[:-1]))
        return int(float(s) / 1000)
    except ValueError:
        return 8000


def _lowlat_bufsize(bitrate: str) -> str:
    """VBV pequeno (~1 frame de margem) p/ CBR de baixa latencia: o encoder nao
    deixa o bitrate "estourar" num frame so e segura os picos que travam o
    decoder. Usamos ~1/3 do bitrate/s (acima de 1 frame, abaixo de 1s)."""
    return f"{max(500, _kbps(bitrate) // 3)}k"


# Escala UNICA de qualidade (a UI manda 1 nivel; o audio segue junto - ver
# main.AUDIO_FOR_QUALITY). max_h limita a ALTURA de saida: como a fonte costuma
# ser 1080p, pedir 2160 nao "sobe" nada (so faz ultra == high). Por isso o topo
# fica em 1080p nativo e o que abre a faixa de verdade e descer a resolucao +
# bitrate ate o chao (360p/1.2M) - diferenca que da pra PERCEBER no celular,
# decodifica facil (menos lag) e segura 60fps em device fraco.
# fps fica em 60 ate "low" (leve POReM suave); so "potato" cai pra 30 (piso real
# pra rede ruim, metade dos pacotes).
PRESETS = {
    "ultra":  Preset("ultra",  1080, 60, "25M"),    # 1080p nitido (teto da fonte)
    "high":   Preset("high",    900, 60, "12M"),
    "medium": Preset("medium",  720, 60, "7M"),
    "low":    Preset("low",     540, 60, "3500k"),   # leve e ainda 60fps
    "potato": Preset("potato",  360, 30, "1200k"),   # piso: celular/rede fraca
}
DEFAULT_PRESET = os.environ.get("RL_PRESET", "high")

_MON_RE = re.compile(
    r"^\s*(\d+):\s+\+(\*)?(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)"
)


def _x_env() -> dict:
    env = dict(os.environ)
    env.setdefault("DISPLAY", os.environ.get("RL_DISPLAY", ":0"))
    if "RL_XAUTHORITY" in os.environ:
        env["XAUTHORITY"] = os.environ["RL_XAUTHORITY"]
    return env


def get_monitors() -> list[dict]:
    """Lista os monitores via `xrandr --listmonitors`."""
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
    """Bounding box de toda a area de trabalho X (todos os monitores)."""
    mons = get_monitors()
    if not mons:
        return (1920, 1080)
    tw = max(m["x"] + m["w"] for m in mons)
    th = max(m["y"] + m["h"] for m in mons)
    return (tw, th)


def pick_region(monitor_index: int | None) -> dict:
    """Regiao de captura: um monitor especifico, ou o primario por padrao."""
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
    """Roda o ffmpeg e entrega AUs (bytes) via callback async."""

    def __init__(self, on_au):
        self._on_au = on_au          # callback(is_key: bool, au: bytes)
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stop = False
        self._low_power = True       # VAAPI low-power = menor latencia

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
                # spatial AQ: melhora a nitidez percebida pro MESMO bitrate (ajuda
                # a rodar num bitrate menor, que e o que o decoder do browser pede).
                "-spatial-aq", "1", "-aq-strength", "8",
                # GOP fixo (sem scenecut) -> keyframes previsiveis; forced-idr p/
                # o reset/ressync do cliente sempre cair num IDR de verdade.
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
        else:  # x264 software (fallback universal)
            if scale_down:
                cmd += ["-vf", f"scale=-2:{preset.max_h}"]
            cmd += [
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-b:v", preset.bitrate, "-maxrate", preset.bitrate,
                "-bufsize", bufsize,
                "-g", str(preset.gop), "-bf", "0",
                # nal-hrd cbr + sliced threads: latencia menor no software tambem.
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
    """AU contem IDR (NAL 5) ou SPS (NAL 7)?"""
    i = 0
    while True:
        sc = au.find(b"\x00\x00\x01", i)
        if sc < 0:
            return False
        nt = au[sc + 3] & 0x1F if sc + 3 < len(au) else 0
        if nt in (5, 7):
            return True
        i = sc + 3
