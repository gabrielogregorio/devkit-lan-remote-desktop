"""Tests for the gamepad remapping persistence (server/gamepad.py)."""
import json

import pytest

from server import gamepad


def test_map_path_forced(monkeypatch):
    monkeypatch.setenv("LRD_GAMEPAD_MAP", "/tmp/x/gp.json")
    assert gamepad._map_path() == "/tmp/x/gp.json"


def test_map_path_xdg(monkeypatch):
    monkeypatch.delenv("LRD_GAMEPAD_MAP", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/cfg")
    assert gamepad._map_path() == "/cfg/devkit-lan-remote-desktop/gamepad-map.json"


def test_map_path_home(monkeypatch):
    monkeypatch.delenv("LRD_GAMEPAD_MAP", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(gamepad.os.path, "expanduser", lambda p: "/home/u/.config")
    assert gamepad._map_path() == "/home/u/.config/devkit-lan-remote-desktop/gamepad-map.json"


def test_load_maps_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("LRD_GAMEPAD_MAP", str(tmp_path / "does-not-exist.json"))
    assert gamepad.load_maps() == {}


def test_load_maps_corrupted(monkeypatch, tmp_path):
    p = tmp_path / "gp.json"
    p.write_text("{garbage not json")
    monkeypatch.setenv("LRD_GAMEPAD_MAP", str(p))
    assert gamepad.load_maps() == {}


def test_load_maps_not_a_dict(monkeypatch, tmp_path):
    p = tmp_path / "gp.json"
    p.write_text("[1,2,3]")  # valid JSON but not a dict
    monkeypatch.setenv("LRD_GAMEPAD_MAP", str(p))
    assert gamepad.load_maps() == {}


def test_save_and_load_roundtrip(monkeypatch, tmp_path):
    p = tmp_path / "sub" / "gp.json"   # directory does not exist yet -> makedirs
    monkeypatch.setenv("LRD_GAMEPAD_MAP", str(p))
    gamepad.save_map("Pad XYZ", {"buttons": {"a": 1}})
    gamepad.save_map("Other", {"axes": {"lx": 0}})
    maps = gamepad.load_maps()
    assert maps["Pad XYZ"] == {"buttons": {"a": 1}}
    assert maps["Other"] == {"axes": {"lx": 0}}
    # persisted to disk
    assert json.loads(p.read_text())["Pad XYZ"]["buttons"]["a"] == 1


def test_save_map_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("LRD_GAMEPAD_MAP", str(tmp_path / "gp.json"))
    with pytest.raises(ValueError):
        gamepad.save_map("", {"a": 1})        # empty pad_id
    with pytest.raises(ValueError):
        gamepad.save_map("ok", "not a dict")  # non-dict mapping


def test_delete_map(monkeypatch, tmp_path):
    monkeypatch.setenv("LRD_GAMEPAD_MAP", str(tmp_path / "gp.json"))
    gamepad.save_map("Pad A", {"buttons": {"a": 1}})
    gamepad.save_map("Pad B", {"buttons": {"b": 2}})
    assert gamepad.delete_map("Pad A") is True   # it existed
    assert gamepad.delete_map("Pad A") is False  # already gone
    maps = gamepad.load_maps()
    assert "Pad A" not in maps and "Pad B" in maps


def test_delete_map_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("LRD_GAMEPAD_MAP", str(tmp_path / "gp.json"))
    with pytest.raises(ValueError):
        gamepad.delete_map("")
