from server.keymap import KEYMAP


def test_keymap_basico():
    for key in ("KeyA", "Digit0", "F1", "Space", "ArrowUp", "Numpad0"):
        assert key in KEYMAP, f"KEYMAP deve ter '{key}'"
        assert isinstance(KEYMAP[key], int), f"KEYMAP['{key}'] deve ser int"
