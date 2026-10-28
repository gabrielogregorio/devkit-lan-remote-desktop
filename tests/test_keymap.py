from server.keymap import KEYMAP


def test_keymap_basics():
    for key in ("KeyA", "Digit0", "F1", "Space", "ArrowUp", "Numpad0"):
        assert key in KEYMAP, f"KEYMAP must have '{key}'"
        assert isinstance(KEYMAP[key], int), f"KEYMAP['{key}'] must be an int"
