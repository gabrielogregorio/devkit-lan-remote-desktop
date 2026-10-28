"""
Captures the audio coming out of the PC (monitor of the PulseAudio/PipeWire
sink), encodes it to Opus and delivers RAW Opus packets (1 per message) ready
for the browser's WebCodecs AudioDecoder.

ffmpeg can only mux Opus into Ogg; so here we run an incremental Ogg parser
that reassembles the packets through lacing and drops the 2 headers
(OpusHead/OpusTags).

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

# special PulseAudio/pipewire-pulse token: follows the default sink's monitor
# without depending on pactl (which may be missing on pure PipeWire hosts).
DEFAULT_MONITOR_TOKEN = "@DEFAULT_MONITOR@"


@dataclass
class AudioPreset:
    name: str
    bitrate: str        # e.g. "128k"
    application: str     # "audio" (music) or "voip"


AUDIO_PRESETS = {
    "high":   AudioPreset("high",   "256k", "audio"),
    "medium": AudioPreset("medium", "128k", "audio"),
    "low":    AudioPreset("low",    "64k",  "audio"),
    "voice":  AudioPreset("voice",  "48k",  "voip"),
}
DEFAULT_AUDIO_PRESET = os.environ.get("LRD_AUDIO_PRESET", "medium")


_SRC_RE = re.compile(r"^\s*\*?\s*(\S+)\s+\[([^\]]*)\]")


def list_monitors() -> list[dict]:
    """Lists the monitor sources through `ffmpeg -sources pulse` (no pactl needed)."""
    try:
        # ffmpeg prints the list to stderr and exits with code !=0; that is fine
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
    """Default capture source: follows the default sink automatically."""
    forced = os.environ.get("LRD_AUDIO_SOURCE")
    if forced:
        return forced
    return DEFAULT_MONITOR_TOKEN


def audio_available() -> bool:
    """Audio is available if ffmpeg supports pulse and there is >=1 monitor (or one is forced)."""
    if os.environ.get("LRD_AUDIO_SOURCE"):
        return True
    if shutil.which("ffmpeg") is None:
        return False
    return len(list_monitors()) > 0


class OggOpusParser:
    """Incremental Ogg parser: feed bytes, get Opus packets back (without headers)."""

    def __init__(self):
        self._buf = bytearray()
        self._partial = bytearray()   # packet still being reassembled (spans segments/pages)
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
                del buf[:-4]          # keep only the tail that may start an "OggS"
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
        # if the page does NOT continue a previous packet, drop the orphan leftover
        if not (header_type & 0x01):
            self._partial = bytearray()
        off = 0
        cur = self._partial
        for lacing in seg_table:
            cur += body[off:off + lacing]
            off += lacing
            if lacing < 255:          # end of packet
                self._take_packet(bytes(cur), packets)
                cur = bytearray()
        self._partial = cur           # continues on the next page if it ended at 255

    def _take_packet(self, pkt: bytes, packets: list[bytes]):
        if not pkt:
            return
        if self._headers_left > 0:
            if pkt[:8] in (b"OpusHead", b"OpusTags"):
                self._headers_left -= 1
                return
            # rare case: already past the headers
            self._headers_left = 0
        packets.append(pkt)


class AudioEncoder:
    """Runs ffmpeg (pulse->opus->ogg) and delivers Opus packets through a callback."""

    def __init__(self, on_packet):
        self._on_packet = on_packet         # callback async(pkt: bytes)
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stop = False
        self.source = default_monitor()

    def _build_cmd(self, preset: AudioPreset) -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            # capture with short fragments -> lower input latency
            "-f", "pulse", "-fragment_size", "960", "-i", self.source,
            "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
            "-c:a", "libopus",
            "-b:a", preset.bitrate,
            "-vbr", "on", "-application", preset.application,
            "-frame_duration", "20",
            # CRITICAL: the Ogg muxer groups ~1s per page by default (page_duration=1e6),
            # holding the audio back. Force 1 page per frame (20ms) -> real time.
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
