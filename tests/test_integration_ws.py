"""
INTEGRATION tests: they start the real aiohttp app and talk over the
WebSockets the same way `web/app.js` does.

The only fakes are what touches hardware/external processes (uinput and ffmpeg);
Hub, AudioHub, queues, routes, JSON serialization and the encoder lifecycle
are all real. This is the layer unit tests miss: what happens when a real
client connects, sends garbage and disconnects.
"""
import asyncio
import json

import pytest

from server import audio, video
from server import main as M

MONITORS = [
    {"index": 0, "name": "HDMI-0", "primary": True, "w": 1920, "h": 1080, "x": 0, "y": 0},
    {"index": 1, "name": "DP-1", "primary": False, "w": 1920, "h": 1080, "x": 0, "y": 1080},
]

# Synthetic AUs: they start with an AUD (0x09) and the keyframe carries an SPS (NAL 7).
AU_KEY = b"\x00\x00\x01\x09\x10" + b"\x00\x00\x01\x67" + b"\x42\xe0\x1e" + b"\xaa" * 8
AU_DELTA = b"\x00\x00\x01\x09\x30" + b"\x00\x00\x01\x41" + b"\xbb" * 8


# ─────────────────────────── edge fakes ───────────────────────────

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
            raise RuntimeError("injector blew up")
        self.handled.append(msg)

    def close(self):
        self.closed = True


class FakeScreenEncoder:
    """Replaces ffmpeg: keeps the callback so the test can "generate" Access Units."""
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


# ─────────────────────────── infrastructure ───────────────────────────

async def wait_until(pred, timeout=2.0, what="condition"):
    """Waits for the WS handler (running in another task) to reach the expected state."""
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.005)
    raise AssertionError(f"{what} did not happen within {timeout}s")


@pytest.fixture
async def client(monkeypatch, aiohttp_client, tmp_path):
    FakeScreenEncoder.instances.clear()
    FakeAudioEncoder.instances.clear()
    monkeypatch.setattr(M, "Injector", FakeInjector)
    monkeypatch.setattr(video, "ScreenEncoder", FakeScreenEncoder)
    monkeypatch.setattr(audio, "AudioEncoder", FakeAudioEncoder)
    monkeypatch.setattr(video, "get_monitors", lambda: MONITORS)
    monkeypatch.setattr(audio, "audio_available", lambda: True)
    # gamepad profiles in a temp file (real persistence, no mock)
    monkeypatch.setenv("LRD_GAMEPAD_MAP", str(tmp_path / "gamepad-map.json"))
    app = M.build_app()
    return await aiohttp_client(app)


def vid_enc() -> FakeScreenEncoder:
    return FakeScreenEncoder.instances[-1]


def aud_enc() -> FakeAudioEncoder:
    return FakeAudioEncoder.instances[-1]


# ───────────────────────────── video ─────────────────────────────

async def test_encoder_starts_with_a_viewer_and_stops_with_nobody(client):
    enc = vid_enc()
    assert enc.started == 0, "encoder must not run without a viewer"
    ws = await client.ws_connect("/ws/video")
    await wait_until(lambda: enc.started == 1, what="encoder start")
    await ws.close()
    await wait_until(lambda: enc.stopped == 1, what="encoder stop")


async def test_viewer_only_receives_from_the_keyframe_on(client):
    """VideoDecoder requires a keyframe as the 1st chunk: the server holds back the deltas."""
    ws = await client.ws_connect("/ws/video")
    enc = vid_enc()
    await wait_until(lambda: enc.started == 1, what="encoder start")

    await enc.push(False, AU_DELTA)          # before the keyframe: dropped
    await enc.push(True, AU_KEY)
    await enc.push(False, AU_DELTA)

    first = await ws.receive(timeout=2)
    assert first.data == AU_KEY, "the 1st AU delivered has to be the keyframe"
    second = await ws.receive(timeout=2)
    assert second.data == AU_DELTA
    await ws.close()


async def test_fanout_delivers_the_same_au_to_everyone(client):
    a = await client.ws_connect("/ws/video")
    b = await client.ws_connect("/ws/video")
    enc = vid_enc()
    hub = client.app["hub"]
    await wait_until(lambda: len(hub.clients) == 2, what="2 viewers")

    await enc.push(True, AU_KEY)
    assert (await a.receive(timeout=2)).data == AU_KEY
    assert (await b.receive(timeout=2)).data == AU_KEY

    # one leaves, the other keeps receiving (encoder stays on)
    await a.close()
    await wait_until(lambda: len(hub.clients) == 1, what="1 viewer")
    assert enc.stopped == 0
    await enc.push(True, AU_KEY)
    assert (await b.receive(timeout=2)).data == AU_KEY
    await b.close()


async def test_preset_change_sends_reset_and_restarts_encoder(client):
    ws = await client.ws_connect("/ws/video")
    inp = await client.ws_connect("/ws/input")
    enc = vid_enc()
    await wait_until(lambda: enc.started == 1, what="encoder start")
    await enc.push(True, AU_KEY)
    assert (await ws.receive(timeout=2)).data == AU_KEY

    await inp.send_json({"t": "q", "preset": "low"})
    msg = await ws.receive(timeout=2)
    assert json.loads(msg.data) == {"t": "reset"}, "client must recreate the decoder"

    await wait_until(lambda: enc.stopped == 1, what="old encoder stop")
    new = vid_enc()
    assert new is not enc and new.started == 1
    assert new.preset.max_h == 540 and new.preset.fps == 60
    await ws.close()
    await inp.close()


async def test_preset_drags_audio_until_the_user_takes_control(client):
    inp = await client.ws_connect("/ws/input")
    ahub = client.app["audio_hub"]

    await inp.send_json({"t": "q", "preset": "potato"})
    await wait_until(lambda: ahub.preset_name == "voice", what="audio follow the preset")
    assert ahub.manual is False

    # manual audio choice: from here on 'q' no longer touches the sound
    await inp.send_json({"t": "aq", "preset": "high"})
    await wait_until(lambda: ahub.preset_name == "high", what="manual audio")
    assert ahub.manual is True

    await inp.send_json({"t": "q", "preset": "potato"})
    await wait_until(lambda: client.app["hub"].preset_name == "potato",
                     what="video change")
    assert ahub.preset_name == "high", "manual audio must not be dragged along"
    await inp.close()


async def test_partial_vq_becomes_custom_and_shows_in_the_api(client):
    inp = await client.ws_connect("/ws/input")
    hub = client.app["hub"]
    res_before = hub.settings.max_h

    await inp.send_json({"t": "vq", "fps": 30})
    await wait_until(lambda: hub.settings.fps == 30, what="fps change")

    data = await (await client.get("/api/monitors")).json()
    assert data["video"] == {"res": res_before, "fps": 30, "bitrate": hub.settings.bitrate}
    assert data["current_preset"] == "custom"
    await inp.close()


async def test_monitor_change_restarts_with_the_new_region(client):
    ws = await client.ws_connect("/ws/video")
    inp = await client.ws_connect("/ws/input")
    enc = vid_enc()
    await wait_until(lambda: enc.started == 1, what="encoder start")
    assert enc.region["name"] == "HDMI-0", "without an explicit choice, it captures the primary"

    await inp.send_json({"t": "mon", "i": 1})
    await wait_until(lambda: vid_enc() is not enc, what="encoder restart")
    assert vid_enc().region["name"] == "DP-1"
    # the mouse geometry follows the captured region
    total, region = client.app["injector"].geometry
    assert total == (1920, 2160) and region["name"] == "DP-1"
    await ws.close()
    await inp.close()


# ───────────────────────────── audio ─────────────────────────────

async def test_audio_starts_with_a_listener_delivers_packets_and_stops(client):
    enc = aud_enc()
    assert enc.started == 0
    ws = await client.ws_connect("/ws/audio")
    await wait_until(lambda: enc.started == 1, what="audio start")

    await enc.push(b"opus-pkt-1")
    assert (await ws.receive(timeout=2)).data == b"opus-pkt-1"

    await ws.close()
    await wait_until(lambda: enc.stopped == 1, what="audio stop")


# ───────────────────────────── input ─────────────────────────────

async def test_input_reaches_the_injector(client):
    inp = await client.ws_connect("/ws/input")
    injector = client.app["injector"]
    await inp.send_json({"t": "k", "code": "KeyA", "down": True})
    await wait_until(lambda: injector.handled, what="key arrive")
    assert injector.handled[0] == {"t": "k", "code": "KeyA", "down": True}
    await inp.close()


GARBAGE = [
    "this is not json",
    "[]",                                   # valid JSON, but not a command
    "5",
    '"texto"',
    "null",
    '{"t": 42}',                            # unknown type
    '{"t": "m"}',                           # missing x/y
    '{"t": "m", "x": "abc", "y": null}',    # wrong types
    '{"t": "k"}',
    '{"t": "g", "a": "not-a-list", "b": 7}',
    '{"t": "vq", "res": "abc"}',            # invalid numbers
    '{"t": "vq", "res": [720]}',
    '{"t": "vq", "fps": {"a": 1}}',
    '{"t": "vq", "bitrate": "whatever"}',
    '{"t": "q", "preset": ["low"]}',        # unhashable
    '{"t": "q", "preset": null}',
    '{"t": "aq", "preset": {"x": 1}}',
    '{"t": "mon", "i": "x"}',
    '{"t": "mon", "i": [1]}',
]


@pytest.mark.parametrize("payload", GARBAGE)
async def test_input_survives_malformed_payload(client, payload):
    """No malformed message may bring down the client's input channel."""
    inp = await client.ws_connect("/ws/input")
    injector = client.app["injector"]
    await inp.send_str(payload)
    # the channel stays alive: the next (valid) command has to arrive
    await inp.send_json({"t": "k", "code": "KeyZ", "down": True})
    await wait_until(lambda: {"t": "k", "code": "KeyZ", "down": True} in injector.handled,
                     what=f"channel survive {payload!r}")
    assert not inp.closed
    await inp.close()


async def test_input_ignores_binary_and_keeps_going(client):
    inp = await client.ws_connect("/ws/input")
    injector = client.app["injector"]
    await inp.send_bytes(b"\x00\x01\x02")
    await inp.send_json({"t": "md", "b": 0})
    await wait_until(lambda: injector.handled, what="channel survive binary")
    assert injector.handled[-1] == {"t": "md", "b": 0}
    await inp.close()


async def test_input_survives_an_injector_that_blows_up(client):
    inp = await client.ws_connect("/ws/input")
    injector = client.app["injector"]
    injector.raise_on_handle = True
    await inp.send_json({"t": "k", "code": "KeyA", "down": True})
    await asyncio.sleep(0.05)
    injector.raise_on_handle = False
    await inp.send_json({"t": "k", "code": "KeyB", "down": True})
    await wait_until(lambda: injector.handled, what="channel survive the error")
    await inp.close()


async def test_absurd_vq_is_clamped_and_does_not_kill_the_encoder(client):
    """A client asking for 99999p / 5000fps must not make ffmpeg unusable."""
    inp = await client.ws_connect("/ws/input")
    hub = client.app["hub"]
    await inp.send_json({"t": "vq", "res": 999999, "fps": 5000, "bitrate": "99M"})
    await wait_until(lambda: hub.settings.max_h != 900, what="vq apply")
    assert hub.settings.max_h <= video.RES_MAX
    assert hub.settings.fps <= video.FPS_MAX
    assert hub.settings.bitrate == "99M"

    await inp.send_json({"t": "vq", "res": 1, "fps": 0})
    await wait_until(lambda: hub.settings.fps == video.FPS_MIN, what="minimum clamp")
    assert hub.settings.max_h == video.RES_MIN
    await inp.close()


# ─────────────────────────── HTTP / static ───────────────────────────

async def test_gamepad_debug_page_is_served(client):
    resp = await client.get("/gamepad-debug.html")
    assert resp.status == 200
    body = await resp.text()
    # language-neutral check: the page text itself is translated in web/
    assert "gamepad" in body.lower() and "<h1" in body


async def test_gamepad_map_full_cycle_with_real_persistence(client):
    profile = {"buttons": {"a": 2, "b": 1}, "axes": {"lx": 0},
              "triggers": {"lt": {"src": "axis", "i": 4, "min": 0, "max": -1}}}

    assert await (await client.get("/api/gamepad-map")).json() == {}

    resp = await client.post("/api/gamepad-map", json={"id": "Pad PS2", "map": profile})
    assert resp.status == 200

    saved = await (await client.get("/api/gamepad-map")).json()
    assert saved == {"Pad PS2": profile}, "the profile comes back identical (opaque blob)"

    resp = await client.delete("/api/gamepad-map", json={"id": "Pad PS2"})
    assert (await resp.json()) == {"ok": True, "removed": True}
    assert await (await client.get("/api/gamepad-map")).json() == {}

    # deleting again: it no longer exists, but it is not an error
    resp = await client.delete("/api/gamepad-map", json={"id": "Pad PS2"})
    assert (await resp.json())["removed"] is False


@pytest.mark.parametrize("body", ['"i-am-a-string"', "[1, 2]", "5", "null"])
async def test_gamepad_map_rejects_json_that_is_not_an_object(client, body):
    """Valid JSON that is not an object has to give 400 - it used to give 500 here."""
    header = {"Content-Type": "application/json"}
    for method in (client.post, client.delete):
        resp = await method("/api/gamepad-map", data=body, headers=header)
        assert resp.status == 400
        assert "error" in await resp.json()
    # the server is still up
    assert (await client.get("/api/monitors")).status == 200


async def test_api_monitors_matches_the_hub_state(client):
    data = await (await client.get("/api/monitors")).json()
    hub = client.app["hub"]
    assert data["current_preset"] == hub.preset_name
    assert data["video"]["res"] == hub.settings.max_h
    assert [m["name"] for m in data["monitors"]] == ["HDMI-0", "DP-1"]
    # the options offered to the UI have to be acceptable to the 'vq' channel
    for r in data["res_options"]:
        assert video.RES_MIN <= r <= video.RES_MAX
    for f in data["fps_options"]:
        assert video.FPS_MIN <= f <= video.FPS_MAX
    for b in data["bitrate_options"]:
        assert video.clean_bitrate(b, "!") == b
