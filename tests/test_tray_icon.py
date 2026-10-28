"""tray_icon.py without GTK: icons, port and process checks, texts, i18n catalog and --install."""
import os
import re
from pathlib import Path

import pytest

import tray_icon as tray

ROOT = Path(tray.__file__).resolve().parent
TR_CALL = re.compile(r'\btr\(\s*"((?:[^"\\]|\\.)*)"')
PLACEHOLDER = re.compile(r"\{(\w+)\}")


@pytest.fixture(autouse=True)
def english(monkeypatch):
    """The texts are asserted in English whatever the language of the machine running the tests."""
    monkeypatch.setattr(tray, "LANG", "en")


def test_icons_on_and_off(tmp_path):
    tray.write_icons(str(tmp_path))
    on = (tmp_path / f"{tray.APP['slug']}-on.svg").read_text()
    off = (tmp_path / f"{tray.APP['slug']}-off.svg").read_text()
    assert on.startswith("<svg") and tray.APP["color"] in on
    assert tray.APP["color"] not in off and "#9a9a9a" in off


def test_parse_ss_takes_every_pid():
    out = ('LISTEN 0 128 0.0.0.0:9 0.0.0.0:* users:(("python3",pid=4242,fd=5),("python3",pid=4243,fd=5))\n'
           "LISTEN 0 128 [::]:9 [::]:*\n")
    assert tray.parse_ss(out) == [4242, 4243]
    assert tray.parse_ss("") == []


def test_belongs_by_cwd_or_command_line(tmp_path):
    proc = tmp_path / "proc"
    app = tmp_path / "app"
    other = tmp_path / "other"
    app.mkdir()
    other.mkdir()
    for pid, cwd, cmd in ((1, app, b"python3\0x.py"), (2, other, b"bash\0" + str(app).encode() + b"/run.sh"),
                          (3, other, b"python3\0y.py")):
        (proc / str(pid)).mkdir(parents=True)
        os.symlink(cwd, proc / str(pid) / "cwd")
        (proc / str(pid) / "cmdline").write_bytes(cmd)
    assert tray.belongs(1, str(app), str(proc))
    assert tray.belongs(2, str(app), str(proc))
    assert not tray.belongs(3, str(app), str(proc))
    assert not tray.belongs(99, str(app), str(proc))  # gone


def test_status_texts():
    name, port = f"{tray.APP['name']} ({tray.APP['about']})", tray.APP["port"]
    assert tray.status_text(True, False) == f"{name}: on air (port {port})"
    assert tray.status_text(False, True) == f"{name}: starting…"
    assert tray.status_text(False, False) == f"{name}: off"


def test_failure_note_prefers_the_log(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("starting\n\nError: port already in use\n\n")
    assert tray.last_log_line(str(log)) == "Error: port already in use"
    assert tray.last_log_line(str(tmp_path / "missing.log")) == ""
    assert tray.failure_note(1, "boom") == "boom"
    assert tray.failure_note(2, "") == "exited with code 2; see the log"


def test_lan_url():
    if tray.APP["lan"]:
        assert tray.lan_url("192.168.0.9") == f"{tray.APP['lan']}://192.168.0.9:{tray.APP['port']}"
    assert tray.lan_ip().count(".") == 3


def test_every_string_has_a_pt_br_line():
    used = set(TR_CALL.findall((ROOT / "tray_icon.py").read_text(encoding="utf-8"))) | {tray.APP["about"]}
    assert used - set(tray.PT_BR) == set(), "add these to PT_BR in tray_icon.py"
    assert set(tray.PT_BR) - used == set(), "no longer used, remove from PT_BR in tray_icon.py"
    for en, pt in tray.PT_BR.items():
        assert set(PLACEHOLDER.findall(en)) == set(PLACEHOLDER.findall(pt)), en


def test_language():
    lang_env = tray.APP["lang_env"]
    assert tray.detect_language({}) == "en"
    assert tray.detect_language({"LANG": "pt_BR.UTF-8"}) == "pt-BR"
    assert tray.detect_language({"LANGUAGE": "pt_BR:en", "LANG": "en_US.UTF-8"}) == "pt-BR"
    assert tray.detect_language({lang_env: "en", "LANG": "pt_BR.UTF-8"}) == "en"
    assert tray.tr("On", lang="pt-BR") == "Ligado"
    assert tray.tr("{name}: off", lang="pt-BR", name="X") == "X: desligado"
    assert tray.tr(tray.APP["about"], lang="pt-BR") == tray.PT_BR[tray.APP["about"]]


@pytest.fixture
def home(tmp_path, monkeypatch):
    env = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data"),
           "XDG_CONFIG_HOME": str(tmp_path / "config")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(tray, "CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(tray, "ICONS", str(tmp_path / "cache" / "icons"))
    return env


def test_install_and_remove(home, capsys):
    apps, auto = tray.desktop_paths()
    assert tray.main(["--install"]) == 0
    for path in (apps, auto):
        text = Path(path).read_text()
        assert f"Exec=/usr/bin/python3 {ROOT}/tray_icon.py\n" in text
        assert f"Name={tray.APP['name']}\n" in text
    assert tray.main(["--remove"]) == 0
    assert not os.path.exists(apps) and not os.path.exists(auto)
    assert "removed" in capsys.readouterr().out


def test_one_icon_per_app(home):
    first = tray.single_instance()
    assert first is not None
    assert tray.single_instance() is None  # a 2nd run gives up
    first.close()
    again = tray.single_instance()
    assert again is not None
    again.close()


def test_second_run_exits_without_gtk(home, capsys):
    lock = tray.single_instance()
    assert tray.main([]) == 0
    assert "already on the bar" in capsys.readouterr().out
    lock.close()
