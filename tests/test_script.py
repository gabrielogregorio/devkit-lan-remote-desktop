"""
Testes do entrypoint `remote-link.sh` - o único caminho que o usuário realmente
executa e que, até aqui, nenhum teste tocava.

Como: o script roda DE VERDADE, mas dentro de um sandbox. Uma cópia dele vai
pra um diretório temporário (ele faz `cd "$(dirname "$BASH_SOURCE")"`, então
certs/venv caem no tmp) e o PATH é trocado por uma pasta com dublês de
`ffmpeg`, `openssl`, `python3`, `sudo`, etc. Cada dublê registra a chamada num
log - dá pra afirmar o que o script tentou fazer sem tocar no sistema, sem
sudo real e sem subir servidor nenhum.
"""
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "remote-link.sh"

# ferramentas reais que o script usa e que não faz sentido dublar
COREUTILS = ["bash", "sh", "sed", "tr", "head", "grep", "awk", "mkdir", "id",
             "cat", "rm", "ls", "dirname", "basename", "env", "sleep", "touch",
             "cut", "sort", "chmod", "uname"]


def _stub(path: Path, corpo: str):
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(corpo))
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path):
    """Cópia do script + PATH só com dublês. Devolve um runner."""
    home = tmp_path / "home"
    home.mkdir()
    binn = tmp_path / "bin"
    binn.mkdir()
    log = tmp_path / "chamadas.log"
    log.write_text("")

    for tool in COREUTILS:
        real = shutil.which(tool)
        if real:
            os.symlink(real, binn / tool)

    registra = f'echo "$(basename "$0") $*" >> "{log}"\n'

    _stub(binn / "ffmpeg", registra + "exit 0\n")
    _stub(binn / "xdpyinfo", registra + "exit 0\n")
    _stub(binn / "pgrep", registra + "exit 1\n")          # nenhum gnome-shell
    _stub(binn / "hostname", registra + """
        if [[ "${1:-}" == "-I" ]]; then echo "192.168.0.77 172.17.0.1 10.1.2.3"
        else echo testhost; fi
    """)
    # openssl dublê: só cria os arquivos que o script espera (rápido e hermético)
    _stub(binn / "openssl", registra + """
        key=""; crt=""
        while [[ $# -gt 0 ]]; do
          case "$1" in -keyout) key="$2"; shift 2;; -out) crt="$2"; shift 2;;
                       *) shift;; esac
        done
        [[ -n "$key" ]] && echo "FAKE KEY" > "$key"
        [[ -n "$crt" ]] && echo "FAKE CERT" > "$crt"
        exit 0
    """)
    for t in ("sudo", "tee", "udevadm", "usermod", "setfacl", "modprobe"):
        _stub(binn / t, registra + 'exec "$@" 2>/dev/null || exit 0\n'
              if t == "sudo" else registra + "cat >/dev/null 2>&1; exit 0\n")
    # python3 dublê: cobre os 4 usos que o script faz
    _stub(binn / "python3", registra + """
        args="$*"
        case "$args" in
          *"import aiohttp, evdev"*)  exit "${FAKE_DEPS_RC:-0}" ;;
          *"server.guardian --check"*) exit "${FAKE_GUARDIAN_RC:-0}" ;;
          *"from evdev import UInput"*) exit "${FAKE_UINPUT_RC:-0}" ;;
          *"server.main"*)
            echo "SERVIDOR_INICIADO"
            echo "RL_PORT=$RL_PORT RL_HOST=$RL_HOST RL_CERT=$RL_CERT RL_KEY=$RL_KEY"
            echo "RL_DISPLAY=$RL_DISPLAY"
            exit 0 ;;
        esac
        exit 0
    """)

    script = tmp_path / "remote-link.sh"
    shutil.copy(SCRIPT, script)
    script.chmod(0o755)

    class Runner:
        dir = tmp_path
        log_path = log

        def run(self, *args, env=None, timeout=60):
            base = {
                "PATH": str(binn),
                "HOME": str(home),
                "USER": "tester",
                "DISPLAY": ":1",
                "TERM": "dumb",
            }
            base.update(env or {})
            return subprocess.run([str(script), *args], capture_output=True,
                                  text=True, env=base, timeout=timeout)

        @property
        def chamadas(self) -> list[str]:
            return log.read_text().splitlines()

        def chamou(self, prog: str) -> bool:
            return any(c.split()[0] == prog for c in self.chamadas if c.strip())

    return Runner()


# ───────────────────────── sintaxe / lint ─────────────────────────

def test_sintaxe_do_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_shellcheck_sem_erros():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck não instalado")
    r = subprocess.run(["shellcheck", "-S", "error", str(SCRIPT)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_script_e_executavel():
    assert os.access(SCRIPT, os.X_OK), "o README manda rodar ./remote-link.sh"


# ───────────────────────── inicialização normal ─────────────────────────

def test_inicia_o_servidor_na_porta_padrao(sandbox):
    r = sandbox.run()
    assert r.returncode == 0, r.stderr
    assert "SERVIDOR_INICIADO" in r.stdout
    assert "RL_PORT=8443" in r.stdout
    assert "RL_HOST=0.0.0.0" in r.stdout
    assert "command not found" not in r.stderr, "o script chamou algo inesperado"


def test_mostra_a_url_da_lan_ignorando_ip_de_docker(sandbox):
    r = sandbox.run()
    assert "https://192.168.0.77:8443" in r.stdout
    # 172.17.x é bridge do Docker: não pode virar o endereço principal
    assert "https://172.17.0.1:8443" not in r.stdout.split("outros IPs")[0]


@pytest.mark.parametrize("args,esperado", [
    ((), "8443"),
    (("--port", "9000"), "9000"),
    (("--port=9001",), "9001"),
])
def test_porta_pela_linha_de_comando(sandbox, args, esperado):
    r = sandbox.run(*args)
    assert f"RL_PORT={esperado}" in r.stdout


def test_porta_pelo_ambiente(sandbox):
    r = sandbox.run(env={"RL_PORT": "9100"})
    assert "RL_PORT=9100" in r.stdout


def test_argumento_desconhecido_nao_quebra(sandbox):
    r = sandbox.run("--sei-la")
    assert r.returncode == 0
    assert "RL_PORT=8443" in r.stdout


def test_passa_o_display_detectado_pro_servidor(sandbox):
    r = sandbox.run(env={"DISPLAY": ":7"})
    assert "RL_DISPLAY=:7" in r.stdout


# ───────────────────────── certificado TLS ─────────────────────────

def test_gera_certificado_na_primeira_vez(sandbox):
    r = sandbox.run()
    cert = sandbox.dir / "certs" / "cert.pem"
    key = sandbox.dir / "certs" / "key.pem"
    assert cert.exists() and key.exists()
    assert f"RL_CERT={cert}" in r.stdout and f"RL_KEY={key}" in r.stdout
    # SAN com os IPs da LAN, senão o navegador recusa o certificado
    chamada = next(c for c in sandbox.chamadas if c.startswith("openssl"))
    assert "subjectAltName=" in chamada
    assert "IP:192.168.0.77" in chamada and "DNS:localhost" in chamada


def test_reaproveita_certificado_existente(sandbox):
    sandbox.run()
    marca = (sandbox.dir / "certs" / "cert.pem").read_text()
    sandbox.log_path.write_text("")
    r = sandbox.run()
    assert not sandbox.chamou("openssl"), "não pode regerar cert a cada boot"
    assert (sandbox.dir / "certs" / "cert.pem").read_text() == marca
    assert "SERVIDOR_INICIADO" in r.stdout


# ───────────────────────── pré-requisitos e guardian ─────────────────────────

def test_sem_ffmpeg_falha_com_mensagem_util(sandbox):
    (sandbox.dir / "bin" / "ffmpeg").unlink()
    r = sandbox.run()
    assert r.returncode != 0
    assert "ffmpeg" in r.stderr and "SERVIDOR_INICIADO" not in r.stdout


def test_sem_openssl_falha(sandbox):
    (sandbox.dir / "bin" / "openssl").unlink()
    r = sandbox.run()
    assert r.returncode != 0
    assert "openssl" in r.stderr


def test_guardian_bloqueia_antes_de_subir_qualquer_coisa(sandbox):
    """Escape hatch anti-ban: guardian != 0 => o servidor NEM inicia."""
    r = sandbox.run(env={"FAKE_GUARDIAN_RC": "1"})
    assert r.returncode != 0
    assert "guardian" in r.stderr.lower()
    assert "SERVIDOR_INICIADO" not in r.stdout
    # e não chegou a gerar certificado / tocar em uinput
    assert not (sandbox.dir / "certs" / "cert.pem").exists()


def test_sem_permissao_de_uinput_avisa_mas_continua(sandbox):
    r = sandbox.run(env={"FAKE_UINPUT_RC": "1"})
    assert r.returncode == 0
    assert "uinput" in r.stdout
    assert "setup" in r.stdout, "tem que dizer COMO resolver"
    assert "SERVIDOR_INICIADO" in r.stdout


def test_cria_venv_quando_faltam_dependencias(sandbox):
    """Sem aiohttp/evdev no sistema, o script monta o venv e usa o python dele."""
    r = sandbox.run(env={"FAKE_DEPS_RC": "1"})
    assert "venv" in r.stdout
    assert any("-m venv" in c for c in sandbox.chamadas)


# ───────────────────────── modo setup ─────────────────────────

def test_setup_configura_uinput_e_sai_sem_subir_servidor(sandbox):
    r = sandbox.run("setup")
    assert r.returncode == 0
    assert "SERVIDOR_INICIADO" not in r.stdout
    joined = "\n".join(sandbox.chamadas)
    assert "modprobe uinput" in joined
    assert "udevadm" in joined
    assert "usermod" in joined or "setfacl" in joined


def test_setup_com_porta_nao_confunde_os_argumentos(sandbox):
    """`setup --port 9000`: o setup roda e sai; a porta não vira '--port'."""
    r = sandbox.run("setup", "--port", "9000")
    assert r.returncode == 0
    assert "SERVIDOR_INICIADO" not in r.stdout
