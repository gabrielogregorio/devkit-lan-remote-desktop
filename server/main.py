"""
LAN Remote Desktop - server.

  static HTTPS     ->  web client (web/)
  WSS /ws/video    ->  H.264 Access Units (binary)   [+ 'reset' control as text]
  WSS /ws/audio    ->  Opus packets (binary)         [+ 'cfg' with the sample rate]
  WSS /ws/input    ->  mouse/keyboard/gamepad and quality changes (JSON text)

The encoders only run while there is >=1 listener/viewer. Each client has a
bounded queue: if it falls behind, it drops (no latency bloat).
"""

import asyncio
import json
import os
import ssl
import sys

# Anti-ban escape hatch: runs BEFORE loading any input (evdev/uinput) or screen
# (ffmpeg) library. If an anti-cheat/online game is open, the process is aborted
# here - the libraries below are never even imported. See server/guardian.py and
# ANTIBAN.md.
from . import guardian

guardian.enforce_preflight()

from aiohttp import WSMsgType, web  # noqa: E402

from . import audio, gamepad, video  # noqa: E402
from .inject import Injector, NullInjector, UInputError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(HERE), "web")

RESET_MSG = json.dumps({"t": "reset"})

# SINGLE quality state: each video level (the only one the UI exposes) drags
# the audio to the same "height". So "low" makes video + audio + transmission
# light at once - without the user managing two controls. The 'aq' channel
# still exists for a manual override/LRD_AUDIO_PRESET, but the UI does not expose it.
AUDIO_FOR_QUALITY = {
    "ultra":  "high",
    "high":   "high",
    "medium": "medium",
    "low":    "low",
    "potato": "voice",
}


class VideoClient:
    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self.q: asyncio.Queue = asyncio.Queue(maxsize=180)
        self.active = False

    def feed(self, is_key: bool, au: bytes):
        if not self.active:
            if not is_key:
                return
            self.active = True
        try:
            self.q.put_nowait(("bin", au))
        except asyncio.QueueFull:
            self._flush()
            self.active = False

    def reset(self):
        self._flush()
        self.active = False
        try:
            self.q.put_nowait(("txt", RESET_MSG))
        except asyncio.QueueFull:
            pass

    def _flush(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def sender(self):
        while True:
            kind, data = await self.q.get()
            if kind == "bin":
                await self.ws.send_bytes(data)
            else:
                await self.ws.send_str(data)


class Hub:
    def __init__(self, injector: Injector):
        self.injector = injector
        self.clients: set[VideoClient] = set()
        self.encoder = video.ScreenEncoder(self._on_au)
        self.preset_name = video.DEFAULT_PRESET
        # INDEPENDENT video parameters: they start from the default preset but the
        # UI can change resolution/fps/bitrate each on its own (set_video).
        base = video.PRESETS.get(
            self.preset_name, video.PRESETS[video.DEFAULT_PRESET])
        self.settings = video.VideoSettings.from_preset(base)
        self.monitor_index: int | None = None
        self._running = False
        self._lock = asyncio.Lock()

    @property
    def preset(self) -> video.VideoSettings:
        # the encoder consumes the current parameters (duck-typed with Preset).
        return self.settings

    @property
    def region(self) -> dict:
        return video.pick_region(self.monitor_index)

    async def _on_au(self, is_key: bool, au: bytes):
        for c in list(self.clients):
            c.feed(is_key, au)

    def _sync_geometry(self):
        self.injector.set_geometry(video.virtual_screen(), self.region)

    async def add_client(self, c: VideoClient):
        async with self._lock:
            self.clients.add(c)
            if not self._running:
                self._sync_geometry()
                self.encoder.start(self.preset, self.region)
                self._running = True
                print(f"[hub] encoder ON ({video.ENCODER}) preset={self.preset_name} "
                      f"region={self.region['name']} {self.region['w']}x{self.region['h']}")

    async def remove_client(self, c: VideoClient):
        async with self._lock:
            self.clients.discard(c)
            if not self.clients and self._running:
                await self.encoder.stop()
                self._running = False
                print("[hub] encoder OFF (no viewers)")

    async def _restart(self):
        async with self._lock:
            if not self._running:
                self._sync_geometry()
                return
            await self.encoder.stop()
            for c in self.clients:
                c.reset()
            self._sync_geometry()
            self.encoder = video.ScreenEncoder(self._on_au)
            self.encoder.start(self.preset, self.region)
            print(f"[hub] encoder RESTART preset={self.preset_name} "
                  f"region={self.region['name']} {self.region['w']}x{self.region['h']}")

    async def set_preset(self, name: str):
        # shortcut: applies the preset's three parameters at once.
        # isinstance before `in`: the client may send a list/dict (unhashable).
        if isinstance(name, str) and name in video.PRESETS:
            self.preset_name = name
            self.settings = video.VideoSettings.from_preset(video.PRESETS[name])
            await self._restart()

    async def set_video(self, max_h=None, fps=None, bitrate=None):
        """INDEPENDENT resolution/fps/bitrate override (without changing the preset).

        Each field goes through video.py's sanitizing: an out-of-range value is
        clamped and garbage (wrong type, meaningless string) keeps the current
        value. So a malformed 'vq' never takes down the encoder or the input connection."""
        s = self.settings
        new = video.VideoSettings(
            max_h=video.clamp_res(max_h, s.max_h) if max_h is not None else s.max_h,
            fps=video.clamp_fps(fps, s.fps) if fps is not None else s.fps,
            bitrate=(video.clean_bitrate(bitrate, s.bitrate)
                     if bitrate is not None else s.bitrate),
            name="custom",
        )
        if (new.max_h, new.fps, new.bitrate) == (s.max_h, s.fps, s.bitrate):
            return
        self.settings = new
        self.preset_name = "custom"
        await self._restart()

    async def set_monitor(self, idx: int | None):
        if idx != self.monitor_index:
            self.monitor_index = idx
            await self._restart()


class AudioClient:
    """A listener. Small queue: if it lags, old Opus packets are dropped."""

    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self.q: asyncio.Queue = asyncio.Queue(maxsize=120)

    def feed(self, pkt: bytes):
        try:
            self.q.put_nowait(pkt)
        except asyncio.QueueFull:
            # drop the oldest one to keep latency low
            try:
                self.q.get_nowait()
                self.q.put_nowait(pkt)
            except asyncio.QueueEmpty:
                pass

    async def sender(self):
        while True:
            pkt = await self.q.get()
            await self.ws.send_bytes(pkt)


class AudioHub:
    """Manages the Opus encoder and the listeners. Only on with >=1 listener."""

    def __init__(self):
        self.clients: set[AudioClient] = set()
        self.encoder = audio.AudioEncoder(self._on_pkt)
        self.preset_name = audio.DEFAULT_AUDIO_PRESET
        # manual=True when the user picked the audio by hand -> the video preset
        # ('q') stops dragging the audio along.
        self.manual = False
        self._running = False
        self._lock = asyncio.Lock()

    @property
    def preset(self) -> audio.AudioPreset:
        return audio.AUDIO_PRESETS.get(
            self.preset_name, audio.AUDIO_PRESETS[audio.DEFAULT_AUDIO_PRESET])

    async def _on_pkt(self, pkt: bytes):
        for c in list(self.clients):
            c.feed(pkt)

    async def add_client(self, c: AudioClient):
        async with self._lock:
            self.clients.add(c)
            if not self._running:
                self.encoder.start(self.preset)
                self._running = True
                print(f"[audio] encoder ON preset={self.preset_name} "
                      f"src={self.encoder.source}")

    async def remove_client(self, c: AudioClient):
        async with self._lock:
            self.clients.discard(c)
            if not self.clients and self._running:
                await self.encoder.stop()
                self._running = False
                print("[audio] encoder OFF (no listeners)")

    async def set_preset(self, name: str):
        if (isinstance(name, str) and name in audio.AUDIO_PRESETS
                and name != self.preset_name):
            self.preset_name = name
            async with self._lock:
                if self._running:
                    await self.encoder.stop()
                    self.encoder = audio.AudioEncoder(self._on_pkt)
                    self.encoder.start(self.preset)
                    print(f"[audio] encoder RESTART preset={self.preset_name}")


# ----------------------------- routes -----------------------------

async def index(request):
    return web.FileResponse(os.path.join(WEB_DIR, "index.html"))


async def api_monitors(request):
    hub: Hub = request.app["hub"]
    ahub: AudioHub = request.app["audio_hub"]
    s = hub.settings
    return web.json_response({
        "encoder": video.ENCODER,
        "monitors": video.get_monitors(),
        "current_monitor": hub.monitor_index,
        "presets": list(video.PRESETS.keys()),
        "current_preset": hub.preset_name,
        # current video parameters + valid ranges for the separate controls
        "video": {"res": s.max_h, "fps": s.fps, "bitrate": s.bitrate},
        "res_options": [360, 480, 540, 720, 900, 1080],
        "fps_options": [30, 45, 60, 90, 120],
        "bitrate_options": ["1200k", "2500k", "3500k", "7M", "12M", "25M"],
        "audio_available": audio.audio_available(),
        "audio_presets": list(audio.AUDIO_PRESETS.keys()),
        "current_audio_preset": ahub.preset_name,
        "audio_manual": ahub.manual,
    })


async def ws_video(request):
    hub: Hub = request.app["hub"]
    ws = web.WebSocketResponse(max_msg_size=0, heartbeat=20)
    await ws.prepare(request)
    client = VideoClient(ws)
    sender = asyncio.create_task(client.sender())
    await hub.add_client(client)
    try:
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        sender.cancel()
        await hub.remove_client(client)
    return ws


async def ws_audio(request):
    ahub: AudioHub = request.app["audio_hub"]
    ws = web.WebSocketResponse(max_msg_size=0, heartbeat=20)
    await ws.prepare(request)
    client = AudioClient(ws)
    sender = asyncio.create_task(client.sender())
    await ahub.add_client(client)
    try:
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        sender.cancel()
        await ahub.remove_client(client)
    return ws


async def api_gamepad_map_get(request):
    """Returns every saved remapping profile (by gamepad.id)."""
    return web.json_response(gamepad.load_maps())


async def api_gamepad_map_post(request):
    """Saves a controller profile. Body: {"id": "<gamepad.id>", "map": {...}}."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    # Valid JSON that is not an object (list, number, string) -> 400, not 500.
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    pad_id = body.get("id")
    mapping = body.get("map")
    try:
        gamepad.save_map(pad_id, mapping)
    except ValueError as ex:
        return web.json_response({"error": str(ex)}, status=400)
    return web.json_response({"ok": True})


async def api_gamepad_map_delete(request):
    """Deletes a controller profile. Body: {"id": "<gamepad.id>"}."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    try:
        removed = gamepad.delete_map(body.get("id"))
    except ValueError as ex:
        return web.json_response({"error": str(ex)}, status=400)
    return web.json_response({"ok": True, "removed": removed})


async def ws_input(request):
    hub: Hub = request.app["hub"]
    ahub: AudioHub = request.app["audio_hub"]
    injector: Injector = request.app["injector"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
            continue
        try:
            data = json.loads(msg.data)
        except Exception:
            continue
        # Valid JSON without the shape of a command (list, number, null): ignore it.
        # Without this a `data.get` blows up and TAKES DOWN the whole input
        # channel - the user loses mouse/keyboard until the client reconnects.
        if not isinstance(data, dict):
            continue
        t = data.get("t")
        if t == "q":
            # preset shortcut: applies video; the audio follows ONLY if the user
            # has not taken manual control of the audio (audio_manual).
            preset = data.get("preset", "")
            if not isinstance(preset, str):
                continue
            await hub.set_preset(preset)
            if not ahub.manual and preset in AUDIO_FOR_QUALITY:
                await ahub.set_preset(AUDIO_FOR_QUALITY[preset])
        elif t == "vq":
            # independent VIDEO quality: {res, fps, bitrate} (any subset)
            await hub.set_video(
                max_h=data.get("res"),
                fps=data.get("fps"),
                bitrate=data.get("bitrate"),
            )
        elif t == "aq":
            # independent AUDIO quality: flags manual control of the audio.
            ahub.manual = True
            await ahub.set_preset(data.get("preset", ""))
        elif t == "mon":
            idx = data.get("i")
            if idx is not None:
                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    continue          # invalid index: ignore the command
            await hub.set_monitor(idx)
        else:
            try:
                injector.handle(data)
            except Exception as ex:
                print(f"[input] error: {ex}")
    return ws


def input_disabled() -> bool:
    return os.environ.get("LRD_INPUT", "on").strip().lower() in ("off", "0", "false", "no")


def make_injector():
    """The real uinput Injector, or a NullInjector (view only) when LRD_INPUT=off
    or when the uinput devices cannot be created. Without /dev/uinput permission
    (or on a CI runner with no such device) the screen must still be viewable,
    instead of the whole server failing to start."""
    if input_disabled():
        print("[input] LRD_INPUT=off: input injection disabled (view only)")
        return NullInjector()
    try:
        return Injector()
    except (OSError, UInputError) as ex:
        print(f"[input] cannot create the uinput devices ({ex}): view only, no mouse/keyboard/gamepad")
        return NullInjector()


def build_app() -> web.Application:
    injector = make_injector()
    hub = Hub(injector)
    audio_hub = AudioHub()
    app = web.Application()
    app["injector"] = injector
    app["hub"] = hub
    app["audio_hub"] = audio_hub
    app.router.add_get("/", index)
    app.router.add_get("/api/monitors", api_monitors)
    app.router.add_get("/api/gamepad-map", api_gamepad_map_get)
    app.router.add_post("/api/gamepad-map", api_gamepad_map_post)
    app.router.add_delete("/api/gamepad-map", api_gamepad_map_delete)
    app.router.add_get("/ws/video", ws_video)
    app.router.add_get("/ws/audio", ws_audio)
    app.router.add_get("/ws/input", ws_input)
    app.router.add_static("/", WEB_DIR)

    async def on_cleanup(app):
        await hub.encoder.stop()
        await audio_hub.encoder.stop()
        injector.close()
    app.on_cleanup.append(on_cleanup)

    # Runtime escape hatch: if an anti-cheat/online game shows up while the tool
    # runs, take EVERYTHING down right away (release the virtual gamepad / uinput
    # and kill ffmpeg) and exit. When in doubt, shut down - a ban is not worth it.
    async def _trip(match: guardian.Match):
        try:
            await hub.encoder.stop()
            await audio_hub.encoder.stop()
            injector.close()
        finally:
            os._exit(99)

    async def on_startup(app):
        app["guardian_task"] = asyncio.create_task(guardian.monitor(_trip))
    app.on_startup.append(on_startup)

    async def stop_guardian(app):
        task = app.get("guardian_task")
        if task:
            task.cancel()
    app.on_cleanup.append(stop_guardian)
    return app


def main():
    host = os.environ.get("LRD_HOST", "0.0.0.0")
    port = int(os.environ.get("LRD_PORT", "8443"))
    cert = os.environ.get("LRD_CERT")
    key = os.environ.get("LRD_KEY")

    ssl_ctx = None
    if cert and key and os.path.exists(cert) and os.path.exists(key):
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(cert, key)
    else:
        print("WARNING: no TLS -> WebCodecs does not work outside localhost.", file=sys.stderr)

    app = build_app()
    scheme = "https" if ssl_ctx else "http"
    print(f"[lan-remote-desktop] video={video.ENCODER} audio={'on' if audio.audio_available() else 'off'} "
          f"serving on {scheme}://{host}:{port}")
    web.run_app(app, host=host, port=port, ssl_context=ssl_ctx, print=None)


if __name__ == "__main__":
    main()
