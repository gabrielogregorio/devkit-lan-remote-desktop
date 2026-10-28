"""
Persistence of the gamepad REMAPPING profile.

The client does the translation (it has the live Gamepad object) and already
sends the standard Xbox layout. Here we only store/serve the profile JSON, per
controller ID (gamepad.id), so it applies on any device that reaches this host -
useful for the knock-off PS2/USB controllers that report a non-standard mapping
with buttons/axes at arbitrary indices.

File format (LRD_GAMEPAD_MAP or $XDG_CONFIG_HOME/devkit-lan-remote-desktop/gamepad-map.json,
with ~/.config when XDG_CONFIG_HOME is unset):

    { "<gamepad.id>": { "buttons": {"a": 1, "b": 2, ...},
                        "axes": {"lx": 0, "ly": 1, ...},
                        "triggers": {"lt": {"src":"axis","i":4}, ...} } }

The meaning of each key is defined by the client (web/app.js). The server
treats the value as an opaque blob and only checks that it is a JSON dict.
"""
import json
import os


def _map_path() -> str:
    forced = os.environ.get("LRD_GAMEPAD_MAP")
    if forced:
        return forced
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "devkit-lan-remote-desktop", "gamepad-map.json")


def load_maps() -> dict:
    """Reads every saved profile. Never raises: missing/corrupted file -> {}."""
    try:
        with open(_map_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_map(pad_id: str, mapping: dict) -> None:
    """Saves/updates a controller profile (by gamepad.id) and persists it."""
    if not pad_id or not isinstance(mapping, dict):
        raise ValueError("pad_id and mapping (dict) are required")
    maps = load_maps()
    maps[str(pad_id)] = mapping
    _write(maps)


def delete_map(pad_id: str) -> bool:
    """Removes a controller profile. Returns True if it existed."""
    if not pad_id:
        raise ValueError("pad_id is required")
    maps = load_maps()
    if str(pad_id) not in maps:
        return False
    del maps[str(pad_id)]
    _write(maps)
    return True


def _write(maps: dict) -> None:
    path = _map_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(maps, f, ensure_ascii=False, indent=2)
