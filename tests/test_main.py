import asyncio
import json
import ssl as ssl_mod
from unittest import mock

import pytest

from server import audio, guardian, video
from server import main as M
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
    """Avoids real xrandr calls in every test of this module."""
    monkeypatch.setattr(video, "get_monitors", lambda: [])


# ───────────────────────── VideoClient ─────────────────────────

def test_videoclient_feed_inactive_non_key():
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    assert c.active is False
    c.feed(False, b"au")
    assert c.q.empty()


def test_videoclient_feed_key_activates():
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    c.feed(True, b"keyframe")
    assert c.active is True
    assert not c.q.empty()


def test_videoclient_feed_queuefull():
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    c.active = True
    # Fill the queue
    for _ in range(180):
        c.q.put_nowait(("bin", b"x"))
    # The next feed must trigger _flush + active=False
    c.feed(True, b"overflow")
    assert c.active is False


def test_videoclient_reset():
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    c.active = True
    c.q.put_nowait(("bin", b"something"))
    c.reset()
    # the queue must hold the reset message
    items = []
    while not c.q.empty():
        items.append(c.q.get_nowait())
    assert any(k == "txt" for k, _ in items)
    assert c.active is False


def test_videoclient_flush_queueempty():
    # QueueEmpty during _flush -> except QueueEmpty: break
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    c.q = mock.MagicMock()
    c.q.empty = mock.Mock(return_value=False)
    c.q.get_nowait = mock.Mock(side_effect=asyncio.QueueEmpty())
    c._flush()  # must not raise


def test_videoclient_reset_queuefull():
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    # Fill the queue and replace _flush with a no-op to force QueueFull on reset
    c.q = asyncio.Queue(maxsize=1)
    c.q.put_nowait(("bin", b"x"))
    c._flush = lambda: None  # no-op -> the queue stays full
    c.reset()  # QueueFull on the reset put_nowait -> except QueueFull: pass


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


def test_audioclient_feed_full_drops_oldest():
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
    # Make get_nowait raise QueueEmpty

    def raise_empty():
        raise asyncio.QueueEmpty()

    c.q.get_nowait = raise_empty
    c.feed(b"new")  # must not raise


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


def test_hub_initial_preset_follows_default(hub_fixture):
    hub, _, _ = hub_fixture
    # initial settings mirror the default preset
    base = video.PRESETS[video.DEFAULT_PRESET]
    assert (hub.settings.max_h, hub.settings.fps, hub.settings.bitrate) == \
        (base.max_h, base.fps, base.bitrate)
    # the .preset property returns the current settings (duck-typed with Preset)
    assert hub.preset is hub.settings


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


async def test_hub_add_first_starts(hub_fixture):
    hub, inj, enc = hub_fixture
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    await hub.add_client(c)
    assert hub._running is True
    enc.start.assert_called_once()
    inj.set_geometry.assert_called()


async def test_hub_add_second_does_not_restart(hub_fixture):
    hub, _, enc = hub_fixture
    ws = mock.MagicMock()
    c1 = M.VideoClient(ws)
    c2 = M.VideoClient(ws)
    await hub.add_client(c1)
    await hub.add_client(c2)
    assert enc.start.call_count == 1


async def test_hub_remove_last_stops(hub_fixture):
    hub, _, enc = hub_fixture
    ws = mock.MagicMock()
    c = M.VideoClient(ws)
    await hub.add_client(c)
    await hub.remove_client(c)
    assert hub._running is False
    enc.stop.assert_called()


async def test_hub_remove_not_last(hub_fixture):
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


async def test_hub_restart_running(monkeypatch):
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


async def test_hub_restart_stopped(hub_fixture):
    hub, inj, enc = hub_fixture
    hub._running = False
    await hub._restart()
    enc.stop.assert_not_called()
    inj.set_geometry.assert_called()


async def test_hub_set_preset_changes(monkeypatch):
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


async def test_hub_set_preset_reapplies_same_name(hub_fixture):
    # reselecting the same preset reapplies the 3 params (clears custom overrides)
    # and restarts the encoder.
    hub, _, enc = hub_fixture
    hub._running = True
    hub.preset_name = "high"
    hub.settings = video.VideoSettings(max_h=123, fps=45, bitrate="1M", name="custom")
    await hub.set_preset("high")
    enc.stop.assert_called()
    assert hub.settings.max_h == video.PRESETS["high"].max_h


async def test_hub_set_preset_invalid(hub_fixture):
    hub, _, enc = hub_fixture
    hub._running = True
    await hub.set_preset("nonexistent")
    enc.stop.assert_not_called()


async def test_hub_set_video_independent(hub_fixture):
    # fps-only override: changes settings, becomes "custom" and restarts
    hub, _, enc = hub_fixture
    hub._running = True
    before = hub.settings
    await hub.set_video(fps=90)
    assert hub.settings.fps == 90
    assert hub.settings.max_h == before.max_h  # the rest is preserved
    assert hub.preset_name == "custom"
    enc.stop.assert_called()


async def test_hub_set_video_without_change_does_not_restart(hub_fixture):
    hub, _, enc = hub_fixture
    hub._running = True
    s = hub.settings
    await hub.set_video(max_h=s.max_h, fps=s.fps, bitrate=s.bitrate)
    enc.stop.assert_not_called()


async def test_hub_set_monitor_changes(monkeypatch):
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


async def test_hub_set_monitor_same(hub_fixture):
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
    # AudioHub._on_pkt fanout
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


async def test_audiohub_add_second(ahub_fixture):
    ahub, enc = ahub_fixture
    ws = mock.MagicMock()
    c1 = M.AudioClient(ws)
    c2 = M.AudioClient(ws)
    await ahub.add_client(c1)
    await ahub.add_client(c2)
    assert enc.start.call_count == 1


async def test_audiohub_remove_last(ahub_fixture):
    ahub, enc = ahub_fixture
    ws = mock.MagicMock()
    c = M.AudioClient(ws)
    await ahub.add_client(c)
    await ahub.remove_client(c)
    assert ahub._running is False
    enc.stop.assert_called()


async def test_audiohub_remove_not_last(ahub_fixture):
    ahub, enc = ahub_fixture
    ws = mock.MagicMock()
    c1 = M.AudioClient(ws)
    c2 = M.AudioClient(ws)
    await ahub.add_client(c1)
    await ahub.add_client(c2)
    enc.stop.reset_mock()
    await ahub.remove_client(c1)
    enc.stop.assert_not_called()


async def test_audiohub_set_preset_running(monkeypatch):
    encs = []

    def mk_enc(cb):
        e = mk_async_encoder()
        encs.append(e)
        return e

    monkeypatch.setattr(audio, "AudioEncoder", mk_enc)
    ahub = M.AudioHub()
    ahub._running = True
    ahub.preset_name = "medium"
    await ahub.set_preset("high")
    assert ahub.preset_name == "high"
    encs[0].stop.assert_called()
    assert encs[1].start.called


async def test_audiohub_set_preset_stopped(ahub_fixture):
    ahub, enc = ahub_fixture
    ahub._running = False
    ahub.preset_name = "medium"
    await ahub.set_preset("high")
    assert ahub.preset_name == "high"
    enc.stop.assert_not_called()


async def test_audiohub_set_preset_invalid(ahub_fixture):
    ahub, enc = ahub_fixture
    await ahub.set_preset("nonexistent")
    enc.stop.assert_not_called()


async def test_audiohub_set_preset_same(ahub_fixture):
    ahub, enc = ahub_fixture
    ahub.preset_name = "medium"
    await ahub.set_preset("medium")
    enc.stop.assert_not_called()


# ───────────────────────── routes / app ─────────────────────────

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
    # new fields of the split quality controls
    assert set(data["video"]) == {"res", "fps", "bitrate"}
    assert data["res_options"] and data["fps_options"] and data["bitrate_options"]
    assert data["audio_manual"] is False


async def test_api_gamepad_map_get(client, monkeypatch):
    monkeypatch.setattr(M.gamepad, "load_maps", lambda: {"Pad": {"buttons": {"a": 1}}})
    resp = await client.get("/api/gamepad-map")
    assert resp.status == 200
    assert (await resp.json())["Pad"]["buttons"]["a"] == 1


async def test_api_gamepad_map_post_ok(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(M.gamepad, "save_map",
                        lambda pid, m: saved.update({"id": pid, "map": m}))
    resp = await client.post("/api/gamepad-map",
                             json={"id": "Pad", "map": {"buttons": {"a": 2}}})
    assert resp.status == 200
    assert (await resp.json())["ok"] is True
    assert saved == {"id": "Pad", "map": {"buttons": {"a": 2}}}


async def test_api_gamepad_map_post_invalid_json(client):
    resp = await client.post("/api/gamepad-map", data="not json",
                             headers={"Content-Type": "application/json"})
    assert resp.status == 400
    assert "error" in (await resp.json())


async def test_api_gamepad_map_post_valueerror(client, monkeypatch):
    def boom(pid, m):
        raise ValueError("pad_id is required")
    monkeypatch.setattr(M.gamepad, "save_map", boom)
    resp = await client.post("/api/gamepad-map", json={"id": "", "map": {}})
    assert resp.status == 400
    assert "error" in (await resp.json())


async def test_api_gamepad_map_delete_ok(client, monkeypatch):
    monkeypatch.setattr(M.gamepad, "delete_map", lambda pid: True)
    resp = await client.delete("/api/gamepad-map", json={"id": "Pad"})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True and body["removed"] is True


async def test_api_gamepad_map_delete_invalid_json(client):
    resp = await client.delete("/api/gamepad-map", data="not json",
                               headers={"Content-Type": "application/json"})
    assert resp.status == 400


async def test_api_gamepad_map_delete_valueerror(client, monkeypatch):
    def boom(pid):
        raise ValueError("pad_id is required")
    monkeypatch.setattr(M.gamepad, "delete_map", boom)
    resp = await client.delete("/api/gamepad-map", json={"id": ""})
    assert resp.status == 400


async def test_ws_video(client):
    # Sends a regular (non-CLOSE/ERROR) message to cover the loop-continue branch
    ws = await client.ws_connect("/ws/video")
    await ws.send_bytes(b"data")
    await asyncio.sleep(0.02)
    await ws.close()


async def test_ws_audio(client):
    # Sends a regular message to cover the loop-continue branch
    ws = await client.ws_connect("/ws/audio")
    await ws.send_bytes(b"data")
    await asyncio.sleep(0.02)
    await ws.close()


async def test_ws_input_commands(client, monkeypatch):
    ws = await client.ws_connect("/ws/input")

    # preset change
    await ws.send_str(json.dumps({"t": "q", "preset": "low"}))
    # independent video (vq): resolution, fps and bitrate
    await ws.send_str(json.dumps({"t": "vq", "res": 720, "fps": 90, "bitrate": "7M"}))
    # audio preset change
    await ws.send_str(json.dumps({"t": "aq", "preset": "low"}))
    # monitor change (numeric)
    await ws.send_str(json.dumps({"t": "mon", "i": 1}))
    # monitor change (null)
    await ws.send_str(json.dumps({"t": "mon", "i": None}))
    # input command
    await ws.send_str(json.dumps({"t": "m", "x": 0.5, "y": 0.5}))
    # invalid JSON -> continue
    await ws.send_str("NOT_JSON{{{")
    # Injector.handle raising -> prints [input] error
    app = client.app
    app["injector"].handle = mock.Mock(side_effect=RuntimeError("test err"))
    await ws.send_str(json.dumps({"t": "m", "x": 0.1, "y": 0.1}))
    await asyncio.sleep(0.05)
    await ws.close()


async def test_ws_input_q_syncs_audio(client):
    # SINGLE state: one 'q' moves video AND audio together (AUDIO_FOR_QUALITY)
    ws = await client.ws_connect("/ws/input")
    await ws.send_str(json.dumps({"t": "q", "preset": "potato"}))
    await asyncio.sleep(0.05)
    assert client.app["hub"].preset_name == "potato"
    assert client.app["audio_hub"].preset_name == M.AUDIO_FOR_QUALITY["potato"]
    await ws.close()


async def test_ws_input_q_preset_without_audio(client):
    # 'q' with a preset that is NOT in AUDIO_FOR_QUALITY: audio does not change
    # (covers the false branch of `if preset in AUDIO_FOR_QUALITY`)
    before = client.app["audio_hub"].preset_name
    ws = await client.ws_connect("/ws/input")
    await ws.send_str(json.dumps({"t": "q", "preset": "zzz"}))
    await asyncio.sleep(0.05)
    assert client.app["audio_hub"].preset_name == before
    await ws.close()


async def test_ws_input_aq_marks_manual_and_q_does_not_drag(client):
    # after a manual 'aq', a preset 'q' NO longer changes the audio.
    ws = await client.ws_connect("/ws/input")
    await ws.send_str(json.dumps({"t": "aq", "preset": "voice"}))
    await asyncio.sleep(0.03)
    assert client.app["audio_hub"].manual is True
    assert client.app["audio_hub"].preset_name == "voice"
    # 'q' now does not drag the audio (stays "voice")
    await ws.send_str(json.dumps({"t": "q", "preset": "ultra"}))
    await asyncio.sleep(0.03)
    assert client.app["audio_hub"].preset_name == "voice"
    await ws.close()


def test_audio_for_quality_covers_all_presets():
    # every video level has a matching audio one (no holes in the single state)
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
    """Builds a WebSocketResponse mock that iterates over `items`."""

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
    # Tests the ws_video break via WSMsgType.ERROR
    from aiohttp import WSMessage, WSMsgType

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
    # Tests the ws_audio break via WSMsgType.ERROR
    from aiohttp import WSMessage, WSMsgType

    error_msg = WSMessage(type=WSMsgType.ERROR, data=None, extra=None)
    MockWS = await _make_mock_ws([error_msg])
    monkeypatch.setattr(M.web, "WebSocketResponse", MockWS)

    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: mk_async_encoder())
    ahub = M.AudioHub()

    req = mock.MagicMock()
    req.app = {"hub": None, "audio_hub": ahub, "injector": None}

    await M.ws_audio(req)


async def test_ws_input_error_msg(monkeypatch):
    # Tests the ws_input break via WSMsgType.ERROR
    from aiohttp import WSMessage, WSMsgType

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


async def test_ws_input_non_text(client):
    ws = await client.ws_connect("/ws/input")
    await ws.send_bytes(b"binary_data")
    await asyncio.sleep(0.05)
    await ws.close()


# ───────────────────────── cleanup ─────────────────────────

async def test_stop_guardian_without_task(monkeypatch):
    # stop_guardian without guardian_task -> `if task:` is False -> exit branch
    FakeUInput.instances.clear()
    monkeypatch.setattr(M, "Injector", lambda: FakeInjector())
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: mk_async_encoder())
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: mk_async_encoder())
    app = M.build_app()
    app.freeze()
    # startup is not called -> guardian_task is not in the app
    await app.cleanup()  # stop_guardian: task=None -> exit branch


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

    monkeypatch.setenv("LRD_GUARDIAN", "on")
    monkeypatch.setattr(guardian, "disabled", lambda: False)

    scan_results = iter([guardian.Match("J", "game", "cs2", "1")])
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

    # Wait for guardian_task to finish
    task = app.get("guardian_task")
    if task:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (TimeoutError, Exception):
            pass

    await asyncio.sleep(0)
    await app.cleanup()

    assert exit_code.get("code") == 99
    enc_v.stop.assert_called()
    fake_inj.close.assert_called()


# ───────────────────────── main() ─────────────────────────

def test_main_without_tls(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("LRD_CERT", raising=False)
    monkeypatch.delenv("LRD_KEY", raising=False)

    FakeUInput.instances.clear()
    monkeypatch.setattr(M, "Injector", lambda: FakeInjector())
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: mk_async_encoder())
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: mk_async_encoder())

    run_app_calls = {}
    monkeypatch.setattr(M.web, "run_app",
                        lambda *a, **kw: run_app_calls.update(kw))
    M.main()
    _, err = capsys.readouterr()
    assert "no TLS" in err and "WARNING" in err
    assert run_app_calls.get("ssl_context") is None


def test_main_with_tls(monkeypatch, tmp_path):
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("CERT")
    key_file.write_text("KEY")

    monkeypatch.setenv("LRD_CERT", str(cert_file))
    monkeypatch.setenv("LRD_KEY", str(key_file))

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


# ───────────────────────── make_injector (LRD_INPUT / no uinput) ─────────────────────────

@pytest.mark.parametrize("value", ["off", "0", "false", "no", " OFF "])
def test_make_injector_lrd_input_off_is_view_only(monkeypatch, capsys, value):
    monkeypatch.setenv("LRD_INPUT", value)
    monkeypatch.setattr(M, "Injector", mock.Mock(side_effect=AssertionError("must not open uinput")))
    inj = M.make_injector()
    assert isinstance(inj, M.NullInjector)
    assert "LRD_INPUT=off" in capsys.readouterr().out


def test_make_injector_default_uses_uinput(monkeypatch):
    monkeypatch.delenv("LRD_INPUT", raising=False)
    fake = FakeInjector()
    monkeypatch.setattr(M, "Injector", lambda: fake)
    assert M.make_injector() is fake


@pytest.mark.parametrize("error", [PermissionError("denied"), M.UInputError("no uinput")])
def test_make_injector_falls_back_when_uinput_fails(monkeypatch, capsys, error):
    monkeypatch.setenv("LRD_INPUT", "on")
    monkeypatch.setattr(M, "Injector", mock.Mock(side_effect=error))
    inj = M.make_injector()
    assert isinstance(inj, M.NullInjector)
    assert "view only" in capsys.readouterr().out


def test_null_injector_does_nothing():
    inj = M.NullInjector()
    inj.set_geometry((1920, 1080), {"x": 0, "y": 0, "w": 1920, "h": 1080})
    inj.handle({"t": "m", "x": 0.5, "y": 0.5})
    inj.close()


async def test_app_starts_view_only_without_uinput(monkeypatch, aiohttp_client):
    monkeypatch.setenv("LRD_INPUT", "on")
    monkeypatch.setattr(M, "Injector", mock.Mock(side_effect=OSError("no /dev/uinput")))
    monkeypatch.setattr(video, "ScreenEncoder", lambda cb: mk_async_encoder())
    monkeypatch.setattr(audio, "AudioEncoder", lambda cb: mk_async_encoder())
    client = await aiohttp_client(M.build_app())
    ws = await client.ws_connect("/ws/input")
    await ws.send_str(json.dumps({"t": "m", "x": 0.5, "y": 0.5}))
    await ws.close()
    assert isinstance(client.server.app["injector"], M.NullInjector)
