import asyncio

from server import audio
from tests.helpers import FakeProcess, build_ogg_page, build_ogg_page_raw, make_create_subprocess

# ───────────────────────── Constants / dataclass ─────────────────────────

def test_audio_presets():
    assert audio.AUDIO_PRESETS["voice"].application == "voip"


# ───────────────────────── default_monitor ─────────────────────────

def test_default_monitor_forced(monkeypatch):
    monkeypatch.setenv("LRD_AUDIO_SOURCE", "mysrc")
    assert audio.default_monitor() == "mysrc"


def test_default_monitor_token(monkeypatch):
    monkeypatch.delenv("LRD_AUDIO_SOURCE", raising=False)
    assert audio.default_monitor() == audio.DEFAULT_MONITOR_TOKEN


# ───────────────────────── list_monitors ─────────────────────────

def test_list_monitors_parse(monkeypatch):
    ffmpeg_output = (
        "Auto-detected sources for pulse:\n"  # header line: not a source, skipped
        "  * alsa_output.pci.analog-stereo.monitor [Monitor of Built-in Audio Stereo]\n"
        "  alsa_output.pci.hdmi-stereo.monitor [Monitor of HDMI Stereo]\n"
        "  alsa_input.pci.analog-stereo [Built-in Input]\n"
    )
    fake = type("R", (), {"stdout": "", "stderr": ffmpeg_output})()
    monkeypatch.setattr(audio.subprocess, "run", lambda *a, **kw: fake)
    mons = audio.list_monitors()
    assert len(mons) == 2
    names = [m["name"] for m in mons]
    assert "alsa_output.pci.analog-stereo.monitor" in names


def test_list_monitors_exception(monkeypatch):
    monkeypatch.setattr(audio.subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    assert audio.list_monitors() == []


# ───────────────────────── audio_available ─────────────────────────

def test_available_forced(monkeypatch):
    monkeypatch.setenv("LRD_AUDIO_SOURCE", "x")
    assert audio.audio_available() is True


def test_available_without_ffmpeg(monkeypatch):
    monkeypatch.delenv("LRD_AUDIO_SOURCE", raising=False)
    monkeypatch.setattr(audio.shutil, "which", lambda *_: None)
    assert audio.audio_available() is False


def test_available_with_monitor(monkeypatch):
    monkeypatch.delenv("LRD_AUDIO_SOURCE", raising=False)
    monkeypatch.setattr(audio.shutil, "which", lambda *_: "/usr/bin/ffmpeg")
    monkeypatch.setattr(audio, "list_monitors", lambda: [{"name": "m", "desc": "d"}])
    assert audio.audio_available() is True


def test_available_without_monitor(monkeypatch):
    monkeypatch.delenv("LRD_AUDIO_SOURCE", raising=False)
    monkeypatch.setattr(audio.shutil, "which", lambda *_: "/usr/bin/ffmpeg")
    monkeypatch.setattr(audio, "list_monitors", lambda: [])
    assert audio.audio_available() is False


# ───────────────────────── OggOpusParser ─────────────────────────

def test_ogg_headers_dropped_and_packet():
    parser = audio.OggOpusParser()
    page1 = build_ogg_page([b"OpusHead\x01\x02\x00\x38\x80\xbb\x00\x00\x00\x00\x00"],
                            header_type=2, serial=1, seqno=0)
    page2 = build_ogg_page([b"OpusTags\x00\x00\x00\x00\x00\x00\x00"],
                            header_type=0, serial=1, seqno=1)
    page3 = build_ogg_page([b"DATA"], header_type=0, serial=1, seqno=2)

    all_data = page1 + page2 + page3
    packets = parser.feed(all_data)
    assert packets == [b"DATA"]


def test_ogg_packet_spans_pages():
    # 300-byte packet: first 255 on page 1, the rest (45) on page 2
    data1 = b"A" * 255
    data2 = b"B" * 45
    full_packet = data1 + data2

    # Page 1: lacing [255] (not terminated - the packet continues)
    page1 = build_ogg_page_raw([255], data1, header_type=0, serial=1, seqno=0)
    # Page 2: continuation (bit 0 set), lacing [45] (ends it)
    page2 = build_ogg_page_raw([45], data2, header_type=0x01, serial=1, seqno=1)

    parser = audio.OggOpusParser()
    # Skip the 2 headers first
    parser._headers_left = 0

    packets = parser.feed(page1 + page2)
    assert len(packets) == 1
    assert packets[0] == full_packet


def test_ogg_reset_orphan():
    parser = audio.OggOpusParser()
    parser._headers_left = 0

    # Page 1: lacing [255] - a 255-byte partial is accumulated
    page1 = build_ogg_page_raw([255], b"X" * 255, header_type=0, serial=1, seqno=0)
    # Page 2: NOT a continuation (header_type=0) - the partial must be reset
    page2 = build_ogg_page([b"hello"], header_type=0, serial=1, seqno=1)

    packets = parser.feed(page1 + page2)
    # The 255 orphan bytes are dropped; only "hello" comes back
    assert packets == [b"hello"]


def test_ogg_incomplete_buffer():
    parser = audio.OggOpusParser()
    parser._headers_left = 0

    page = build_ogg_page([b"data"], header_type=0, serial=1, seqno=0)
    # Feed only half
    half = len(page) // 2
    packets1 = parser.feed(page[:half])
    assert packets1 == []
    # Feed the rest
    packets2 = parser.feed(page[half:])
    assert packets2 == [b"data"]


def test_ogg_garbage_before_oggs():
    parser = audio.OggOpusParser()
    parser._headers_left = 0

    page = build_ogg_page([b"ok"], header_type=0, serial=1, seqno=0)
    packets = parser.feed(b"GARBAGE_FIRST" + page)
    assert packets == [b"ok"]


def test_ogg_without_oggs_trims():
    parser = audio.OggOpusParser()
    # No "OggS" and more than 4 bytes - must keep only the tail
    parser.feed(b"X" * 100)
    assert len(parser._buf) <= 4


def test_ogg_take_packet_empty():
    parser = audio.OggOpusParser()
    parser._headers_left = 0

    # Page with lacing 0 -> empty packet -> ignored
    page = build_ogg_page_raw([0], b"", header_type=0, serial=1, seqno=0)
    packets = parser.feed(page)
    assert packets == []


def test_ogg_next_page_incomplete_header():
    # len(buf) >= 27 but < header_len -> the early return None
    parser = audio.OggOpusParser()
    nsegs = 3
    header = b"OggS" + bytes([0, 0]) + bytes(8) + bytes(4) + bytes(4) + bytes(4)
    header += bytes([nsegs])  # 27 bytes total
    partial_seg_table = bytes([10])  # only 1 byte, nsegs=3 needs 3
    result = parser.feed(header + partial_seg_table)
    assert result == []


def test_ogg_next_page_incomplete_body():
    # header_len ok but total > len(buf) -> the second return None
    parser = audio.OggOpusParser()
    header = b"OggS" + bytes([0, 0]) + bytes(8) + bytes(4) + bytes(4) + bytes(4)
    header += bytes([1])  # nsegs=1
    seg_table = bytes([100])  # body must have 100 bytes
    incomplete_body = b"x" * 50
    result = parser.feed(header + seg_table + incomplete_body)
    assert result == []


def test_ogg_take_packet_rare_after_headers():
    # _headers_left > 0 but the packet does not start with OpusHead/Tags
    parser = audio.OggOpusParser()
    assert parser._headers_left == 2

    page = build_ogg_page([b"NOT_OPUS_DATA"], header_type=0, serial=1, seqno=0)
    packets = parser.feed(page)
    assert len(packets) == 1
    assert packets[0] == b"NOT_OPUS_DATA"
    assert parser._headers_left == 0


# ───────────────────────── AudioEncoder ─────────────────────────

def test_audio_build_cmd():
    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)
    cmd = enc._build_cmd(audio.AUDIO_PRESETS["medium"])
    assert "libopus" in cmd
    assert "-page_duration" in cmd
    assert "20000" in cmd
    assert enc.source in cmd


async def test_audio_read_stream():
    # Pages with the headers and 1 data packet
    page_head = build_ogg_page([b"OpusHead\x01\x02\x00\x38\x80\xbb\x00\x00"],
                                header_type=2, serial=1, seqno=0)
    page_tags = build_ogg_page([b"OpusTags\x00\x00\x00\x00\x00\x00\x00"],
                                header_type=0, serial=1, seqno=1)
    page_data = build_ogg_page([b"OPUS_PKT"], header_type=0, serial=1, seqno=2)
    ogg_bytes = page_head + page_tags + page_data

    received = []

    async def on_packet(pkt):
        received.append(pkt)

    enc = audio.AudioEncoder(on_packet)
    enc.proc = FakeProcess(stdout_chunks=[ogg_bytes])
    enc._stop = False
    await enc._read_stream()
    assert received == [b"OPUS_PKT"]


async def test_audio_drain_stderr(capsys):
    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)
    enc.proc = FakeProcess(stderr_lines=[b"audio err\n", b"  \n"])
    await enc._drain_stderr()
    out = capsys.readouterr().out
    assert "audio err" in out


async def test_audio_drain_stderr_without_proc():
    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)
    enc.proc = None
    await enc._drain_stderr()


async def test_audio_stop_without_proc():
    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)
    enc.proc = None
    enc._task = None
    await enc.stop()


async def test_audio_stop_terminates():
    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)
    proc = FakeProcess()
    proc.returncode = None
    enc.proc = proc

    async def done():
        pass

    task = asyncio.create_task(done())
    await task
    enc._task = task
    await enc.stop()
    assert proc.terminated


async def test_audio_stop_terminate_and_kill_fail():
    # kill fails too -> covers the inner except/pass
    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)
    proc = FakeProcess(terminate_raises=True)
    proc.returncode = None

    def kill_raises():
        raise OSError("kill boom")

    proc.kill = kill_raises
    enc.proc = proc
    enc._task = None
    await enc.stop()


async def test_audio_stop_task_raises():
    # await self._task raises -> covers the except around it
    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)
    enc.proc = None

    async def bad_coro():
        raise RuntimeError("task error")

    task = asyncio.create_task(bad_coro())
    await asyncio.sleep(0)
    enc._task = task
    await enc.stop()


async def test_audio_drain_stderr_exception():
    # async for raises -> covers the except in _drain_stderr
    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)

    class ErrorStream:
        def __aiter__(self): return self
        async def __anext__(self): raise RuntimeError("stderr boom")

    from unittest import mock
    proc_mock = mock.MagicMock()
    proc_mock.stderr = ErrorStream()
    enc.proc = proc_mock
    await enc._drain_stderr()


async def test_audio_read_stream_immediate_stop():
    # _stop=True before the loop -> the while exits right away
    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)
    enc.proc = FakeProcess(stdout_chunks=[b"data"])
    enc._stop = True
    await enc._read_stream()


async def test_audio_supervise_stop_during_sleep(monkeypatch):
    # _stop set during the sleep -> while condition False -> exit
    fake_cse = make_create_subprocess([FakeProcess()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_cse)

    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)

    async def stop_during_sleep(d):
        enc._stop = True

    monkeypatch.setattr(asyncio, "sleep", stop_during_sleep)
    enc.start(audio.AUDIO_PRESETS["medium"])
    await enc._task
    assert enc._stop is True


async def test_audio_stop_terminate_fails_kills():
    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)
    proc = FakeProcess(terminate_raises=True)
    proc.returncode = None
    enc.proc = proc
    enc._task = None
    await enc.stop()
    assert proc.killed


async def test_audio_supervise_limit(monkeypatch):
    fake_cse = make_create_subprocess([FakeProcess()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_cse)

    async def fake_sleep(d):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)
    enc.start(audio.AUDIO_PRESETS["medium"])
    await enc._task

    assert fake_cse.calls["n"] >= 6


async def test_audio_supervise_stop(monkeypatch):
    fake_cse = make_create_subprocess([FakeProcess()])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_cse)

    async def fake_sleep(d):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def noop(pkt):
        pass

    enc = audio.AudioEncoder(noop)

    async def stop_now():
        enc._stop = True

    enc._read_stream = stop_now
    enc.start(audio.AUDIO_PRESETS["medium"])
    await enc._task

    assert enc._stop is True
    assert fake_cse.calls["n"] == 1
