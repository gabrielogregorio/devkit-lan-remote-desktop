"""Fakes determinísticos para evitar uinput/ffmpeg/proc reais nos testes."""
import asyncio


# ----------------------- subprocess assíncrono falso -----------------------
class FakeStream:
    def __init__(self, chunks=None, lines=None):
        self._chunks = list(chunks or [])
        self._lines = list(lines or [])

    async def read(self, n=-1):
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def __aiter__(self):
        self._it = iter(self._lines)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeProcess:
    def __init__(self, stdout_chunks=None, stderr_lines=None,
                 terminate_raises=False):
        self.stdout = FakeStream(chunks=stdout_chunks)
        self.stderr = FakeStream(lines=stderr_lines)
        self.returncode = None
        self._terminate_raises = terminate_raises
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        if self._terminate_raises:
            raise OSError("boom")
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.returncode = self.returncode or 0
        return self.returncode


def make_create_subprocess(processes):
    seq = list(processes)
    calls = {"n": 0}

    async def _fake(*args, **kwargs):
        calls["n"] += 1
        if seq:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return FakeProcess()
    _fake.calls = calls
    return _fake


# ----------------------------- UInput falso -----------------------------
class FakeUInput:
    instances = []

    def __init__(self, *args, close_raises=False, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.writes = []
        self.syns = 0
        self.closed = False
        self._close_raises = close_raises
        FakeUInput.instances.append(self)

    def write(self, etype, code, value):
        self.writes.append((etype, code, value))

    def syn(self):
        self.syns += 1

    def close(self):
        if self._close_raises:
            raise OSError("close boom")
        self.closed = True


# ----------------------------- páginas Ogg -----------------------------
def build_ogg_page(segments, header_type=0, serial=1, seqno=0):
    """Monta UMA página Ogg válida. `segments`: lista de bytes completos."""
    seg_table = bytearray()
    body = bytearray()
    for seg in segments:
        n = len(seg)
        while n >= 255:
            seg_table.append(255)
            n -= 255
        seg_table.append(n)
        body += seg
    page = bytearray()
    page += b"OggS"
    page += bytes([0])
    page += bytes([header_type])
    page += bytes(8)
    page += serial.to_bytes(4, "little")
    page += seqno.to_bytes(4, "little")
    page += bytes(4)
    page += bytes([len(seg_table)])
    page += bytes(seg_table)
    page += bytes(body)
    return bytes(page)


def build_ogg_page_raw(seg_table_bytes, body, header_type=0, serial=1, seqno=0):
    """Monta página Ogg com seg_table explícita (permite lacing=255 no fim)."""
    seg_table_bytes = bytearray(seg_table_bytes)
    page = bytearray()
    page += b"OggS"
    page += bytes([0, header_type])
    page += bytes(8)
    page += serial.to_bytes(4, "little")
    page += seqno.to_bytes(4, "little")
    page += bytes(4)
    page += bytes([len(seg_table_bytes)])
    page += bytes(seg_table_bytes)
    page += bytes(body)
    return bytes(page)


# ----------------------------- /proc falso (Linux) -----------------------------
def fake_proc(monkeypatch, guardian, procs):
    """Faz guardian._candidates_linux ler um /proc em memória."""
    import os as _os

    monkeypatch.setattr(guardian.os, "listdir", lambda p: list(procs.keys()))

    def fake_readlink(path):
        pid = path.split("/")[2]
        exe = procs.get(pid, {}).get("exe")
        if exe is None:
            raise OSError("no exe")
        return exe
    monkeypatch.setattr(guardian.os, "readlink", fake_readlink)

    class FakeFile:
        def __init__(self, data): self._data = data
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._data

    def fake_open(path, mode="rb"):
        parts = path.split("/")
        pid, name = parts[2], parts[3]
        rec = procs.get(pid, {})
        if name == "comm":
            v = rec.get("comm")
            if v is None: raise OSError("no comm")
            return FakeFile(v.encode())
        if name == "cmdline":
            cl = rec.get("cmdline")
            if cl is None: raise OSError("no cmdline")
            return FakeFile(("\x00".join(cl) + "\x00").encode())
        raise OSError("unknown")

    monkeypatch.setattr(guardian, "open", fake_open, raising=False)
