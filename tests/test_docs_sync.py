"""
Guarda anti-drift da documentação.

Doc que descreve um código que não existe mais é pior que doc nenhuma: o
próximo agente (ou o próprio autor daqui a 3 meses) acredita nela. Estes testes
extraem do CÓDIGO a lista de rotas, tipos de mensagem, presets e variáveis
`RL_*`, e exigem que cada item apareça na documentação.

Não checa redação - checa existência. Se falhar, quase sempre é a doc que ficou
para trás, não o teste.
"""
import ast
import re
from pathlib import Path

import pytest

from server import audio, main as M, video

RAIZ = Path(__file__).resolve().parent.parent
CLAUDE_MD = (RAIZ / "CLAUDE.md").read_text(encoding="utf-8")
README_MD = (RAIZ / "README.md").read_text(encoding="utf-8")
TESTING_MD = (RAIZ / "TESTING.md").read_text(encoding="utf-8")
ANTIBAN_MD = (RAIZ / "ANTIBAN.md").read_text(encoding="utf-8")
TODA_DOC = CLAUDE_MD + README_MD + TESTING_MD + ANTIBAN_MD


def _fonte(*partes) -> str:
    return (RAIZ.joinpath(*partes)).read_text(encoding="utf-8")


# ───────────────────────── rotas HTTP/WS ─────────────────────────

def rotas_registradas() -> set[str]:
    """Extrai os caminhos de `app.router.add_*(...)` do build_app()."""
    arvore = ast.parse(_fonte("server", "main.py"))
    rotas = set()
    for no in ast.walk(arvore):
        if (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)
                and no.func.attr.startswith("add_") and no.args
                and isinstance(no.args[0], ast.Constant)
                and isinstance(no.args[0].value, str)):
            rotas.add(no.args[0].value)
    return rotas


def test_toda_rota_esta_documentada():
    rotas = rotas_registradas()
    assert rotas, "não achei rota nenhuma - o parser quebrou?"
    faltando = [r for r in rotas
                if r not in ("/", "/api/gamepad-map")  # descritas por extenso
                and r not in CLAUDE_MD]
    assert not faltando, f"rotas sem documentação em CLAUDE.md: {faltando}"
    # as duas com tratamento especial também precisam aparecer
    assert "/api/gamepad-map" in CLAUDE_MD
    assert "/api/monitors" in CLAUDE_MD


def test_rotas_documentadas_existem_de_fato():
    """O contrário: a doc não pode prometer endpoint que não existe."""
    rotas = rotas_registradas()
    prometidas = set(re.findall(r"`(/(?:api|ws)/[a-z\-/]+)`", CLAUDE_MD))
    inexistentes = [r for r in prometidas if r not in rotas]
    assert not inexistentes, f"CLAUDE.md promete rota que não existe: {inexistentes}"


def test_pagina_de_debug_documentada_existe():
    assert (RAIZ / "web" / "gamepad-debug.html").exists()
    assert "gamepad-debug.html" in CLAUDE_MD
    assert "gamepad-debug.html" in README_MD


# ───────────────────── tipos de mensagem do /ws/input ─────────────────────

def tipos_tratados() -> set[str]:
    """Tipos aceitos: os do dispatch do ws_input + os do Injector.handle."""
    fonte = _fonte("server", "main.py") + _fonte("server", "inject.py")
    return set(re.findall(r'[t]\s*==\s*"(\w+)"', fonte))


def test_todo_tipo_de_mensagem_esta_documentado():
    faltando = [t for t in tipos_tratados()
                if f'"t":"{t}"' not in CLAUDE_MD.replace(" ", "")]
    assert not faltando, (
        f"tipos aceitos pelo servidor e ausentes do protocolo em CLAUDE.md: "
        f"{sorted(faltando)}")


def test_doc_nao_promete_tipo_que_o_servidor_ignora():
    bloco = CLAUDE_MD.split("`/ws/input`")[1].split("## ")[0]
    documentados = set(re.findall(r'\{"t"\s*:\s*"(\w+)"', bloco))
    assert documentados, "não achei o protocolo documentado"
    fantasmas = documentados - tipos_tratados()
    assert not fantasmas, f"CLAUDE.md documenta tipo inexistente: {sorted(fantasmas)}"


def test_cliente_so_envia_tipos_que_o_servidor_trata():
    """A ponta a ponta do contrato: o que o app.js manda, o servidor entende."""
    enviados = set(re.findall(r'sendInput\(\{\s*t:\s*"(\w+)"', _fonte("web", "app.js")))
    assert enviados, "não achei sendInput no cliente"
    assert enviados <= tipos_tratados(), (
        f"cliente envia tipo que o servidor ignora: "
        f"{sorted(enviados - tipos_tratados())}")


# ───────────────────────── presets e faixas ─────────────────────────

@pytest.mark.parametrize("preset", list(video.PRESETS))
def test_preset_de_video_documentado(preset):
    assert preset in CLAUDE_MD, f"preset de vídeo '{preset}' não citado no CLAUDE.md"
    assert preset in README_MD, f"preset de vídeo '{preset}' não citado no README"


@pytest.mark.parametrize("preset", list(audio.AUDIO_PRESETS))
def test_preset_de_audio_documentado(preset):
    assert preset in TODA_DOC, f"preset de áudio '{preset}' não documentado"


def test_tabela_do_readme_bate_com_os_presets_reais():
    """Cada linha da tabela de qualidade tem que refletir o PRESETS de verdade."""
    for nome, p in video.PRESETS.items():
        linha = next((l for l in README_MD.splitlines()
                      if l.strip().startswith(f"| {nome} ")), None)
        assert linha, f"preset '{nome}' sem linha na tabela do README"
        assert f"{p.max_h}p" in linha, f"resolução errada na linha de '{nome}': {linha}"
        assert f"| {p.fps} " in linha, f"fps errado na linha de '{nome}': {linha}"


def test_mapa_de_audio_por_qualidade_e_valido():
    """AUDIO_FOR_QUALITY tem que casar presets reais dos dois lados."""
    assert set(M.AUDIO_FOR_QUALITY) == set(video.PRESETS)
    assert set(M.AUDIO_FOR_QUALITY.values()) <= set(audio.AUDIO_PRESETS)


def test_faixas_de_saneamento_documentadas():
    """As faixas do 'vq' estão na doc com os números do código."""
    assert f"{video.RES_MIN}" in CLAUDE_MD and f"{video.RES_MAX}" in CLAUDE_MD
    assert f"{video.FPS_MIN}" in CLAUDE_MD and f"{video.FPS_MAX}" in CLAUDE_MD


def test_opcoes_oferecidas_pela_api_sao_aceitas_pelo_saneador():
    """Coerência interna: a UI não pode oferecer valor que o servidor rejeita."""
    for r in [360, 480, 540, 720, 900, 1080]:
        assert video.clamp_res(r, -1) == r
    for f in [30, 45, 60, 90, 120]:
        assert video.clamp_fps(f, -1) == f
    for b in ["1200k", "2500k", "3500k", "7M", "12M", "25M"]:
        assert video.clean_bitrate(b, "!") == b


# ───────────────────────── variáveis de ambiente ─────────────────────────

def envs_usadas() -> set[str]:
    fontes = "".join(_fonte("server", f.name) for f in (RAIZ / "server").glob("*.py"))
    fontes += _fonte("remote-link.sh")
    return set(re.findall(r"\bRL_[A-Z_]+\b", fontes))


def test_toda_variavel_rl_esta_documentada():
    faltando = sorted(v for v in envs_usadas() if v not in TODA_DOC)
    assert not faltando, f"variáveis usadas no código e não documentadas: {faltando}"


def test_claude_md_documenta_as_variaveis_do_servidor():
    """As que mudam comportamento em runtime têm que estar no guia do agente."""
    faltando = sorted(v for v in envs_usadas() if v not in CLAUDE_MD)
    assert not faltando, f"faltando na tabela do CLAUDE.md: {faltando}"


def test_doc_nao_inventa_variavel():
    documentadas = set(re.findall(r"\bRL_[A-Z_]+\b", CLAUDE_MD + README_MD))
    fantasmas = sorted(documentadas - envs_usadas())
    assert not fantasmas, f"doc cita variável que o código não lê: {fantasmas}"


# ───────────────────────── estrutura dos arquivos ─────────────────────────

@pytest.mark.parametrize("doc", ["CLAUDE.md", "README.md", "TESTING.md", "ANTIBAN.md"])
def test_documentos_existem_e_nao_estao_vazios(doc):
    texto = (RAIZ / doc).read_text(encoding="utf-8")
    assert len(texto) > 500, f"{doc} está vazio ou raso demais"


def test_layout_do_claude_md_cita_todo_modulo_do_servidor():
    for py in (RAIZ / "server").glob("*.py"):
        if py.name == "__init__.py":
            continue
        assert py.name in CLAUDE_MD, f"módulo {py.name} ausente do layout no CLAUDE.md"


def test_todo_arquivo_de_teste_esta_no_testing_md():
    arquivos = [p.name for p in (RAIZ / "tests").glob("test_*.py")]
    arquivos += [p.name for p in (RAIZ / "web" / "__tests__").glob("*.test.js")]
    arquivos += [p.name for p in (RAIZ / "web" / "e2e").glob("*.spec.js")]
    faltando = sorted(a for a in arquivos if a not in TESTING_MD)
    assert not faltando, f"arquivos de teste não descritos no TESTING.md: {faltando}"


def test_comandos_de_teste_do_readme_batem_com_o_package_json():
    import json
    scripts = json.loads(_fonte("package.json"))["scripts"]
    for nome in scripts:
        assert f"npm run {nome}" in TODA_DOC, f"script '{nome}' não documentado"


def test_links_internos_apontam_para_arquivos_existentes():
    for doc in ("CLAUDE.md", "README.md", "TESTING.md", "ANTIBAN.md"):
        texto = (RAIZ / doc).read_text(encoding="utf-8")
        for alvo in re.findall(r"\]\((?!https?:)([^)#]+)\)", texto):
            assert (RAIZ / alvo).exists(), f"{doc} aponta para '{alvo}' que não existe"
