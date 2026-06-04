"""
Persistencia do perfil de REMAPEAMENTO de gamepad.

O cliente faz a traducao (ele tem o objeto Gamepad ao vivo) e envia ja no
formato padrao Xbox. Aqui so guardamos/servimos o JSON do perfil, por ID do
controle (gamepad.id), pra valer em qualquer device que acesse este host -
util pros controles "pirata" de PS2/USB que reportam mapping nao-standard com
botoes/eixos em indices arbitrarios.

Formato do arquivo (RL_GAMEPAD_MAP ou ~/.config/remote-link/gamepad-map.json):

    { "<gamepad.id>": { "buttons": {"a": 1, "b": 2, ...},
                        "axes": {"lx": 0, "ly": 1, ...},
                        "triggers": {"lt": {"src":"axis","i":4}, ...} } }

A semantica de cada chave e definida pelo cliente (web/app.js). O servidor
trata o valor como um blob opaco e so valida que e um dict JSON.
"""
import json
import os


def _map_path() -> str:
    forced = os.environ.get("RL_GAMEPAD_MAP")
    if forced:
        return forced
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "remote-link", "gamepad-map.json")


def load_maps() -> dict:
    """Le todos os perfis salvos. Nunca levanta: arquivo ausente/corrompido -> {}."""
    try:
        with open(_map_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_map(pad_id: str, mapping: dict) -> None:
    """Salva/atualiza o perfil de um controle (por gamepad.id) e persiste."""
    if not pad_id or not isinstance(mapping, dict):
        raise ValueError("pad_id e mapping (dict) obrigatorios")
    maps = load_maps()
    maps[str(pad_id)] = mapping
    _write(maps)


def delete_map(pad_id: str) -> bool:
    """Remove o perfil de um controle. Retorna True se existia."""
    if not pad_id:
        raise ValueError("pad_id obrigatorio")
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
