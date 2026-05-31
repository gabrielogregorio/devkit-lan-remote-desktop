"""
Captura o audio que sai do PC (monitor do sink PulseAudio/PipeWire), encoda em
Opus e entrega pacotes Opus CRUS (1 por mensagem) prontos pro WebCodecs
AudioDecoder do navegador.

ffmpeg so sabe muxar Opus em Ogg; entao aqui fazemos um parser Ogg incremental
que remonta os pacotes via lacing e descarta os 2 headers (OpusHead/OpusTags).

    pulse(monitor) -> libopus -> ogg(pipe) -> [parser Ogg] -> pacotes Opus
"""

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

SAMPLE_RATE = 48000
CHANNELS = 2

# token especial do PulseAudio/pipewire-pulse: segue o monitor do sink default
# sem depender de pactl (que pode nao existir em hosts PipeWire puros).
DEFAULT_MONITOR_TOKEN = "@DEFAULT_MONITOR@"


@dataclass
class AudioPreset:
    name: str
    bitrate: str        # ex.: "128k"
    application: str     # "audio" (musica) ou "voip"


AUDIO_PRESETS = {
    "alta":   AudioPreset("alta",   "256k", "audio"),
    "media":  AudioPreset("media",  "128k", "audio"),
    "baixa":  AudioPreset("baixa",  "64k",  "audio"),
    "voz":    AudioPreset("voz",    "48k",  "voip"),
}
DEFAULT_AUDIO_PRESET = os.environ.get("RL_AUDIO_PRESET", "media")


_SRC_RE = re.compile(r"^\s*\*?\s*(\S+)\s+\[([^\]]*)\]")


def list_monitors() -> list[dict]:
    """Lista as monitor sources via `ffmpeg -sources pulse` (sem precisar de pactl)."""
    try:
        # ffmpeg imprime a lista no stderr e sai com codigo !=0; tudo bem
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-sources", "pulse"],
            capture_output=True, text=True, timeout=8,
        )
        text = (out.stdout or "") + (out.stderr or "")
    except Exception:
        text = ""
    mons = []
    for line in text.splitlines():
        m = _SRC_RE.match(line)
        if not m:
            continue
        name, desc = m.group(1), m.group(2)
        if name.endswith(".monitor"):
            mons.append({"name": name, "desc": desc})
    return mons


def default_monitor() -> str:
    """Fonte de captura padrao: segue o sink default automaticamente."""
    forced = os.environ.get("RL_AUDIO_SOURCE")
    if forced:
        return forced
    return DEFAULT_MONITOR_TOKEN


def audio_available() -> bool:
    """Audio disponivel se ffmpeg tem suporte a pulse e ha >=1 monitor (ou forcado)."""
    if os.environ.get("RL_AUDIO_SOURCE"):
        return True
    if shutil.which("ffmpeg") is None:
        return False
    return len(list_monitors()) > 0


class OggOpusParser:
    """Parser Ogg incremental: alimenta bytes, devolve pacotes Opus (sem headers)."""

    def __init__(self):
        self._buf = bytearray()
        self._partial = bytearray()   # pacote ainda sendo remontado (cruza segmentos/paginas)
        self._headers_left = 2        # OpusHead + OpusTags

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        packets: list[bytes] = []
        while True:
            page = self._next_page()
            if page is None:
                break
            self._emit_page(page, packets)
        return packets

    def _next_page(self):
        buf = self._buf
        start = buf.find(b"OggS")
        if start < 0:
            if len(buf) > 4:
                del buf[:-4]          # mantem so a cauda que pode iniciar "OggS"
            return None
        if start > 0:
            del buf[:start]
        if len(buf) < 27:
            return None
        nsegs = buf[26]
        header_len = 27 + nsegs
        if len(buf) < header_len:
            return None
        seg_table = buf[27:header_len]
        body_len = sum(seg_table)
        total = header_len + body_len
        if len(buf) < total:
            return None
        header_type = buf[5]
        body = bytes(buf[header_len:total])
        del buf[:total]
        return (header_type, seg_table, body)

    def _emit_page(self, page, packets: list[bytes]):
        header_type, seg_table, body = page
        # se a pagina NAO continua um pacote anterior, descarta sobra orfã
        if not (header_type & 0x01):
            self._partial = bytearray()
        off = 0
        cur = self._partial
        for lacing in seg_table:
            cur += body[off:off + lacing]
            off += lacing
            if lacing < 255:          # fim de pacote
                self._take_packet(bytes(cur), packets)
                cur = bytearray()
        self._partial = cur           # continua na proxima pagina se acabou em 255

    def _take_packet(self, pkt: bytes, packets: list[bytes]):
        if not pkt:
            return
        if self._headers_left > 0:
            if pkt[:8] in (b"OpusHead", b"OpusTags"):
                self._headers_left -= 1
                return
            # caso raro: ja passou dos headers
            self._headers_left = 0
        packets.append(pkt)


class AudioEncoder:
    """Roda o ffmpeg (pulse->opus->ogg) e entrega pacotes Opus via callback."""

    def __init__(self, on_packet):
        self._on_packet = on_packet         # callback async(pkt: bytes)
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stop = False
        self.source = default_monitor()

    def _build_cmd(self, preset: AudioPreset) -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            # captura com fragmentos curtos -> menor latencia de entrada
            "-f", "pulse", "-fragment_size", "960", "-i", self.source,
            "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
            "-c:a", "libopus",
            "-b:a", preset.bitrate,
            "-vbr", "on", "-application", preset.application,
            "-frame_duration", "20",
            # CRITICO: o muxer Ogg agrupa ~1s/pagina por padrao (page_duration=1e6),
            # segurando o audio. Forca 1 pagina por frame (20ms) -> tempo real.
            "-page_duration", "20000",
            "-flush_packets", "1",
            "-f", "ogg", "pipe:1",
        ]

    def start(self, preset: AudioPreset):
        self._stop = False
        self._task = asyncio.create_task(self._supervise(preset))

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

    async def _supervise(self, preset: AudioPreset):
        attempts = 0
        while not self._stop:
            attempts += 1
            self.proc = await asyncio.create_subprocess_exec(
                *self._build_cmd(preset),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stderr_task = asyncio.create_task(self._drain_stderr())
            try:
                await self._read_stream()
            finally:
                stderr_task.cancel()
            if self._stop or attempts > 5:
                break
            await asyncio.sleep(0.5)

    async def _drain_stderr(self):
        if not self.proc or not self.proc.stderr:
            return
        try:
            async for line in self.proc.stderr:
                txt = line.decode(errors="replace").strip()
                if txt:
                    print(f"[ffmpeg-audio] {txt}")
        except Exception:
            pass

    async def _read_stream(self):
        parser = OggOpusParser()
        reader = self.proc.stdout
        while not self._stop:
            chunk = await reader.read(8192)
            if not chunk:
                break
            for pkt in parser.feed(chunk):
                await self._on_packet(pkt)
