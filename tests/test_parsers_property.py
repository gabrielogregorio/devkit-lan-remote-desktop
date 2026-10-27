"""
Testes de PROPRIEDADE dos dois parsers incrementais (Ogg/Opus e Access Units
H.264). São o ponto mais frágil do projeto: alimentam-se de um pipe, então os
bytes chegam picotados em fronteiras arbitrárias, que mudam a cada execução.

A propriedade central dos dois é a mesma:

    o resultado NÃO PODE depender de como o stream foi picotado.

Se depender, o sintoma em produção é chuvisco/áudio picotado só "às vezes" -
o tipo de bug que não reproduz. Aqui a gente reparte o mesmo stream de 200
jeitos aleatórios (semente fixa) e exige saída idêntica.
"""
import random

import pytest

from server import audio, video
from tests.helpers import FakeProcess, build_ogg_page, build_ogg_page_raw

SEEDS = [3, 11, 99, 2718, 31415]
AUD = b"\x00\x00\x01\x09"


def chunkify(data: bytes, rnd: random.Random, max_chunk=64) -> list[bytes]:
    """Reparte `data` em pedaços de tamanho aleatório (como um pipe faria)."""
    out, i = [], 0
    while i < len(data):
        n = rnd.randint(1, max_chunk)
        out.append(data[i:i + n])
        i += n
    return out


# ───────────────────────── Ogg / Opus ─────────────────────────

def opus_stream(packets: list[bytes]) -> bytes:
    """Stream Ogg completo: os 2 headers + os pacotes de áudio (1 por página)."""
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
def test_ogg_resultado_independe_do_picote(seed):
    rnd = random.Random(seed)
    pacotes = [bytes([(i * 7 + j) % 251 + 1 for j in range(rnd.randint(1, 300))])
               for i in range(12)]
    stream = opus_stream(pacotes)

    esperado = parse_all([stream])
    assert esperado == pacotes, "de uma vez só já tem que bater"

    for _ in range(40):
        assert parse_all(chunkify(stream, rnd)) == pacotes


def test_ogg_byte_a_byte_e_igual_a_de_uma_vez():
    """Pior caso do picote: 1 byte por vez."""
    pacotes = [b"\x01\x02\x03", b"\xaa" * 100, b"\xff"]
    stream = opus_stream(pacotes)
    assert parse_all([bytes([b]) for b in stream]) == pacotes


@pytest.mark.parametrize("seed", SEEDS)
def test_ogg_pacote_gigante_atravessa_paginas(seed):
    """Pacote > 255 bytes usa lacing 255 e pode cruzar páginas: tem que voltar
    inteiro, sem juntar/quebrar no lugar errado."""
    rnd = random.Random(seed)
    grande = bytes((i % 251) + 1 for i in range(1000))
    # 1ª página termina em lacing 255 (pacote continua), 2ª tem a continuação
    parte1, parte2 = grande[:765], grande[765:]
    data = build_ogg_page([b"OpusHead" + b"\x01" * 11], seqno=0)
    data += build_ogg_page([b"OpusTags"], seqno=1)
    data += build_ogg_page_raw([255, 255, 255], parte1, header_type=0, seqno=2)
    data += build_ogg_page_raw([len(parte2)], parte2, header_type=0x01, seqno=3)

    assert parse_all([data]) == [grande]
    for _ in range(20):
        assert parse_all(chunkify(data, rnd)) == [grande]


@pytest.mark.parametrize("seed", SEEDS)
def test_ogg_com_lixo_no_meio_nao_levanta_e_nao_inventa_pacote(seed):
    rnd = random.Random(seed)
    stream = opus_stream([b"\x11" * 40, b"\x22" * 40])
    lixo = bytes(rnd.randrange(256) for _ in range(rnd.randint(1, 200)))
    for combinado in (lixo + stream, stream + lixo, lixo + stream + lixo):
        pacotes = parse_all(chunkify(combinado, rnd))
        # nunca levanta; e o que sair não pode ser vazio (pacote fantasma)
        assert all(len(p) > 0 for p in pacotes)


@pytest.mark.parametrize("seed", SEEDS)
def test_ogg_so_lixo_nao_acumula_memoria(seed):
    """Sem 'OggS' o buffer tem que ser podado, senão um stream errado come RAM."""
    rnd = random.Random(seed)
    parser = audio.OggOpusParser()
    for _ in range(200):
        # lixo sem a assinatura OggS
        blob = bytes(rnd.choice(b"XYZW\x00\x01") for _ in range(500))
        assert parser.feed(blob) == []
    assert len(parser._buf) <= 4, "buffer cresceu com lixo"


def test_ogg_descarta_exatamente_os_dois_headers():
    parser = audio.OggOpusParser()
    stream = opus_stream([b"\xab" * 10])
    assert parser.feed(stream) == [b"\xab" * 10]
    assert parser._headers_left == 0
    # depois dos headers, um pacote que POR ACASO comece com "OpusHead" passa
    parser2 = audio.OggOpusParser()
    parser2.feed(opus_stream([b"\x01"]))
    extra = build_ogg_page([b"OpusHead" + b"\x99"], seqno=9)
    assert parser2.feed(extra) == [b"OpusHead" + b"\x99"]


async def test_audio_encoder_entrega_os_mesmos_pacotes_em_qualquer_picote():
    """Ponta a ponta do lado do áudio: ffmpeg falso -> callback do AudioHub."""
    pacotes = [b"\x31" * 60, b"\x32" * 80, b"\x33" * 20]
    stream = opus_stream(pacotes)
    rnd = random.Random(7)
    for _ in range(15):
        recebidos = []

        async def on_pkt(p):
            recebidos.append(p)

        enc = audio.AudioEncoder(on_pkt)
        enc.proc = FakeProcess(stdout_chunks=chunkify(stream, rnd))
        await enc._read_stream()
        assert recebidos == pacotes


# ───────────────────────── Access Units H.264 ─────────────────────────

def make_au(kind: str, n: int) -> bytes:
    """AU sintética: AUD + (SPS se key) + slice. Sem 00 00 01 no payload."""
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
    """Fatiamento de referência (o último AU fica pendente: falta o próximo AUD)."""
    pos, marcas = 0, []
    while True:
        i = stream.find(AUD, pos)
        if i < 0:
            break
        marcas.append(i)
        pos = i + 4
    return [stream[marcas[k]:marcas[k + 1]] for k in range(len(marcas) - 1)]


async def collect_aus(chunks) -> list[tuple[bool, bytes]]:
    saida = []

    async def on_au(is_key, au):
        saida.append((is_key, au))

    enc = video.ScreenEncoder(on_au)
    enc.proc = FakeProcess(stdout_chunks=list(chunks))
    await enc._read_stream()
    return saida


@pytest.mark.parametrize("seed", SEEDS)
async def test_au_fatiamento_independe_do_picote(seed):
    rnd = random.Random(seed)
    aus = [make_au("key" if i % 5 == 0 else "delta", rnd.randint(5, 400))
           for i in range(20)]
    stream = b"".join(aus)
    esperado = split_reference(stream)
    assert esperado == aus[:-1], "referência: o último AU fica esperando o próximo AUD"

    for _ in range(30):
        saida = await collect_aus(chunkify(stream, rnd, max_chunk=128))
        assert [au for _, au in saida] == esperado


async def test_au_chunk_gigante_de_uma_vez_so():
    aus = [make_au("key", 100), make_au("delta", 100), make_au("delta", 100)]
    stream = b"".join(aus)
    saida = await collect_aus([stream])
    assert [au for _, au in saida] == aus[:-1]


async def test_au_keyframe_marcado_corretamente():
    aus = [make_au("key", 20), make_au("delta", 20), make_au("delta", 20),
           make_au("key", 20), make_au("delta", 20)]
    saida = await collect_aus([b"".join(aus)])
    assert [k for k, _ in saida] == [True, False, False, True]


async def test_au_lixo_antes_do_primeiro_aud_e_descartado():
    aus = [make_au("key", 30), make_au("delta", 30)]
    saida = await collect_aus([b"\x99\x88\x77" + b"".join(aus)])
    assert [au for _, au in saida] == [aus[0]]


@pytest.mark.parametrize("seed", SEEDS)
async def test_au_stream_aleatorio_nao_levanta(seed):
    """Ruído puro no lugar do H.264: não pode explodir o leitor."""
    rnd = random.Random(seed)
    lixo = bytes(rnd.randrange(256) for _ in range(4000))
    saida = await collect_aus(chunkify(lixo, rnd))
    for is_key, au in saida:
        assert au.startswith(AUD) and isinstance(is_key, bool)


@pytest.mark.parametrize("seed", SEEDS)
def test_is_keyframe_nunca_levanta_com_bytes_aleatorios(seed):
    rnd = random.Random(seed)
    for _ in range(400):
        blob = bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 60)))
        assert isinstance(video._is_keyframe(blob), bool)


def test_is_keyframe_start_code_truncado_no_fim():
    """AU terminando exatamente no start code: não pode ler fora do buffer."""
    assert video._is_keyframe(b"\x00\x00\x01") is False
    assert video._is_keyframe(b"\xff\x00\x00\x01") is False
    assert video._is_keyframe(b"") is False
