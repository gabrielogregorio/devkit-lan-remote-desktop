"""Testes da persistencia de remapeamento de gamepad (server/gamepad.py)."""
import json

import pytest

from server import gamepad


def test_map_path_forcado(monkeypatch):
    monkeypatch.setenv("RL_GAMEPAD_MAP", "/tmp/x/gp.json")
    assert gamepad._map_path() == "/tmp/x/gp.json"


def test_map_path_xdg(monkeypatch):
    monkeypatch.delenv("RL_GAMEPAD_MAP", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/cfg")
    assert gamepad._map_path() == "/cfg/remote-link/gamepad-map.json"


def test_map_path_home(monkeypatch):
    monkeypatch.delenv("RL_GAMEPAD_MAP", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(gamepad.os.path, "expanduser", lambda p: "/home/u/.config")
    assert gamepad._map_path() == "/home/u/.config/remote-link/gamepad-map.json"


def test_load_maps_ausente(monkeypatch, tmp_path):
    monkeypatch.setenv("RL_GAMEPAD_MAP", str(tmp_path / "naoexiste.json"))
    assert gamepad.load_maps() == {}


def test_load_maps_corrompido(monkeypatch, tmp_path):
    p = tmp_path / "gp.json"
    p.write_text("{lixo nao json")
    monkeypatch.setenv("RL_GAMEPAD_MAP", str(p))
    assert gamepad.load_maps() == {}


def test_load_maps_nao_dict(monkeypatch, tmp_path):
    p = tmp_path / "gp.json"
    p.write_text("[1,2,3]")  # JSON valido mas nao dict
    monkeypatch.setenv("RL_GAMEPAD_MAP", str(p))
    assert gamepad.load_maps() == {}


def test_save_e_load_roundtrip(monkeypatch, tmp_path):
    p = tmp_path / "sub" / "gp.json"   # diretorio ainda nao existe -> makedirs
    monkeypatch.setenv("RL_GAMEPAD_MAP", str(p))
    gamepad.save_map("Pad XYZ", {"buttons": {"a": 1}})
    gamepad.save_map("Outro", {"axes": {"lx": 0}})
    maps = gamepad.load_maps()
    assert maps["Pad XYZ"] == {"buttons": {"a": 1}}
    assert maps["Outro"] == {"axes": {"lx": 0}}
    # persistiu no disco
    assert json.loads(p.read_text())["Pad XYZ"]["buttons"]["a"] == 1


def test_save_map_invalido(monkeypatch, tmp_path):
    monkeypatch.setenv("RL_GAMEPAD_MAP", str(tmp_path / "gp.json"))
    with pytest.raises(ValueError):
        gamepad.save_map("", {"a": 1})        # pad_id vazio
    with pytest.raises(ValueError):
        gamepad.save_map("ok", "naoeh dict")  # mapping nao-dict
