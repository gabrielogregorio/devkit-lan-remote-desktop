"""Desktop strings (the guardian terminal banner and its block dialog). English is the source
text and the key; PT_BR holds the Brazilian Portuguese line. The language comes from LRD_LANG,
then the usual LANGUAGE / LC_ALL / LC_MESSAGES / LANG: pt* shows pt-BR, anything else English.
tests/test_i18n.py fails on a missing line or on different {placeholders}. Logs stay English."""

import os

PT_BR = {
    "Anti-cheat running: {name} (process '{process}', PID {pid}).":
        "Anticheat ativo: {name} (processo '{process}', PID {pid}).",
    "Online game detected: {name} (process '{process}', PID {pid}).":
        "Jogo online detectado: {name} (processo '{process}', PID {pid}).",
    "STARTUP BLOCKED": "INICIALIZAÇÃO BLOQUEADA",
    "EMERGENCY SHUTDOWN": "ENCERRAMENTO DE EMERGÊNCIA",
    "This tool controls the mouse, screen, keyboard and (virtual) gamepad.":
        "Esta ferramenta controla mouse, tela, teclado e gamepad (virtual).",
    "To an anti-cheat this looks like a CHEAT/automation and can BAN your account.":
        "Para um anticheat isso parece CHEAT/automação e pode BANIR sua conta.",
    "For safety it will NOT run while that is open.":
        "Por segurança ela NÃO vai rodar enquanto isso estiver aberto.",
    "(close the game/anti-cheat and try again - or LRD_GUARDIAN=off at your own risk)":
        "(feche o jogo/anticheat e tente de novo - ou LRD_GUARDIAN=off por sua conta e risco)",
    "LAN Remote Desktop blocked - ban risk": "LAN Remote Desktop bloqueado - risco de banimento",
    "That is why LAN Remote Desktop was not started.": "Por isso o LAN Remote Desktop não foi iniciado.",
    "That is why LAN Remote Desktop was shut down.": "Por isso o LAN Remote Desktop foi encerrado.",
    "Close the game/anti-cheat and try again.": "Feche o jogo/anticheat e tente novamente.",
}


def detect_language(env=None) -> str:
    env = os.environ if env is None else env
    for name in ("LRD_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = env.get(name, "")
        if value:
            first = value.split(":")[0]
            return "pt-BR" if first.lower().startswith("pt") else "en"
    return "en"


LANG = detect_language()


def tr(text: str, lang: str | None = None, **values) -> str:
    lang = LANG if lang is None else lang
    line = PT_BR.get(text, text) if lang == "pt-BR" else text
    return line.format(**values) if values else line
