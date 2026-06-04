"""
remote-link - servidor.

  HTTPS estatico   ->  cliente web (web/)
  WSS /ws/video    ->  Access Units H.264 (binario)  [+ controle 'reset' em texto]
  WSS /ws/audio    ->  pacotes Opus (binario)        [+ 'cfg' com sample rate]
  WSS /ws/input    ->  mouse/teclado/gamepad e troca de qualidade (texto JSON)

Os encoders so rodam quando ha >=1 ouvinte/espectador. Cada cliente tem fila
limitada: se ficar pra tras, descarta (sem inchar latencia).
"""

import asyncio
import json
import os
import ssl
import sys

# Escape hatch anti-banimento: roda ANTES de carregar qualquer lib de controle
# (evdev/uinput) ou de tela (ffmpeg). Se houver anticheat/jogo online aberto, o
# processo é abortado aqui - as libs abaixo nem chegam a ser importadas. Ver
# server/guardian.py e o doc ANTIBAN.md.
from . import guardian
guardian.enforce_preflight()

from aiohttp import web, WSMsgType  # noqa: E402

from . import audio, video  # noqa: E402
from .inject import Injector  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(HERE), "web")

RESET_MSG = json.dumps({"t": "reset"})

# Estado UNICO de qualidade: cada nivel de video (a UI so expoe esse) arrasta o
# audio na mesma "altura". Assim "low" deixa video + audio + transmissao leves
# de uma vez - sem o usuario gerenciar dois controles. O canal 'aq' continua
# existindo pra override manual/RL_AUDIO_PRESET, mas a UI nao o expoe.
AUDIO_FOR_QUALITY = {
    "ultra":  "alta",
    "high":   "alta",
    "medium": "media",
    "low":    "baixa",
    "potato": "voz",
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
        # parametros de video INDEPENDENTES: comecam do preset default mas a UI
        # pode mexer em resolucao/fps/bitrate cada um por si (set_video).
        base = video.PRESETS.get(
            self.preset_name, video.PRESETS[video.DEFAULT_PRESET])
        self.settings = video.VideoSettings.from_preset(base)
        self.monitor_index: int | None = None
        self._running = False
        self._lock = asyncio.Lock()

    @property
    def preset(self) -> video.VideoSettings:
        # o encoder consome os parametros atuais (duck-type com Preset).
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
                      f"regiao={self.region['name']} {self.region['w']}x{self.region['h']}")

    async def remove_client(self, c: VideoClient):
        async with self._lock:
            self.clients.discard(c)
            if not self.clients and self._running:
                await self.encoder.stop()
                self._running = False
                print("[hub] encoder OFF (sem espectadores)")

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
                  f"regiao={self.region['name']} {self.region['w']}x{self.region['h']}")

    async def set_preset(self, name: str):
        # atalho: aplica os tres parametros do preset de uma vez.
        if name in video.PRESETS:
            self.preset_name = name
            self.settings = video.VideoSettings.from_preset(video.PRESETS[name])
            await self._restart()

    async def set_video(self, max_h=None, fps=None, bitrate=None):
        """Override INDEPENDENTE de resolucao/fps/bitrate (sem trocar preset)."""
        s = self.settings
        new = video.VideoSettings(
            max_h=int(max_h) if max_h is not None else s.max_h,
            fps=int(fps) if fps is not None else s.fps,
            bitrate=str(bitrate) if bitrate is not None else s.bitrate,
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
    """Um ouvinte. Fila pequena: se atrasar, descarta pacotes Opus antigos."""

    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self.q: asyncio.Queue = asyncio.Queue(maxsize=120)

    def feed(self, pkt: bytes):
        try:
            self.q.put_nowait(pkt)
        except asyncio.QueueFull:
            # descarta o mais antigo pra manter latencia baixa
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
    """Gerencia o encoder Opus e os ouvintes. Liga so com >=1 ouvinte."""

    def __init__(self):
        self.clients: set[AudioClient] = set()
        self.encoder = audio.AudioEncoder(self._on_pkt)
        self.preset_name = audio.DEFAULT_AUDIO_PRESET
        # manual=True quando o usuario escolheu o audio na mao -> o preset de
        # video ('q') para de arrastar o audio junto.
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
                print("[audio] encoder OFF (sem ouvintes)")

    async def set_preset(self, name: str):
        if name in audio.AUDIO_PRESETS and name != self.preset_name:
            self.preset_name = name
            async with self._lock:
                if self._running:
                    await self.encoder.stop()
                    self.encoder = audio.AudioEncoder(self._on_pkt)
                    self.encoder.start(self.preset)
                    print(f"[audio] encoder RESTART preset={self.preset_name}")


# ----------------------------- rotas -----------------------------

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
        # parametros de video atuais + faixas validas pros controles separados
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
        t = data.get("t")
        if t == "q":
            # atalho de preset: aplica video; o audio acompanha SO se o usuario
            # nao tiver assumido o controle manual do audio (audio_manual).
            preset = data.get("preset", "")
            await hub.set_preset(preset)
            if not ahub.manual and preset in AUDIO_FOR_QUALITY:
                await ahub.set_preset(AUDIO_FOR_QUALITY[preset])
        elif t == "vq":
            # qualidade de VIDEO independente: {res, fps, bitrate} (qualquer subset)
            await hub.set_video(
                max_h=data.get("res"),
                fps=data.get("fps"),
                bitrate=data.get("bitrate"),
            )
        elif t == "aq":
            # qualidade de AUDIO independente: marca controle manual do audio.
            ahub.manual = True
            await ahub.set_preset(data.get("preset", ""))
        elif t == "mon":
            idx = data.get("i")
            await hub.set_monitor(None if idx is None else int(idx))
        else:
            try:
                injector.handle(data)
            except Exception as ex:
                print(f"[input] erro: {ex}")
    return ws


def build_app() -> web.Application:
    injector = Injector()
    hub = Hub(injector)
    audio_hub = AudioHub()
    app = web.Application()
    app["injector"] = injector
    app["hub"] = hub
    app["audio_hub"] = audio_hub
    app.router.add_get("/", index)
    app.router.add_get("/api/monitors", api_monitors)
    app.router.add_get("/ws/video", ws_video)
    app.router.add_get("/ws/audio", ws_audio)
    app.router.add_get("/ws/input", ws_input)
    app.router.add_static("/", WEB_DIR)

    async def on_cleanup(app):
        await hub.encoder.stop()
        await audio_hub.encoder.stop()
        injector.close()
    app.on_cleanup.append(on_cleanup)

    # Escape hatch em runtime: se um anticheat/jogo online aparecer enquanto a
    # ferramenta roda, derruba TUDO na hora (solta o gamepad virtual / uinput e
    # mata o ffmpeg) e sai. Na duvida, fecha - risco de ban nao compensa.
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
    host = os.environ.get("RL_HOST", "0.0.0.0")
    port = int(os.environ.get("RL_PORT", "8443"))
    cert = os.environ.get("RL_CERT")
    key = os.environ.get("RL_KEY")

    ssl_ctx = None
    if cert and key and os.path.exists(cert) and os.path.exists(key):
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(cert, key)
    else:
        print("AVISO: sem TLS -> WebCodecs nao funciona fora de localhost.", file=sys.stderr)

    app = build_app()
    scheme = "https" if ssl_ctx else "http"
    print(f"[remote-link] video={video.ENCODER} audio={'on' if audio.audio_available() else 'off'} "
          f"servindo em {scheme}://{host}:{port}")
    web.run_app(app, host=host, port=port, ssl_context=ssl_ctx, print=None)


if __name__ == "__main__":
    main()
