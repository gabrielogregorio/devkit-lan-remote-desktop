import asyncio
import json
import ssl as ssl_mod
import pytest
from unittest import mock
from aiohttp import web

from server import main as M, video, audio, guardian
from tests.helpers import FakeUInput


# ──────────────────────────── helpers ────────────────────────────────

class FakeInjector:
    def __init__(self):
        self.set_geometry = mock.Mock()
        self.handle = mock.Mock()
        self.close = mock.Mock()


def mk_async_encoder():
    enc = mock.MagicMock()
    enc.start = mock.Mock()
    enc.stop = mock.AsyncMock()
    return enc


@pytest.fixture(autouse=True)
def _patch_get_monitors(monkeypatch):
    """Evita chamadas reais ao xrandr em todos os testes deste módulo."""
    monkeypatch.setattr(video, "get_monitors", lambda: [])


# ───────────────────────── VideoClient ─────────────────────────

def test_videoclient_feed_inativo_nao_key():
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    assert c.active is False
    c.feed(False, b"au")
    assert c.q.empty()


def test_videoclient_feed_key_ativa():
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    c.feed(True, b"keyframe")
    assert c.active is True
    assert not c.q.empty()


def test_videoclient_feed_queuefull():
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    c.active = True
    # Enche a fila
    for _ in range(180):
        c.q.put_nowait(("bin", b"x"))
    # Próximo feed deve acionar _flush + active=False
    c.feed(True, b"overflow")
    assert c.active is False


def test_videoclient_reset():
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    c.active = True
    c.q.put_nowait(("bin", b"something"))
    c.reset()
    # fila deve ter a mensagem de reset
    items = []
    while not c.q.empty():
        items.append(c.q.get_nowait())
    assert any(k == "txt" for k, _ in items)
    assert c.active is False


def test_videoclient_flush_queueempty():
    # QueueEmpty durante _flush → except QueueEmpty: break (linhas 66-67)
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    c.q = mock.MagicMock()
    c.q.empty = mock.Mock(return_value=False)
    c.q.get_nowait = mock.Mock(side_effect=asyncio.QueueEmpty())
    c._flush()  # não deve levantar


def test_videoclient_reset_queuefull():
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    # Enche a fila e substitui _flush por no-op para forçar QueueFull no reset
    c.q = asyncio.Queue(maxsize=1)
    c.q.put_nowait(("bin", b"x"))
    c._flush = lambda: None  # no-op → fila permanece cheia
    c.reset()  # QueueFull no put_nowait do reset → except QueueFull: pass


async def test_videoclient_sender():
    ws = mock.MagicMock()
    ws.send_bytes = mock.AsyncMock()
    ws.send_str = mock.AsyncMock()
    c = M.VideoClient(ws)
    c.q.put_nowait(("bin", b"frame"))
    c.q.put_nowait(("txt", '{"t":"reset"}'))

    task = asyncio.create_task(c.sender())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    ws.send_bytes.assert_called_once_with(b"frame")
    ws.send_str.assert_called_once_with('{"t":"reset"}')


# ───────────────────────── AudioClient ─────────────────────────

def test_audioclient_feed():
    ws = mock.MagicMock()
    c = M.AudioClient(ws)
    c.feed(b"pkt")
    assert not c.q.empty()


def test_audioclient_feed_full_descarta_antigo():
    ws = mock.MagicMock()
    c = M.AudioClient(ws)
    c.q = asyncio.Queue(maxsize=1)
    c.q.put_nowait(b"old")
    c.feed(b"new")
    assert c.q.get_nowait() == b"new"


def test_audioclient_feed_full_queueempty():
    ws = mock.MagicMock()
    c = M.AudioClient(ws)
    c.q = asyncio.Queue(maxsize=1)
    c.q.put_nowait(b"old")
    # Faz get_nowait lançar QueueEmpty
    original_get = c.q.get_nowait

    def raise_empty():
        raise asyncio.QueueEmpty()

    c.q.get_nowait = raise_empty
    c.feed(b"new")  # não deve levantar


async def test_audioclient_sender():
    ws = mock.MagicMock()
    ws.send_bytes = mock.AsyncMock()
    c = M.AudioClient(ws)
    c.q.put_nowait(b"opus_data")

    task = asyncio.create_task(c.sender())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    ws.send_bytes.assert_called_once_with(b"opus_data")


# ───────────────────────── Hub ─────────────────────────

@pytest.fixture
def hub_fixture(monkeypatch):
    enc = mk_async_encoder()
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: enc)
    inj = FakeInjector()
    hub = M.Hub(inj)
    return hub, inj, enc


def test_hub_preset_valido_invalido(hub_fixture):
    hub, _, _ = hub_fixture
    hub.preset_name = "zzz"
    assert hub.preset == video.PRESETS[video.DEFAULT_PRESET]


def test_hub_region(hub_fixture, monkeypatch):
    hub, _, _ = hub_fixture
    fake_region = {"name": "test", "w": 800, "h": 600, "x": 0, "y": 0}
    monkeypatch.setattr(video, "pick_region", lambda idx: fake_region)
    assert hub.region == fake_region


async def test_hub_on_au_fanout(hub_fixture):
    hub, _, _ = hub_fixture
    c1 = mock.MagicMock()
    c2 = mock.MagicMock()
    hub.clients = {c1, c2}
    await hub._on_au(True, b"au")
    c1.feed.assert_called_once_with(True, b"au")
    c2.feed.assert_called_once_with(True, b"au")


async def test_hub_add_first_inicia(hub_fixture):
    hub, inj, enc = hub_fixture
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    await hub.add_client(c)
    assert hub._running is True
    enc.start.assert_called_once()
    inj.set_geometry.assert_called()


async def test_hub_add_segundo_nao_reinicia(hub_fixture):
    hub, _, enc = hub_fixture
    ws = mock.MagicMock()
    c1 = M.VideoClient(ws)
    c2 = M.VideoClient(ws)
    await hub.add_client(c1)
    await hub.add_client(c2)
    assert enc.start.call_count == 1


async def test_hub_remove_ultimo_para(hub_fixture):
    hub, _, enc = hub_fixture
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    await hub.add_client(c)
    await hub.remove_client(c)
    assert hub._running is False
    enc.stop.assert_called()


async def test_hub_remove_nao_ultimo(hub_fixture):
    hub, _, enc = hub_fixture
    ws = mock.MagicMock()
    c1 = M.VideoClient(ws)
    c2 = M.VideoClient(ws)
    await hub.add_client(c1)
    await hub.add_client(c2)
    enc.stop.reset_mock()
    await hub.remove_client(c1)
    assert hub._running is True
    enc.stop.assert_not_called()


async def test_hub_restart_rodando(monkeypatch):
    encs = []

    def mk_enc(cb):
        e = mk_async_encoder()
        encs.append(e)
        return e

    monkeypatch.setattr(video, "ScreenEncoder", mk_enc)
    inj = FakeInjector()
    hub = M.Hub(inj)
    hub._running = True
    hub.clients = {mock.MagicMock()}
    await hub._restart()
    encs[0].stop.assert_called()
    assert encs[1].start.called


async def test_hub_restart_parado(hub_fixture):
    hub, inj, enc = hub_fixture
    hub._running = False
    await hub._restart()
    enc.stop.assert_not_called()
    inj.set_geometry.assert_called()


async def test_hub_set_preset_muda(monkeypatch):
    encs = []

    def mk_enc(cb):
        e = mk_async_encoder()
        encs.append(e)
        return e

    monkeypatch.setattr(video, "ScreenEncoder", mk_enc)
    inj = FakeInjector()
    hub = M.Hub(inj)
    hub._running = True
    hub.clients = {mock.MagicMock()}
    hub.preset_name = "high"
    await hub.set_preset("low")
    assert hub.preset_name == "low"


async def test_hub_set_preset_igual(hub_fixture):
    hub, _, enc = hub_fixture
    hub._running = True
    hub.preset_name = "high"
    await hub.set_preset("high")
    enc.stop.assert_not_called()


async def test_hub_set_preset_invalido(hub_fixture):
    hub, _, enc = hub_fixture
    hub._running = True
    await hub.set_preset("nonexistent")
    enc.stop.assert_not_called()


async def test_hub_set_monitor_muda(monkeypatch):
    encs = []

    def mk_enc(cb):
        e = mk_async_encoder()
        encs.append(e)
        return e

    monkeypatch.setattr(video, "ScreenEncoder", mk_enc)
    inj = FakeInjector()
    hub = M.Hub(inj)
    hub._running = True
    hub.clients = {mock.MagicMock()}
    hub.monitor_index = None
    await hub.set_monitor(1)
    assert hub.monitor_index == 1


async def test_hub_set_monitor_igual(hub_fixture):
    hub, _, enc = hub_fixture
    hub._running = True
    hub.monitor_index = 1
    await hub.set_monitor(1)
    enc.stop.assert_not_called()


# ───────────────────────── AudioHub ─────────────────────────

@pytest.fixture
def ahub_fixture(monkeypatch):
    enc = mk_async_encoder()
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: enc)
    ahub = M.AudioHub()
    return ahub, enc


def test_audiohub_preset_fallback(ahub_fixture):
    ahub, _ = ahub_fixture
    ahub.preset_name = "zzz"
    assert ahub.preset == audio.AUDIO_PRESETS[audio.DEFAULT_AUDIO_PRESET]


async def test_audiohub_on_pkt(ahub_fixture):
    # AudioHub._on_pkt fanout (linhas 186-187)
    ahub, _ = ahub_fixture
    c1 = mock.MagicMock()
    ahub.clients = {c1}
    await ahub._on_pkt(b"pkt")
    c1.feed.assert_called_once_with(b"pkt")


async def test_audiohub_add_first(ahub_fixture):
    ahub, enc = ahub_fixture
    ws = mock.MagicMock()
    c = M.AudioClient(ws)
    await ahub.add_client(c)
    assert ahub._running is True
    enc.start.assert_called_once()


async def test_audiohub_add_segundo(ahub_fixture):
    ahub, enc = ahub_fixture
    ws = mock.MagicMock()
    c1 = M.AudioClient(ws)
    c2 = M.AudioClient(ws)
    await ahub.add_client(c1)
    await ahub.add_client(c2)
    assert enc.start.call_count == 1


async def test_audiohub_remove_ultimo(ahub_fixture):
    ahub, enc = ahub_fixture
    ws = mock.MagicMock()
    c = M.AudioClient(ws)
    await ahub.add_client(c)
    await ahub.remove_client(c)
    assert ahub._running is False
    enc.stop.assert_called()


async def test_audiohub_remove_nao_ultimo(ahub_fixture):
    ahub, enc = ahub_fixture
    ws = mock.MagicMock()
    c1 = M.AudioClient(ws)
    c2 = M.AudioClient(ws)
    await ahub.add_client(c1)
    await ahub.add_client(c2)
    enc.stop.reset_mock()
    await ahub.remove_client(c1)
    enc.stop.assert_not_called()


async def test_audiohub_set_preset_rodando(monkeypatch):
    encs = []

    def mk_enc(cb):
        e = mk_async_encoder()
        encs.append(e)
        return e

    monkeypatch.setattr(audio, "AudioEncoder", mk_enc)
    ahub = M.AudioHub()
    ahub._running = True
    ahub.preset_name = "media"
    await ahub.set_preset("alta")
    assert ahub.preset_name == "alta"
    encs[0].stop.assert_called()
    assert encs[1].start.called


async def test_audiohub_set_preset_parado(ahub_fixture):
    ahub, enc = ahub_fixture
    ahub._running = False
    ahub.preset_name = "media"
    await ahub.set_preset("alta")
    assert ahub.preset_name == "alta"
    enc.stop.assert_not_called()


async def test_audiohub_set_preset_invalido(ahub_fixture):
    ahub, enc = ahub_fixture
    await ahub.set_preset("nonexistent")
    enc.stop.assert_not_called()


async def test_audiohub_set_preset_igual(ahub_fixture):
    ahub, enc = ahub_fixture
    ahub.preset_name = "media"
    await ahub.set_preset("media")
    enc.stop.assert_not_called()


# ───────────────────────── rotas / app ─────────────────────────

@pytest.fixture
async def client(monkeypatch, aiohttp_client):
    FakeUInput.instances.clear()
    monkeypatch.setattr(M, "Injector", lambda: FakeInjector())
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: mk_async_encoder())
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: mk_async_encoder())
    app = M.build_app()
    return await aiohttp_client(app)


async def test_index(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert "text/html" in resp.content_type


async def test_api_monitors(client, monkeypatch):
    monkeypatch.setattr(video, "get_monitors",
                        lambda: [{"index": 0, "name": "HDMI-0", "primary": True,
                                  "w": 1920, "h": 1080, "x": 0, "y": 0}])
    monkeypatch.setattr(audio, "audio_available", lambda: True)
    monkeypatch.setattr(audio, "list_monitors", lambda: [{"name": "m", "desc": "d"}])
    resp = await client.get("/api/monitors")
    assert resp.status == 200
    data = await resp.json()
    assert "encoder" in data
    assert "monitors" in data
    assert "presets" in data
    assert "audio_presets" in data


async def test_ws_video(client):
    # Envia mensagem regular (não-CLOSE/ERROR) para cobrir branch 247->246
    ws = await client.ws_connect("/ws/video")
    await ws.send_bytes(b"data")
    await asyncio.sleep(0.02)
    await ws.close()


async def test_ws_audio(client):
    # Envia mensagem regular para cobrir branch 264->263
    ws = await client.ws_connect("/ws/audio")
    await ws.send_bytes(b"data")
    await asyncio.sleep(0.02)
    await ws.close()


async def test_ws_input_comandos(client, monkeypatch):
    ws = await client.ws_connect("/ws/input")

    # preset change
    await ws.send_str(json.dumps({"t": "q", "preset": "low"}))
    # audio preset change
    await ws.send_str(json.dumps({"t": "aq", "preset": "baixa"}))
    # monitor change (numérico)
    await ws.send_str(json.dumps({"t": "mon", "i": 1}))
    # monitor change (null)
    await ws.send_str(json.dumps({"t": "mon", "i": None}))
    # input command
    await ws.send_str(json.dumps({"t": "m", "x": 0.5, "y": 0.5}))
    # JSON inválido → continue
    await ws.send_str("NOT_JSON{{{")
    # Injector.handle com exception → imprime [input] erro
    app = client.app
    app["injector"].handle = mock.Mock(side_effect=RuntimeError("test err"))
    await ws.send_str(json.dumps({"t": "m", "x": 0.1, "y": 0.1}))
    await asyncio.sleep(0.05)
    await ws.close()


async def test_ws_input_q_sincroniza_audio(client):
    # estado UNICO: um 'q' move video E audio juntos (AUDIO_FOR_QUALITY)
    ws = await client.ws_connect("/ws/input")
    await ws.send_str(json.dumps({"t": "q", "preset": "potato"}))
    await asyncio.sleep(0.05)
    assert client.app["hub"].preset_name == "potato"
    assert client.app["audio_hub"].preset_name == M.AUDIO_FOR_QUALITY["potato"]
    await ws.close()


async def test_ws_input_q_preset_sem_audio(client):
    # 'q' com preset que NAO esta em AUDIO_FOR_QUALITY: audio nao muda
    # (cobre o ramo falso do `if preset in AUDIO_FOR_QUALITY`)
    before = client.app["audio_hub"].preset_name
    ws = await client.ws_connect("/ws/input")
    await ws.send_str(json.dumps({"t": "q", "preset": "zzz"}))
    await asyncio.sleep(0.05)
    assert client.app["audio_hub"].preset_name == before
    await ws.close()


def test_audio_for_quality_cobre_todos_presets():
    # todo nivel de video tem um audio correspondente (sem buracos no estado unico)
    assert set(M.AUDIO_FOR_QUALITY) == set(video.PRESETS)
    for a in M.AUDIO_FOR_QUALITY.values():
        assert a in audio.AUDIO_PRESETS


async def test_ws_video_close_message(client):
    ws = await client.ws_connect("/ws/video")
    await ws.close()
    await asyncio.sleep(0.05)


async def test_ws_audio_close_message(client):
    ws = await client.ws_connect("/ws/audio")
    await ws.close()
    await asyncio.sleep(0.05)


async def _make_mock_ws(items):
    """Cria um mock de WebSocketResponse que itera sobre `items`."""
    from aiohttp import WSMsgType, WSMessage

    class _MockWS:
        def __init__(self, **kw):
            pass

        async def prepare(self, req):
            pass

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for item in items:
                yield item

        async def send_bytes(self, d):
            pass

        async def send_str(self, d):
            pass

    return _MockWS


async def test_ws_video_error_msg(monkeypatch):
    # Testa o break em 247-248 via WSMsgType.ERROR
    from aiohttp import WSMsgType, WSMessage

    error_msg = WSMessage(type=WSMsgType.ERROR, data=None, extra=None)
    MockWS = await _make_mock_ws([error_msg])
    monkeypatch.setattr(M.web, "WebSocketResponse", MockWS)

    FakeUInput.instances.clear()
    fake_inj = FakeInjector()
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: mk_async_encoder())
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: mk_async_encoder())

    hub = M.Hub(fake_inj)

    req = mock.MagicMock()
    req.app = {"hub": hub, "audio_hub": M.AudioHub(), "injector": fake_inj}

    await M.ws_video(req)


async def test_ws_audio_error_msg(monkeypatch):
    # Testa o break em 264-265 via WSMsgType.ERROR
    from aiohttp import WSMsgType, WSMessage

    error_msg = WSMessage(type=WSMsgType.ERROR, data=None, extra=None)
    MockWS = await _make_mock_ws([error_msg])
    monkeypatch.setattr(M.web, "WebSocketResponse", MockWS)

    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: mk_async_encoder())
    ahub = M.AudioHub()

    req = mock.MagicMock()
    req.app = {"hub": None, "audio_hub": ahub, "injector": None}

    await M.ws_audio(req)


async def test_ws_input_error_msg(monkeypatch):
    # Testa o break em 281 via WSMsgType.ERROR
    from aiohttp import WSMsgType, WSMessage

    error_msg = WSMessage(type=WSMsgType.ERROR, data=None, extra=None)
    MockWS = await _make_mock_ws([error_msg])
    monkeypatch.setattr(M.web, "WebSocketResponse", MockWS)

    FakeUInput.instances.clear()
    fake_inj = FakeInjector()
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: mk_async_encoder())
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: mk_async_encoder())

    hub = M.Hub(fake_inj)
    ahub = M.AudioHub()

    req = mock.MagicMock()
    req.app = {"hub": hub, "audio_hub": ahub, "injector": fake_inj}

    await M.ws_input(req)


async def test_ws_input_nao_texto(client):
    ws = await client.ws_connect("/ws/input")
    await ws.send_bytes(b"binary_data")
    await asyncio.sleep(0.05)
    await ws.close()


# ───────────────────────── cleanup ─────────────────────────

async def test_stop_guardian_sem_task(monkeypatch):
    # stop_guardian sem guardian_task → if task: False → 341->exit
    FakeUInput.instances.clear()
    monkeypatch.setattr(M, "Injector", lambda: FakeInjector())
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: mk_async_encoder())
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: mk_async_encoder())
    app = M.build_app()
    app.freeze()
    # não chama startup → guardian_task não está no app
    await app.cleanup()  # stop_guardian: task=None → branch 341->exit


async def test_on_cleanup(monkeypatch):
    FakeUInput.instances.clear()
    fake_inj = FakeInjector()
    monkeypatch.setattr(M, "Injector", lambda: fake_inj)

    enc_v = mk_async_encoder()
    enc_a = mk_async_encoder()
    enc_iter = iter([enc_v, enc_a])
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: next(enc_iter))
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: enc_a)

    app = M.build_app()
    app.freeze()
    await app.startup()
    await app.cleanup()

    enc_v.stop.assert_called()
    fake_inj.close.assert_called()


# ───────────────────────── _trip (runtime) ─────────────────────────

async def test_trip_runtime(monkeypatch):
    exit_code = {}

    def fake_exit(code):
        exit_code["code"] = code

    monkeypatch.setenv("RL_GUARDIAN", "on")
    monkeypatch.setattr(guardian, "disabled", lambda: False)

    scan_results = iter([guardian.Match("J", "jogo", "cs2", "1")])
    monkeypatch.setattr(guardian, "scan", lambda: next(scan_results))
    monkeypatch.setattr(guardian, "show_block_notice", lambda *a: None)
    monkeypatch.setattr(M.os, "_exit", fake_exit)

    FakeUInput.instances.clear()
    fake_inj = FakeInjector()
    monkeypatch.setattr(M, "Injector", lambda: fake_inj)

    enc_v = mk_async_encoder()
    enc_a = mk_async_encoder()
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: enc_v)
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: enc_a)

    app = M.build_app()
    app.freeze()
    await app.startup()

    # Aguarda a guardian_task completar
    task = app.get("guardian_task")
    if task:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass

    await asyncio.sleep(0)
    await app.cleanup()

    assert exit_code.get("code") == 99
    enc_v.stop.assert_called()
    fake_inj.close.assert_called()


# ───────────────────────── main() ─────────────────────────

def test_main_sem_tls(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("RL_CERT", raising=False)
    monkeypatch.delenv("RL_KEY", raising=False)

    FakeUInput.instances.clear()
    monkeypatch.setattr(M, "Injector", lambda: FakeInjector())
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: mk_async_encoder())
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: mk_async_encoder())

    run_app_calls = {}
    monkeypatch.setattr(M.web, "run_app",
                        lambda *a, **kw: run_app_calls.update(kw))
    M.main()
    _, err = capsys.readouterr()
    assert "sem TLS" in err or "AVISO" in err
    assert run_app_calls.get("ssl_context") is None


def test_main_com_tls(monkeypatch, tmp_path):
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("CERT")
    key_file.write_text("KEY")

    monkeypatch.setenv("RL_CERT", str(cert_file))
    monkeypatch.setenv("RL_KEY", str(key_file))

    FakeUInput.instances.clear()
    monkeypatch.setattr(M, "Injector", lambda: FakeInjector())
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: mk_async_encoder())
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: mk_async_encoder())

    class FakeSSLCtx:
        def load_cert_chain(self, *a): pass

    monkeypatch.setattr(ssl_mod, "SSLContext", lambda *a: FakeSSLCtx())

    run_app_calls = {}
    monkeypatch.setattr(M.web, "run_app",
                        lambda *a, **kw: run_app_calls.update(kw))
    M.main()
    assert run_app_calls.get("ssl_context") is not None
