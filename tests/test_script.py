"""
Tests for the `run.sh` entrypoint - the only path the user actually runs and
that, until now, no test touched.

How: the script runs FOR REAL, but inside a sandbox. A copy of it goes to a
temporary directory (it does `cd "$(dirname "$BASH_SOURCE")"`, so certs/venv
land in tmp) and PATH is swapped for a folder of stand-ins for `ffmpeg`,
`openssl`, `python3`, `sudo`, etc. Each stand-in logs the call - so we can
assert what the script tried to do without touching the system, without real
sudo and without starting any server.
"""
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "run.sh"

# real tools the script uses that make no sense to stub
COREUTILS = ["bash", "sh", "sed", "tr", "head", "grep", "awk", "mkdir", "id",
             "cat", "rm", "ls", "dirname", "basename", "env", "sleep", "touch",
             "cut", "sort", "chmod", "uname"]


def _stub(path: Path, body: str):
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path):
    """Copy of the script + a PATH with stand-ins only. Returns a runner."""
    home = tmp_path / "home"
    home.mkdir()
    binn = tmp_path / "bin"
    binn.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("")

    for tool in COREUTILS:
        real = shutil.which(tool)
        if real:
            os.symlink(real, binn / tool)

    record = f'echo "$(basename "$0") $*" >> "{log}"\n'

    _stub(binn / "ffmpeg", record + "exit 0\n")
    _stub(binn / "xdpyinfo", record + "exit 0\n")
    _stub(binn / "pgrep", record + "exit 1\n")          # no gnome-shell
    _stub(binn / "hostname", record + """
        if [[ "${1:-}" == "-I" ]]; then echo "192.168.0.77 172.17.0.1 10.1.2.3"
        else echo testhost; fi
    """)
    # openssl stand-in: only creates the files the script expects (fast and hermetic)
    _stub(binn / "openssl", record + """
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
        _stub(binn / t, record + 'exec "$@" 2>/dev/null || exit 0\n'
              if t == "sudo" else record + "cat >/dev/null 2>&1; exit 0\n")
    # python3 stand-in: covers the 4 uses the script makes
    _stub(binn / "python3", record + """
        args="$*"
        case "$args" in
          *"import aiohttp, evdev"*)  exit "${FAKE_DEPS_RC:-0}" ;;
          *"server.guardian --check"*) exit "${FAKE_GUARDIAN_RC:-0}" ;;
          *"from evdev import UInput"*) exit "${FAKE_UINPUT_RC:-0}" ;;
          *"server.main"*)
            echo "SERVER_STARTED"
            echo "LRD_PORT=$LRD_PORT LRD_HOST=$LRD_HOST LRD_CERT=$LRD_CERT LRD_KEY=$LRD_KEY"
            echo "LRD_DISPLAY=$LRD_DISPLAY"
            exit 0 ;;
        esac
        exit 0
    """)

    script = tmp_path / "run.sh"
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
        def calls(self) -> list[str]:
            return log.read_text().splitlines()

        def called(self, prog: str) -> bool:
            return any(c.split()[0] == prog for c in self.calls if c.strip())

    return Runner()


# ───────────────────────── syntax / lint ─────────────────────────

def test_bash_syntax():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_shellcheck_has_no_errors():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck not installed")
    r = subprocess.run(["shellcheck", "-S", "error", str(SCRIPT)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK), "the README says to run ./run.sh"


# ───────────────────────── normal startup ─────────────────────────

def test_starts_the_server_on_the_default_port(sandbox):
    r = sandbox.run()
    assert r.returncode == 0, r.stderr
    assert "SERVER_STARTED" in r.stdout
    assert "LRD_PORT=8443" in r.stdout
    assert "LRD_HOST=0.0.0.0" in r.stdout
    assert "command not found" not in r.stderr, "the script called something unexpected"


def test_shows_the_lan_url_ignoring_the_docker_ip(sandbox):
    r = sandbox.run()
    assert "https://192.168.0.77:8443" in r.stdout
    # 172.17.x is the Docker bridge: it must not become the main address
    assert "https://172.17.0.1:8443" not in r.stdout.split("other IPs")[0]


@pytest.mark.parametrize("args,expected", [
    ((), "8443"),
    (("--port", "9000"), "9000"),
    (("--port=9001",), "9001"),
])
def test_port_from_the_command_line(sandbox, args, expected):
    r = sandbox.run(*args)
    assert f"LRD_PORT={expected}" in r.stdout


def test_port_from_the_environment(sandbox):
    r = sandbox.run(env={"LRD_PORT": "9100"})
    assert "LRD_PORT=9100" in r.stdout


def test_unknown_argument_does_not_break(sandbox):
    r = sandbox.run("--whatever")
    assert r.returncode == 0
    assert "LRD_PORT=8443" in r.stdout


def test_passes_the_detected_display_to_the_server(sandbox):
    r = sandbox.run(env={"DISPLAY": ":7"})
    assert "LRD_DISPLAY=:7" in r.stdout


# ───────────────────────── TLS certificate ─────────────────────────

def test_creates_the_certificate_the_first_time(sandbox):
    r = sandbox.run()
    cert = sandbox.dir / "certs" / "cert.pem"
    key = sandbox.dir / "certs" / "key.pem"
    assert cert.exists() and key.exists()
    assert f"LRD_CERT={cert}" in r.stdout and f"LRD_KEY={key}" in r.stdout
    # SAN with the LAN IPs, otherwise the browser rejects the certificate
    call = next(c for c in sandbox.calls if c.startswith("openssl"))
    assert "subjectAltName=" in call
    assert "IP:192.168.0.77" in call and "DNS:localhost" in call
    assert "/CN=lan-remote-desktop" in call


def test_reuses_an_existing_certificate(sandbox):
    sandbox.run()
    mark = (sandbox.dir / "certs" / "cert.pem").read_text()
    sandbox.log_path.write_text("")
    r = sandbox.run()
    assert not sandbox.called("openssl"), "must not regenerate the cert on every boot"
    assert (sandbox.dir / "certs" / "cert.pem").read_text() == mark
    assert "SERVER_STARTED" in r.stdout


# ───────────────────────── requirements and guardian ─────────────────────────

def test_without_ffmpeg_fails_with_a_useful_message(sandbox):
    (sandbox.dir / "bin" / "ffmpeg").unlink()
    r = sandbox.run()
    assert r.returncode != 0
    assert "ffmpeg" in r.stderr and "SERVER_STARTED" not in r.stdout


def test_without_openssl_fails(sandbox):
    (sandbox.dir / "bin" / "openssl").unlink()
    r = sandbox.run()
    assert r.returncode != 0
    assert "openssl" in r.stderr


def test_guardian_blocks_before_starting_anything(sandbox):
    """Anti-ban escape hatch: guardian != 0 => the server does not even start."""
    r = sandbox.run(env={"FAKE_GUARDIAN_RC": "1"})
    assert r.returncode != 0
    assert "guardian" in r.stderr.lower()
    assert "SERVER_STARTED" not in r.stdout
    # and it never got to create the certificate / touch uinput
    assert not (sandbox.dir / "certs" / "cert.pem").exists()


def test_without_uinput_permission_warns_but_goes_on(sandbox):
    r = sandbox.run(env={"FAKE_UINPUT_RC": "1"})
    assert r.returncode == 0
    assert "uinput" in r.stdout
    assert "setup" in r.stdout, "it has to say HOW to fix it"
    assert "SERVER_STARTED" in r.stdout


def test_creates_the_venv_when_dependencies_are_missing(sandbox):
    """Without aiohttp/evdev on the system, the script builds the venv and uses its python."""
    r = sandbox.run(env={"FAKE_DEPS_RC": "1"})
    assert "venv" in r.stdout
    assert any("-m venv" in c for c in sandbox.calls)


# ───────────────────────── setup mode ─────────────────────────

def test_setup_configures_uinput_and_exits_without_starting_the_server(sandbox):
    r = sandbox.run("setup")
    assert r.returncode == 0
    assert "SERVER_STARTED" not in r.stdout
    joined = "\n".join(sandbox.calls)
    assert "modprobe uinput" in joined
    assert "udevadm" in joined
    assert "usermod" in joined or "setfacl" in joined
    assert "/etc/udev/rules.d/99-lan-remote-desktop-uinput.rules" in joined


def test_setup_with_port_does_not_mix_up_the_arguments(sandbox):
    """`setup --port 9000`: setup runs and exits; the port does not become '--port'."""
    r = sandbox.run("setup", "--port", "9000")
    assert r.returncode == 0
    assert "SERVER_STARTED" not in r.stdout
