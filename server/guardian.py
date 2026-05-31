"""
guardian.py - escape hatch (kill switch) anti-banimento.

POR QUE ISTO EXISTE
-------------------
O remote-link injeta mouse, teclado e um **gamepad Xbox 360 virtual**
(045e:028e) via /dev/uinput e captura a tela. Para um anticheat, isso é
indistinguivel de cheat/automacao: um "jogador" sendo controlado de OUTRO PC,
com entrada sintetica e um controle que nao existe fisicamente. Rodar essa
ferramenta junto com um jogo online competitivo é caminho garantido para
BANIMENTO da conta (as vezes ate do hardware/HWID).

O QUE ESTA GUARDA FAZ
---------------------
  1. PRE-FLIGHT (na inicializacao): antes de carregar qualquer lib de controle
     (evdev/uinput) ou de tela (ffmpeg), varre os processos. Se achar QUALQUER
     anticheat ou jogo online da lista (ex.: Vanguard/vgc.exe ligado no boot do
     Windows), a ferramenta NEM SOBE.
  2. RUNTIME (enquanto roda): varre os processos a cada poucos segundos. Ao
     detectar qualquer item da lista, DERRUBA o servidor na hora - solta o
     uinput (some o gamepad virtual) e mata o ffmpeg (para a captura) antes de
     sair.
  3. AVISO NA TELA: nos dois casos mostra um aviso (dialogo grafico + banner no
     terminal) dizendo QUAL programa/jogo disparou e POR QUE a ferramenta nao
     pode rodar (controla mouse/tela/teclado/controle -> risco de ban).

É proposital ser conservador: NA DUVIDA, FECHA. Latencia de jogo casual nao vale
perder a conta.

Sem dependencias extras: Linux le /proc, Windows usa `tasklist`, outros usam
`ps`. Este modulo NAO importa evdev, aiohttp nem ffmpeg - pode ser chamado antes
de tudo.

Para desligar (sob sua conta e risco):  RL_GUARDIAN=off ./remote-link.sh
"""

import os
import subprocess
import sys

# ---------------------------------------------------------------------------
# LISTA NEGRA
#
# Cada entrada: "Nome amigavel": [basenames de processo, em minusculo].
# O match é por basename EXATO do executavel (case-insensitive), comparando
# contra: o nome do processo, o alvo do /proc/PID/exe e o basename de CADA
# token do cmdline (assim pegamos tambem jogos rodando via Proton/Wine, onde o
# .exe aparece nos argumentos).
# ---------------------------------------------------------------------------

# --- Anticheats (o sinal mais forte: se o driver/servico esta no ar, fecha) ---
ANTICHEATS = {
    "Riot Vanguard (Valorant/LoL)": [
        "vgc.exe", "vgtray.exe", "vgc", "vgk.sys", "vanguard.exe",
    ],
    "Easy Anti-Cheat (EAC/EOS)": [
        "easyanticheat.exe", "easyanticheat_eos.exe", "easyanticheat_x64.exe",
        "easyanticheat", "easyanticheat_x64", "eac_launcher.exe",
        "start_protected_game.exe",
    ],
    "BattlEye": [
        "beservice.exe", "beservice_x64.exe", "bedaisy.sys",
        "battleye.exe", "beclient.dll", "beservice_x64",
    ],
    "nProtect GameGuard": [
        "gameguard.des", "gamemon.des", "gamemon64.des", "npggnt.des",
        "gameguard.exe", "gg.exe", "x86_64.aes",
    ],
    "XignCode3 / Wellbia xigncode": [
        "xhunter1.sys", "xigncode", "xigncode3", "x64.xem", "x86.xem",
    ],
    "PunkBuster": ["pnkbstra.exe", "pnkbstrb.exe", "pnkbstra", "pnkbstrb"],
    "FACEIT Anti-Cheat": ["faceitclient.exe", "faceit.exe", "faceitservice.exe"],
    "ESEA Client": ["esea.exe", "eseaclient.exe"],
    "miHoYo/HoYoverse ACE / mhyprot": [
        "mhyprot2.sys", "mhyprot3.sys", "ace-base.sys", "acegame.sys",
        "anticheatexpert.exe", "sgaccore.dll",
    ],
    "Denuvo Anti-Cheat": ["denuvo.sys", "denuvocheat.sys"],
    "Treyarch/Ricochet (CoD)": ["ricochet.sys"],
    "Sabreshield / Sabre": ["sabresvc.sys"],
}

# --- Jogos online/multiplayer com ban por anticheat (principais do mundo) ---
# Inclui .exe (Windows/Proton) e o nome nativo Linux quando existe.
GAMES = {
    "Valorant": ["valorant.exe", "valorant-win64-shipping.exe"],
    "League of Legends": ["leagueclient.exe", "league of legends.exe", "leagueoflegends.exe"],
    "Teamfight Tactics": ["leagueclient.exe"],
    "Fortnite": ["fortniteclient-win64-shipping.exe", "fortnitelauncher.exe"],
    "Counter-Strike 2 / CS:GO": ["cs2.exe", "cs2", "csgo.exe", "csgo_linux64"],
    "PUBG: Battlegrounds": ["tslgame.exe"],
    "Apex Legends": ["r5apex.exe", "r5apex_dx12.exe"],
    "Call of Duty (MW/Warzone/BO)": [
        "cod.exe", "modernwarfare.exe", "blackopscoldwar.exe",
        "warzone.exe", "bootstrapper.exe", "cod22-cod.exe", "iw8sp.exe",
    ],
    "Rainbow Six Siege": ["rainbowsix.exe", "rainbowsix_dx11.exe", "rainbowsixgame.exe"],
    "Overwatch 2": ["overwatch.exe"],
    "Diablo IV": ["diabloiv.exe", "fenris.exe"],
    "Destiny 2": ["destiny2.exe"],
    "Rust": ["rustclient.exe", "rust.exe"],
    "DayZ": ["dayz_x64.exe", "dayz_be.exe"],
    "Escape from Tarkov": ["escapefromtarkov.exe", "escapefromtarkov_be.exe"],
    "GTA V Online / FiveM": ["gta5.exe", "gta5_enhanced.exe", "fivem.exe", "playgtav.exe", "ragemp_v.exe"],
    "Red Dead Online (RDR2)": ["rdr2.exe"],
    "Battlefield (2042/V/1/4)": ["bf2042.exe", "bfv.exe", "bf1.exe", "bf4.exe"],
    "Roblox": ["robloxplayerbeta.exe", "robloxplayerbeta"],
    "Genshin Impact": ["genshinimpact.exe", "yuanshen.exe"],
    "Honkai: Star Rail": ["starrail.exe"],
    "Zenless Zone Zero": ["zenlesszonezero.exe", "zenlesszonezero", "nap.exe"],
    "Wuthering Waves": ["wutheringwaves.exe", "client-win64-shipping.exe"],
    "Dead by Daylight": ["deadbydaylight-win64-shipping.exe"],
    "Halo Infinite / MCC": ["haloinfinite.exe", "mcc-win64-shipping.exe"],
    "Paladins": ["paladins.exe"],
    "Smite / Smite 2": ["smite.exe", "smite2.exe"],
    "War Thunder": ["aces.exe", "aces"],
    "World of Tanks": ["worldoftanks.exe", "wot.exe"],
    "World of Warships": ["worldofwarships.exe"],
    "Dota 2": ["dota2.exe", "dota2"],
    "Team Fortress 2": ["tf_win64.exe", "tf.exe", "tf_linux64"],
    "Lost Ark": ["lostark.exe"],
    "New World": ["newworld.exe"],
    "Black Desert Online": ["blackdesert64.exe", "blackdesert.exe"],
    "ARK: Survival (Ascended/Evolved)": ["shootergame.exe", "arkascended.exe"],
    "The Finals": ["discovery.exe"],
    "Marvel Rivals": ["marvelrivals.exe", "marvel-win64-shipping.exe"],
    "Hunt: Showdown": ["huntgame.exe"],
    "Sea of Thieves": ["sotgame.exe"],
    "For Honor": ["forhonor.exe"],
    "The Division 2": ["thedivision2.exe"],
    "Warframe": ["warframe.x64.exe", "warframe.exe"],
    "Fall Guys": ["fallguys_client.exe", "fallguys_client-win64-shipping.exe"],
    "Naraka: Bladepoint": ["narakabladepoint.exe", "na_login.exe"],
    "Delta Force": ["deltaforceclient-win64-shipping.exe", "deltaforce.exe"],
    "Splitgate 2": ["splitgate2.exe", "portal-win64-shipping.exe"],
    "Throne and Liberty": ["tl.exe", "throneandliberty.exe"],
    "Once Human": ["oncehuman.exe"],
    "Gray Zone Warfare": ["grayzonewarfare.exe", "gzw.exe"],
    "Hell Let Loose": ["hll-win64-shipping.exe"],
    "Star Citizen": ["starcitizen.exe", "rsi launcher.exe"],
    "EA SPORTS FC / FIFA": ["fc25.exe", "fc24.exe", "fifa23.exe", "fc.exe"],
    "Battlefield 6": ["bf6.exe"],
}


def _norm(name: str) -> str:
    return os.path.basename(name.strip().strip('"').strip("'")).lower()


# Mapa basename -> nome amigavel; e o tipo (anticheat/jogo).
_INDEX: dict[str, tuple[str, str]] = {}
for _disp, _names in ANTICHEATS.items():
    for _n in _names:
        _INDEX[_norm(_n)] = (_disp, "anticheat")
for _disp, _names in GAMES.items():
    for _n in _names:
        _INDEX.setdefault(_norm(_n), (_disp, "jogo"))


class Match:
    """Um processo da lista negra encontrado."""

    def __init__(self, display: str, kind: str, hit: str, pid: str = "?"):
        self.display = display   # nome amigavel ("Valorant")
        self.kind = kind         # "anticheat" | "jogo"
        self.hit = hit           # basename que casou ("valorant.exe")
        self.pid = pid

    def reason(self) -> str:
        if self.kind == "anticheat":
            return (f"Anticheat ativo: {self.display} (processo '{self.hit}', "
                    f"PID {self.pid}).")
        return (f"Jogo online detectado: {self.display} (processo '{self.hit}', "
                f"PID {self.pid}).")


# ----------------------------- varredura -----------------------------

def _candidates_linux():
    """Gera (pid, set_de_basenames) para cada processo via /proc."""
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return
    for pid in pids:
        names = set()
        base = f"/proc/{pid}"
        try:
            with open(f"{base}/comm", "rb") as f:
                names.add(f.read().decode("utf-8", "replace").strip().lower())
        except OSError:
            pass
        try:
            names.add(_norm(os.readlink(f"{base}/exe")))
        except OSError:
            pass
        try:
            with open(f"{base}/cmdline", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            for tok in raw.split("\x00"):
                if tok:
                    names.add(_norm(tok))
        except OSError:
            pass
        names.discard("")
        if names:
            yield pid, names


def _candidates_windows():
    """Gera (pid, set_de_basenames) via `tasklist`."""
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return
    import csv
    import io
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2:
            yield row[1].strip(), {_norm(row[0])}


def _candidates_generic():
    """Fallback (macOS/BSD): `ps`."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,comm="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, comm = line.partition(" ")
        yield pid.strip(), {_norm(comm)}


def _iter_processes():
    if sys.platform.startswith("linux"):
        yield from _candidates_linux()
    elif sys.platform.startswith("win"):
        yield from _candidates_windows()
    else:
        yield from _candidates_generic()


def scan() -> "Match | None":
    """Retorna o PRIMEIRO match da lista negra, ou None se a maquina esta limpa.
    Anticheats tem prioridade no relato (varredura unica, ordem por processo)."""
    best = None
    for pid, names in _iter_processes():
        for n in names:
            found = _INDEX.get(n)
            if found:
                disp, kind = found
                m = Match(disp, kind, n, pid)
                if kind == "anticheat":
                    return m  # sinal mais forte: reporta na hora
                if best is None:
                    best = m
    return best


# ----------------------------- aviso na tela -----------------------------

def _banner(m: Match, phase: str) -> str:
    head = ("INICIALIZACAO BLOQUEADA" if phase == "preflight"
            else "ENCERRAMENTO DE EMERGENCIA")
    return (
        "\n"
        "\033[1;41;97m  ╔══════════════════════════════════════════════════════════╗  \033[0m\n"
        f"\033[1;41;97m  ║  remote-link - {head:<41} ║  \033[0m\n"
        "\033[1;41;97m  ╚══════════════════════════════════════════════════════════╝  \033[0m\n"
        f"\033[1;31m  ✗ {m.reason()}\033[0m\n"
        "\033[1;33m  Esta ferramenta controla mouse, tela, teclado e gamepad (virtual).\n"
        "  Para um anticheat isso parece um CHEAT/automacao e pode BANIR sua conta.\n"
        "  Por seguranca ela NAO vai rodar enquanto isso estiver aberto.\033[0m\n"
        "\033[2m  (feche o jogo/anticheat e tente de novo - ou RL_GUARDIAN=off por sua conta e risco)\033[0m\n"
    )


def _gui_notice(m: Match, phase: str):
    """Tenta um dialogo grafico (best-effort, nunca derruba o processo)."""
    title = "remote-link bloqueado - risco de banimento"
    body = (
        f"{m.reason()}\n\n"
        "A ferramenta controla mouse, tela, teclado e gamepad virtual. "
        "Para um anticheat isso parece cheat/automacao e pode BANIR sua conta, "
        "por isso o remote-link "
        + ("nao foi iniciado." if phase == "preflight" else "foi encerrado.")
        + "\n\nFeche o jogo/anticheat e tente novamente."
    )
    try:
        if sys.platform.startswith("win"):
            import ctypes  # 0x10 = MB_ICONERROR | MB_SYSTEMMODAL(0x1000)
            ctypes.windll.user32.MessageBoxW(0, body, title, 0x1010)
            return
    except Exception:
        pass
    # Linux/macOS: tenta zenity -> kdialog -> notify-send -> xmessage
    env = dict(os.environ)
    env.setdefault("DISPLAY", os.environ.get("RL_DISPLAY", ":0"))
    for cmd in (
        ["zenity", "--error", "--width=460", "--title", title, "--text", body],
        ["kdialog", "--error", body, "--title", title],
        ["notify-send", "-u", "critical", title, body],
        ["xmessage", "-center", f"{title}\n\n{body}"],
    ):
        try:
            subprocess.Popen(cmd, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except (OSError, subprocess.SubprocessError):
            continue


def show_block_notice(m: Match, phase: str):
    """Aviso completo: banner no terminal + dialogo na tela."""
    sys.stderr.write(_banner(m, phase))
    sys.stderr.flush()
    _gui_notice(m, phase)


# ----------------------------- API publica -----------------------------

def disabled() -> bool:
    return os.environ.get("RL_GUARDIAN", "on").strip().lower() in (
        "off", "0", "false", "no", "nao", "disable", "disabled")


def enforce_preflight():
    """Chamado ANTES de carregar libs de controle. Se a maquina nao estiver
    limpa, mostra o aviso e ABORTA o processo (sys.exit(3))."""
    if disabled():
        sys.stderr.write(
            "\033[1;33m! guardian DESATIVADO (RL_GUARDIAN=off) - sem protecao "
            "anti-ban.\033[0m\n")
        return
    m = scan()
    if m:
        show_block_notice(m, "preflight")
        sys.exit(3)


async def monitor(on_trip, interval: float | None = None):
    """Loop assincrono. A cada `interval`s varre os processos; ao achar um item
    da lista, mostra o aviso e chama `on_trip(match)` (coro) para derrubar tudo.
    Intervalo padrao 2s; ajustavel via RL_GUARDIAN_INTERVAL (segundos)."""
    if disabled():
        return
    if interval is None:
        try:
            interval = max(0.5, float(os.environ.get("RL_GUARDIAN_INTERVAL", "2.0")))
        except ValueError:
            interval = 2.0
    import asyncio
    loop = asyncio.get_running_loop()
    while True:
        m = await loop.run_in_executor(None, scan)
        if m:
            show_block_notice(m, "runtime")
            await on_trip(m)
            return
        await asyncio.sleep(interval)


# ----------------------------- CLI -----------------------------

def _main(argv):
    if "--list" in argv:
        print("# Anticheats:")
        for d in ANTICHEATS:
            print(f"  - {d}")
        print("# Jogos:")
        for d in GAMES:
            print(f"  - {d}")
        return 0
    # default / --check : pre-flight para o remote-link.sh
    if disabled():
        sys.stderr.write("! guardian off (RL_GUARDIAN)\n")
        return 0
    m = scan()
    if m:
        show_block_notice(m, "preflight")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
