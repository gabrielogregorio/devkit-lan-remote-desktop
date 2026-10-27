"""
Testes de INTEGRAÇÃO: sobem o app aiohttp de verdade e conversam pelos
WebSockets do mesmo jeito que `web/app.js` conversa.

Os únicos fakes são o que toca hardware/processo externo (uinput e ffmpeg);
Hub, AudioHub, filas, rotas, serialização JSON e o ciclo de vida dos encoders
são todos reais. É a camada que os testes unitários não pegam: o que acontece
quando um cliente de verdade conecta, manda coisa errada e desconecta.
"""
import asyncio
import json

import pytest

from server import main as M, audio, video

MONITORS = [
    {"index": 0, "name": "HDMI-0", "primary": True, "w": 1920, "h": 1080, "x": 0, "y": 0},
    {"index": 1, "name": "DP-1", "primary": False, "w": 1920, "h": 1080, "x": 0, "y": 1080},
]

# AUs sintéticas: começam com AUD (0x09) e o keyframe carrega um SPS (NAL 7).
AU_KEY = b"\x00\x00\x01\x09\x10" + b"\x00\x00\x01\x67" + b"\x42\xe0\x1e" + b"\xaa" * 8
AU_DELTA = b"\x00\x00\x01\x09\x30" + b"\x00\x00\x01\x41" + b"\xbb" * 8


# ─────────────────────────── fakes de borda ───────────────────────────

class FakeInjector:
    def __init__(self):
        self.handled = []
        self.geometry = None
        self.closed = False
        self.raise_on_handle = False

    def set_geometry(self, total, region):
        self.geometry = (total, region)

    def handle(self, msg):
        if self.raise_on_handle:
            raise RuntimeError("injector explodiu")
        self.handled.append(msg)

    def close(self):
        self.closed = True


class FakeScreenEncoder:
    """Substitui o ffmpeg: guarda o callback pra o teste "gerar" Access Units."""
    instances: list["FakeScreenEncoder"] = []

    def __init__(self, on_au):
        self.on_au = on_au
        self.started = 0
        self.stopped = 0
        self.preset = None
        self.region = None
        FakeScreenEncoder.instances.append(self)

    def start(self, preset, region):
        self.started += 1
        self.preset = preset
        self.region = region

    async def stop(self):
        self.stopped += 1

    async def push(self, is_key, au):
        await self.on_au(is_key, au)


class FakeAudioEncoder:
    instances: list["FakeAudioEncoder"] = []

    def __init__(self, on_pkt):
        self.on_pkt = on_pkt
        self.started = 0
        self.stopped = 0
        self.preset = None
        self.source = "@DEFAULT_MONITOR@"
        FakeAudioEncoder.instances.append(self)

    def start(self, preset):
        self.started += 1
        self.preset = preset

    async def stop(self):
        self.stopped += 1

    async def push(self, pkt):
        await self.on_pkt(pkt)


# ─────────────────────────── infraestrutura ───────────────────────────

async def wait_until(pred, timeout=2.0, what="condição"):
    """Espera o handler do WS (que roda em outra task) chegar no estado esperado."""
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.005)
    raise AssertionError(f"{what} não ocorreu em {timeout}s")


@pytest.fixture
async def client(monkeypatch, aiohttp_client, tmp_path):
    FakeScreenEncoder.instances.clear()
    FakeAudioEncoder.instances.clear()
    monkeypatch.setattr(M, "Injector", FakeInjector)
    monkeypatch.setattr(video, "ScreenEncoder", FakeScreenEncoder)
    monkeypatch.setattr(audio, "AudioEncoder", FakeAudioEncoder)
    monkeypatch.setattr(video, "get_monitors", lambda: MONITORS)
    monkeypatch.setattr(audio, "audio_available", lambda: True)
    # perfis de gamepad num arquivo temporário (persistência real, sem mock)
    monkeypatch.setenv("RL_GAMEPAD_MAP", str(tmp_path / "gamepad-map.json"))
    app = M.build_app()
    return await aiohttp_client(app)


def vid_enc() -> FakeScreenEncoder:
    return FakeScreenEncoder.instances[-1]


def aud_enc() -> FakeAudioEncoder:
    return FakeAudioEncoder.instances[-1]


# ───────────────────────────── vídeo ─────────────────────────────

async def test_encoder_liga_com_espectador_e_desliga_sem_ninguem(client):
    enc = vid_enc()
    assert enc.started == 0, "encoder não pode rodar sem espectador"
    ws = await client.ws_connect("/ws/video")
    await wait_until(lambda: enc.started == 1, what="encoder ligar")
    await ws.close()
    await wait_until(lambda: enc.stopped == 1, what="encoder desligar")


async def test_espectador_so_recebe_a_partir_do_keyframe(client):
    """O VideoDecoder exige keyframe como 1º chunk: o servidor segura os deltas."""
    ws = await client.ws_connect("/ws/video")
    enc = vid_enc()
    await wait_until(lambda: enc.started == 1, what="encoder ligar")

    await enc.push(False, AU_DELTA)          # antes do keyframe: descartado
    await enc.push(True, AU_KEY)
    await enc.push(False, AU_DELTA)

    first = await ws.receive(timeout=2)
    assert first.data == AU_KEY, "o 1º AU entregue tem que ser o keyframe"
    second = await ws.receive(timeout=2)
    assert second.data == AU_DELTA
    await ws.close()


async def test_fanout_entrega_o_mesmo_au_para_todos(client):
    a = await client.ws_connect("/ws/video")
    b = await client.ws_connect("/ws/video")
    enc = vid_enc()
    hub = client.app["hub"]
    await wait_until(lambda: len(hub.clients) == 2, what="2 espectadores")

    await enc.push(True, AU_KEY)
    assert (await a.receive(timeout=2)).data == AU_KEY
    assert (await b.receive(timeout=2)).data == AU_KEY

    # um sai, o outro continua recebendo (encoder segue ligado)
    await a.close()
    await wait_until(lambda: len(hub.clients) == 1, what="1 espectador")
    assert enc.stopped == 0
    await enc.push(True, AU_KEY)
    assert (await b.receive(timeout=2)).data == AU_KEY
    await b.close()


async def test_troca_de_preset_manda_reset_e_reinicia_encoder(client):
    ws = await client.ws_connect("/ws/video")
    inp = await client.ws_connect("/ws/input")
    enc = vid_enc()
    await wait_until(lambda: enc.started == 1, what="encoder ligar")
    await enc.push(True, AU_KEY)
    assert (await ws.receive(timeout=2)).data == AU_KEY

    await inp.send_json({"t": "q", "preset": "low"})
    msg = await ws.receive(timeout=2)
    assert json.loads(msg.data) == {"t": "reset"}, "cliente precisa recriar o decoder"

    await wait_until(lambda: enc.stopped == 1, what="encoder antigo parar")
    novo = vid_enc()
    assert novo is not enc and novo.started == 1
    assert novo.preset.max_h == 540 and novo.preset.fps == 60
    await ws.close()
    await inp.close()


async def test_preset_arrasta_audio_ate_o_usuario_assumir_o_controle(client):
    inp = await client.ws_connect("/ws/input")
    ahub = client.app["audio_hub"]

    await inp.send_json({"t": "q", "preset": "potato"})
    await wait_until(lambda: ahub.preset_name == "voz", what="áudio seguir o preset")
    assert ahub.manual is False

    # escolha manual de áudio: a partir daqui 'q' não mexe mais no som
    await inp.send_json({"t": "aq", "preset": "alta"})
    await wait_until(lambda: ahub.preset_name == "alta", what="áudio manual")
    assert ahub.manual is True

    await inp.send_json({"t": "q", "preset": "potato"})
    await wait_until(lambda: client.app["hub"].preset_name == "potato",
                     what="vídeo mudar")
    assert ahub.preset_name == "alta", "áudio manual não pode ser arrastado"
    await inp.close()


async def test_vq_parcial_vira_custom_e_aparece_na_api(client):
    inp = await client.ws_connect("/ws/input")
    hub = client.app["hub"]
    res_antes = hub.settings.max_h

    await inp.send_json({"t": "vq", "fps": 30})
    await wait_until(lambda: hub.settings.fps == 30, what="fps mudar")

    data = await (await client.get("/api/monitors")).json()
    assert data["video"] == {"res": res_antes, "fps": 30, "bitrate": hub.settings.bitrate}
    assert data["current_preset"] == "custom"
    await inp.close()


async def test_troca_de_monitor_reinicia_com_a_nova_regiao(client):
    ws = await client.ws_connect("/ws/video")
    inp = await client.ws_connect("/ws/input")
    enc = vid_enc()
    await wait_until(lambda: enc.started == 1, what="encoder ligar")
    assert enc.region["name"] == "HDMI-0", "sem escolha explícita, captura o primário"

    await inp.send_json({"t": "mon", "i": 1})
    await wait_until(lambda: vid_enc() is not enc, what="encoder reiniciar")
    assert vid_enc().region["name"] == "DP-1"
    # a geometria do mouse acompanha a região capturada
    total, region = client.app["injector"].geometry
    assert total == (1920, 2160) and region["name"] == "DP-1"
    await ws.close()
    await inp.close()


# ───────────────────────────── áudio ─────────────────────────────

async def test_audio_liga_com_ouvinte_entrega_pacotes_e_desliga(client):
    enc = aud_enc()
    assert enc.started == 0
    ws = await client.ws_connect("/ws/audio")
    await wait_until(lambda: enc.started == 1, what="áudio ligar")

    await enc.push(b"opus-pkt-1")
    assert (await ws.receive(timeout=2)).data == b"opus-pkt-1"

    await ws.close()
    await wait_until(lambda: enc.stopped == 1, what="áudio desligar")


# ───────────────────────────── input ─────────────────────────────

async def test_input_chega_no_injector(client):
    inp = await client.ws_connect("/ws/input")
    injector = client.app["injector"]
    await inp.send_json({"t": "k", "code": "KeyA", "down": True})
    await wait_until(lambda: injector.handled, what="tecla chegar")
    assert injector.handled[0] == {"t": "k", "code": "KeyA", "down": True}
    await inp.close()


LIXO = [
    "isso não é json",
    "[]",                                   # JSON válido, mas não é um comando
    "5",
    '"texto"',
    "null",
    '{"t": 42}',                            # tipo desconhecido
    '{"t": "m"}',                           # faltando x/y
    '{"t": "m", "x": "abc", "y": null}',    # tipos errados
    '{"t": "k"}',
    '{"t": "g", "a": "nao-e-lista", "b": 7}',
    '{"t": "vq", "res": "abc"}',            # números inválidos
    '{"t": "vq", "res": [720]}',
    '{"t": "vq", "fps": {"a": 1}}',
    '{"t": "vq", "bitrate": "sei-la"}',
    '{"t": "q", "preset": ["low"]}',        # não-hashable
    '{"t": "q", "preset": null}',
    '{"t": "aq", "preset": {"x": 1}}',
    '{"t": "mon", "i": "x"}',
    '{"t": "mon", "i": [1]}',
]


@pytest.mark.parametrize("payload", LIXO)
async def test_input_sobrevive_a_payload_malformado(client, payload):
    """Nenhuma mensagem torta pode derrubar o canal de input do cliente."""
    inp = await client.ws_connect("/ws/input")
    injector = client.app["injector"]
    await inp.send_str(payload)
    # o canal continua vivo: o comando seguinte (válido) tem que chegar
    await inp.send_json({"t": "k", "code": "KeyZ", "down": True})
    await wait_until(lambda: {"t": "k", "code": "KeyZ", "down": True} in injector.handled,
                     what=f"canal sobreviver a {payload!r}")
    assert not inp.closed
    await inp.close()


async def test_input_ignora_binario_e_continua(client):
    inp = await client.ws_connect("/ws/input")
    injector = client.app["injector"]
    await inp.send_bytes(b"\x00\x01\x02")
    await inp.send_json({"t": "md", "b": 0})
    await wait_until(lambda: injector.handled, what="canal sobreviver a binário")
    assert injector.handled[-1] == {"t": "md", "b": 0}
    await inp.close()


async def test_input_sobrevive_a_injector_que_explode(client):
    inp = await client.ws_connect("/ws/input")
    injector = client.app["injector"]
    injector.raise_on_handle = True
    await inp.send_json({"t": "k", "code": "KeyA", "down": True})
    await asyncio.sleep(0.05)
    injector.raise_on_handle = False
    await inp.send_json({"t": "k", "code": "KeyB", "down": True})
    await wait_until(lambda: injector.handled, what="canal sobreviver ao erro")
    await inp.close()


async def test_vq_absurdo_e_clampado_e_nao_derruba_o_encoder(client):
    """Um cliente pedindo 99999p / 5000fps não pode inviabilizar o ffmpeg."""
    inp = await client.ws_connect("/ws/input")
    hub = client.app["hub"]
    await inp.send_json({"t": "vq", "res": 999999, "fps": 5000, "bitrate": "99M"})
    await wait_until(lambda: hub.settings.max_h != 900, what="vq aplicar")
    assert hub.settings.max_h <= video.RES_MAX
    assert hub.settings.fps <= video.FPS_MAX
    assert hub.settings.bitrate == "99M"

    await inp.send_json({"t": "vq", "res": 1, "fps": 0})
    await wait_until(lambda: hub.settings.fps == video.FPS_MIN, what="clamp mínimo")
    assert hub.settings.max_h == video.RES_MIN
    await inp.close()


# ─────────────────────────── HTTP / estáticos ───────────────────────────

async def test_pagina_de_debug_de_gamepad_e_servida(client):
    resp = await client.get("/gamepad-debug.html")
    assert resp.status == 200
    body = await resp.text()
    assert "Diagnóstico de gamepad" in body


async def test_gamepad_map_ciclo_completo_com_persistencia_real(client):
    perfil = {"buttons": {"a": 2, "b": 1}, "axes": {"lx": 0},
              "triggers": {"lt": {"src": "axis", "i": 4, "min": 0, "max": -1}}}

    assert await (await client.get("/api/gamepad-map")).json() == {}

    resp = await client.post("/api/gamepad-map", json={"id": "Pad PS2", "map": perfil})
    assert resp.status == 200

    salvos = await (await client.get("/api/gamepad-map")).json()
    assert salvos == {"Pad PS2": perfil}, "o perfil volta idêntico (blob opaco)"

    resp = await client.delete("/api/gamepad-map", json={"id": "Pad PS2"})
    assert (await resp.json()) == {"ok": True, "removed": True}
    assert await (await client.get("/api/gamepad-map")).json() == {}

    # apagar de novo: não existe mais, mas não é erro
    resp = await client.delete("/api/gamepad-map", json={"id": "Pad PS2"})
    assert (await resp.json())["removed"] is False


@pytest.mark.parametrize("body", ['"sou-uma-string"', "[1, 2]", "5", "null"])
async def test_gamepad_map_rejeita_json_que_nao_e_objeto(client, body):
    """JSON válido mas sem forma de objeto tem que dar 400 - já deu 500 aqui."""
    cabecalho = {"Content-Type": "application/json"}
    for metodo in (client.post, client.delete):
        resp = await metodo("/api/gamepad-map", data=body, headers=cabecalho)
        assert resp.status == 400
        assert "error" in await resp.json()
    # servidor segue de pé
    assert (await client.get("/api/monitors")).status == 200


async def test_api_monitors_bate_com_o_estado_do_hub(client):
    data = await (await client.get("/api/monitors")).json()
    hub = client.app["hub"]
    assert data["current_preset"] == hub.preset_name
    assert data["video"]["res"] == hub.settings.max_h
    assert [m["name"] for m in data["monitors"]] == ["HDMI-0", "DP-1"]
    # as opções oferecidas à UI têm que ser aceitáveis pelo canal 'vq'
    for r in data["res_options"]:
        assert video.RES_MIN <= r <= video.RES_MAX
    for f in data["fps_options"]:
        assert video.FPS_MIN <= f <= video.FPS_MAX
    for b in data["bitrate_options"]:
        assert video.clean_bitrate(b, "!") == b
