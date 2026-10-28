"""
PROPERTY tests for the two incremental parsers (Ogg/Opus and H.264 Access
Units). They are the most fragile spot of the project: they are fed from a
pipe, so the bytes arrive chopped at arbitrary boundaries that change on every
run.

The core property of both is the same:

    the result MUST NOT depend on how the stream was chopped.

If it does, the symptom in production is video noise/choppy audio only
"sometimes" - the kind of bug that does not reproduce. Here we split the same
stream in 200 random ways (fixed seed) and demand identical output.
"""
import random

import pytest

from server import audio, video
from tests.helpers import FakeProcess, build_ogg_page, build_ogg_page_raw

SEEDS = [3, 11, 99, 2718, 31415]
AUD = b"\x00\x00\x01\x09"


def chunkify(data: bytes, rnd: random.Random, max_chunk=64) -> list[bytes]:
    """Splits `data` into random-sized chunks (as a pipe would)."""
    out, i = [], 0
    while i < len(data):
        n = rnd.randint(1, max_chunk)
        out.append(data[i:i + n])
        i += n
    return out


# ───────────────────────── Ogg / Opus ─────────────────────────

def opus_stream(packets: list[bytes]) -> bytes:
    """Full Ogg stream: the 2 headers + the audio packets (1 per page)."""
    data = build_ogg_page([b"OpusHead" + b"\x01" * 11], seqno=0)
    data += build_ogg_page([b"OpusTags" + b"\x00" * 8], seqno=1)
    for i, p in enumerate(packets):
        data += build_ogg_page([p], seqno=2 + i)
    return data


def parse_all(data_chunks) -> list[bytes]:
    parser = audio.OggOpusParser()
    out = []
    for c in data_chunks:
        out += parser.feed(c)
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_ogg_result_does_not_depend_on_chunking(seed):
    rnd = random.Random(seed)
    packets = [bytes([(i * 7 + j) % 251 + 1 for j in range(rnd.randint(1, 300))])
               for i in range(12)]
    stream = opus_stream(packets)

    expected = parse_all([stream])
    assert expected == packets, "all at once must already match"

    for _ in range(40):
        assert parse_all(chunkify(stream, rnd)) == packets


def test_ogg_byte_by_byte_equals_all_at_once():
    """Worst chunking case: 1 byte at a time."""
    packets = [b"\x01\x02\x03", b"\xaa" * 100, b"\xff"]
    stream = opus_stream(packets)
    assert parse_all([bytes([b]) for b in stream]) == packets


@pytest.mark.parametrize("seed", SEEDS)
def test_ogg_huge_packet_spans_pages(seed):
    """A packet > 255 bytes uses lacing 255 and may cross pages: it must come
    back whole, without joining/splitting in the wrong place."""
    rnd = random.Random(seed)
    big = bytes((i % 251) + 1 for i in range(1000))
    # 1st page ends in lacing 255 (the packet continues), the 2nd has the continuation
    part1, part2 = big[:765], big[765:]
    data = build_ogg_page([b"OpusHead" + b"\x01" * 11], seqno=0)
    data += build_ogg_page([b"OpusTags"], seqno=1)
    data += build_ogg_page_raw([255, 255, 255], part1, header_type=0, seqno=2)
    data += build_ogg_page_raw([len(part2)], part2, header_type=0x01, seqno=3)

    assert parse_all([data]) == [big]
    for _ in range(20):
        assert parse_all(chunkify(data, rnd)) == [big]


@pytest.mark.parametrize("seed", SEEDS)
def test_ogg_with_garbage_in_the_middle_does_not_raise_or_invent_packets(seed):
    rnd = random.Random(seed)
    stream = opus_stream([b"\x11" * 40, b"\x22" * 40])
    garbage = bytes(rnd.randrange(256) for _ in range(rnd.randint(1, 200)))
    for combined in (garbage + stream, stream + garbage, garbage + stream + garbage):
        packets = parse_all(chunkify(combined, rnd))
        # never raises; and whatever comes out cannot be empty (a ghost packet)
        assert all(len(p) > 0 for p in packets)


@pytest.mark.parametrize("seed", SEEDS)
def test_ogg_only_garbage_does_not_pile_up_memory(seed):
    """Without 'OggS' the buffer must be trimmed, otherwise a wrong stream eats RAM."""
    rnd = random.Random(seed)
    parser = audio.OggOpusParser()
    for _ in range(200):
        # garbage without the OggS signature
        blob = bytes(rnd.choice(b"XYZW\x00\x01") for _ in range(500))
        assert parser.feed(blob) == []
    assert len(parser._buf) <= 4, "the buffer grew with garbage"


def test_ogg_drops_exactly_the_two_headers():
    parser = audio.OggOpusParser()
    stream = opus_stream([b"\xab" * 10])
    assert parser.feed(stream) == [b"\xab" * 10]
    assert parser._headers_left == 0
    # after the headers, a packet that HAPPENS to start with "OpusHead" goes through
    parser2 = audio.OggOpusParser()
    parser2.feed(opus_stream([b"\x01"]))
    extra = build_ogg_page([b"OpusHead" + b"\x99"], seqno=9)
    assert parser2.feed(extra) == [b"OpusHead" + b"\x99"]


async def test_audio_encoder_delivers_the_same_packets_with_any_chunking():
    """End to end on the audio side: fake ffmpeg -> AudioHub callback."""
    packets = [b"\x31" * 60, b"\x32" * 80, b"\x33" * 20]
    stream = opus_stream(packets)
    rnd = random.Random(7)
    for _ in range(15):
        received = []

        async def on_pkt(p, received=received):
            received.append(p)

        enc = audio.AudioEncoder(on_pkt)
        enc.proc = FakeProcess(stdout_chunks=chunkify(stream, rnd))
        await enc._read_stream()
        assert received == packets


# ───────────────────────── Access Units H.264 ─────────────────────────

def make_au(kind: str, n: int) -> bytes:
    """Synthetic AU: AUD + (SPS if key) + slice. No 00 00 01 in the payload."""
    au = bytearray(AUD + b"\x10")
    if kind == "key":
        au += b"\x00\x00\x01\x67" + bytes([0x42, 0xE0, 0x1E])       # SPS
        au += b"\x00\x00\x01\x68" + bytes([0xCE, 0x3C, 0x80])       # PPS
        au += b"\x00\x00\x01\x65"                                    # IDR
    else:
        au += b"\x00\x00\x01\x41"                                    # slice P
    au += bytes(((i * 13) % 0xEF) + 0x10 for i in range(n))          # payload
    return bytes(au)


def split_reference(stream: bytes) -> list[bytes]:
    """Reference split (the last AU stays pending: the next AUD is missing)."""
    pos, marks = 0, []
    while True:
        i = stream.find(AUD, pos)
        if i < 0:
            break
        marks.append(i)
        pos = i + 4
    return [stream[marks[k]:marks[k + 1]] for k in range(len(marks) - 1)]


async def collect_aus(chunks) -> list[tuple[bool, bytes]]:
    output = []

    async def on_au(is_key, au):
        output.append((is_key, au))

    enc = video.ScreenEncoder(on_au)
    enc.proc = FakeProcess(stdout_chunks=list(chunks))
    await enc._read_stream()
    return output


@pytest.mark.parametrize("seed", SEEDS)
async def test_au_split_does_not_depend_on_chunking(seed):
    rnd = random.Random(seed)
    aus = [make_au("key" if i % 5 == 0 else "delta", rnd.randint(5, 400))
           for i in range(20)]
    stream = b"".join(aus)
    expected = split_reference(stream)
    assert expected == aus[:-1], "reference: the last AU waits for the next AUD"

    for _ in range(30):
        output = await collect_aus(chunkify(stream, rnd, max_chunk=128))
        assert [au for _, au in output] == expected


async def test_au_huge_chunk_all_at_once():
    aus = [make_au("key", 100), make_au("delta", 100), make_au("delta", 100)]
    stream = b"".join(aus)
    output = await collect_aus([stream])
    assert [au for _, au in output] == aus[:-1]


async def test_au_keyframe_flagged_correctly():
    aus = [make_au("key", 20), make_au("delta", 20), make_au("delta", 20),
           make_au("key", 20), make_au("delta", 20)]
    output = await collect_aus([b"".join(aus)])
    assert [k for k, _ in output] == [True, False, False, True]


async def test_au_garbage_before_the_first_aud_is_dropped():
    aus = [make_au("key", 30), make_au("delta", 30)]
    output = await collect_aus([b"\x99\x88\x77" + b"".join(aus)])
    assert [au for _, au in output] == [aus[0]]


@pytest.mark.parametrize("seed", SEEDS)
async def test_au_random_stream_does_not_raise(seed):
    """Pure noise instead of H.264: it must not blow up the reader."""
    rnd = random.Random(seed)
    garbage = bytes(rnd.randrange(256) for _ in range(4000))
    output = await collect_aus(chunkify(garbage, rnd))
    for is_key, au in output:
        assert au.startswith(AUD) and isinstance(is_key, bool)


@pytest.mark.parametrize("seed", SEEDS)
def test_is_keyframe_never_raises_on_random_bytes(seed):
    rnd = random.Random(seed)
    for _ in range(400):
        blob = bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 60)))
        assert isinstance(video._is_keyframe(blob), bool)


def test_is_keyframe_start_code_truncated_at_the_end():
    """AU ending exactly on the start code: it must not read past the buffer."""
    assert video._is_keyframe(b"\x00\x00\x01") is False
    assert video._is_keyframe(b"\xff\x00\x00\x01") is False
    assert video._is_keyframe(b"") is False
