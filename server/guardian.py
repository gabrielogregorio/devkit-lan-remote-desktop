"""
guardian.py - anti-ban escape hatch (kill switch).

WHY THIS EXISTS
---------------
LAN Remote Desktop injects mouse, keyboard and a **virtual Xbox 360 gamepad**
(045e:028e) through /dev/uinput and captures the screen. To an anti-cheat this
is indistinguishable from a cheat/automation: a "player" being driven from
ANOTHER PC, with synthetic input and a controller that does not physically
exist. Running this tool next to a competitive online game is a sure way to get
the account BANNED (sometimes even the hardware/HWID).

WHAT THIS GUARD DOES
--------------------
  1. PRE-FLIGHT (at startup): before loading any input (evdev/uinput) or screen
     (ffmpeg) library, it scans the processes. If it finds ANY anti-cheat or
     online game from the list (e.g. Vanguard/vgc.exe running from Windows
     boot), the tool DOES NOT EVEN START.
  2. RUNTIME (while running): scans the processes every few seconds. When it
     detects any listed item, it TAKES THE SERVER DOWN right away - releases
     uinput (the virtual gamepad disappears) and kills ffmpeg (stops the
     capture) before exiting.
  3. ON-SCREEN NOTICE: in both cases it shows a notice (graphical dialog +
     terminal banner) saying WHICH program/game triggered it and WHY the tool
     cannot run (it controls mouse/screen/keyboard/gamepad -> ban risk).

Being conservative is on purpose: WHEN IN DOUBT, SHUT DOWN. Casual game latency
is not worth losing the account.

No extra dependencies: Linux reads /proc, Windows uses `tasklist`, others use
`ps`. This module does NOT import evdev, aiohttp or ffmpeg - it can be called
before everything else.

To turn it off (at your own risk):  LRD_GUARDIAN=off ./run.sh
"""

import os
import subprocess
import sys

from .i18n import tr

# ---------------------------------------------------------------------------
# BLOCKLIST
#
# Each entry: "Friendly name": [process basenames, lowercase].
# The match is by the EXACT basename of the executable (case-insensitive),
# compared against: the process name, the /proc/PID/exe target and the basename
# of EACH cmdline token (so games running through Proton/Wine, where the .exe
# shows up in the arguments, are caught too).
# ---------------------------------------------------------------------------

# --- Anti-cheats (the strongest signal: if the driver/service is up, shut down) ---
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

# --- Online/multiplayer games with anti-cheat bans (the world's main ones) ---
# Includes the .exe (Windows/Proton) and the native Linux name when there is one.
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


# Map basename -> friendly name and kind (anticheat/game).
_INDEX: dict[str, tuple[str, str]] = {}
for _disp, _names in ANTICHEATS.items():
    for _n in _names:
        _INDEX[_norm(_n)] = (_disp, "anticheat")
for _disp, _names in GAMES.items():
    for _n in _names:
        _INDEX.setdefault(_norm(_n), (_disp, "game"))


class Match:
    """A blocklisted process that was found."""

    def __init__(self, display: str, kind: str, hit: str, pid: str = "?"):
        self.display = display   # friendly name ("Valorant")
        self.kind = kind         # "anticheat" | "game"
        self.hit = hit           # basename that matched ("valorant.exe")
        self.pid = pid

    def reason(self) -> str:
        if self.kind == "anticheat":
            return tr("Anti-cheat running: {name} (process '{process}', PID {pid}).",
                      name=self.display, process=self.hit, pid=self.pid)
        return tr("Online game detected: {name} (process '{process}', PID {pid}).",
                  name=self.display, process=self.hit, pid=self.pid)


# ----------------------------- scanning -----------------------------

def _candidates_linux():
    """Yields (pid, set_of_basenames) for each process through /proc."""
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
    """Yields (pid, set_of_basenames) through `tasklist`."""
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
    """Returns the FIRST blocklist match, or None when the machine is clean.
    Anti-cheats take priority in the report (single pass, ordered by process)."""
    best = None
    for pid, names in _iter_processes():
        for n in names:
            found = _INDEX.get(n)
            if found:
                disp, kind = found
                m = Match(disp, kind, n, pid)
                if kind == "anticheat":
                    return m  # strongest signal: report right away
                if best is None:
                    best = m
    return best


# ----------------------------- on-screen notice -----------------------------

def _risk_lines() -> tuple[str, str]:
    return (tr("This tool controls the mouse, screen, keyboard and (virtual) gamepad."),
            tr("To an anti-cheat this looks like a CHEAT/automation and can BAN your account."))


def _banner(m: Match, phase: str) -> str:
    head = tr("STARTUP BLOCKED") if phase == "preflight" else tr("EMERGENCY SHUTDOWN")
    controls, risk = _risk_lines()
    safety = tr("For safety it will NOT run while that is open.")
    hint = tr("(close the game/anti-cheat and try again - or LRD_GUARDIAN=off at your own risk)")
    return (
        "\n"
        "\033[1;41;97m  ╔══════════════════════════════════════════════════════════╗  \033[0m\n"
        f"\033[1;41;97m  ║  LAN Remote Desktop - {head:<34} ║  \033[0m\n"
        "\033[1;41;97m  ╚══════════════════════════════════════════════════════════╝  \033[0m\n"
        f"\033[1;31m  ✗ {m.reason()}\033[0m\n"
        f"\033[1;33m  {controls}\n"
        f"  {risk}\n"
        f"  {safety}\033[0m\n"
        f"\033[2m  {hint}\033[0m\n"
    )


def _gui_notice(m: Match, phase: str):
    """Tries a graphical dialog (best-effort, never takes the process down)."""
    title = tr("LAN Remote Desktop blocked - ban risk")
    outcome = (tr("That is why LAN Remote Desktop was not started.") if phase == "preflight"
               else tr("That is why LAN Remote Desktop was shut down."))
    controls, risk = _risk_lines()
    retry = tr("Close the game/anti-cheat and try again.")
    body = f"{m.reason()}\n\n{controls} {risk} {outcome}\n\n{retry}"
    try:
        if sys.platform.startswith("win"):
            import ctypes  # 0x10 = MB_ICONERROR | MB_SYSTEMMODAL(0x1000)
            ctypes.windll.user32.MessageBoxW(0, body, title, 0x1010)
            return
    except Exception:
        pass
    # Linux/macOS: tries zenity -> kdialog -> notify-send -> xmessage
    env = dict(os.environ)
    env.setdefault("DISPLAY", os.environ.get("LRD_DISPLAY", ":0"))
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
    """Full notice: terminal banner + on-screen dialog."""
    sys.stderr.write(_banner(m, phase))
    sys.stderr.flush()
    _gui_notice(m, phase)


# ----------------------------- public API -----------------------------

def disabled() -> bool:
    return os.environ.get("LRD_GUARDIAN", "on").strip().lower() in (
        "off", "0", "false", "no", "disable", "disabled")


def enforce_preflight():
    """Called BEFORE loading the input libraries. If the machine is not clean,
    shows the notice and ABORTS the process (sys.exit(3))."""
    if disabled():
        sys.stderr.write(
            "\033[1;33m! guardian DISABLED (LRD_GUARDIAN=off) - no anti-ban "
            "protection.\033[0m\n")
        return
    m = scan()
    if m:
        show_block_notice(m, "preflight")
        sys.exit(3)


async def monitor(on_trip, interval: float | None = None):
    """Async loop. Every `interval` seconds it scans the processes; when it finds
    a listed item it shows the notice and calls `on_trip(match)` (a coroutine)
    to take everything down. Default interval 2s; tunable with
    LRD_GUARDIAN_INTERVAL (seconds)."""
    if disabled():
        return
    if interval is None:
        try:
            interval = max(0.5, float(os.environ.get("LRD_GUARDIAN_INTERVAL", "2.0")))
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
        print("# Anti-cheats:")
        for d in ANTICHEATS:
            print(f"  - {d}")
        print("# Games:")
        for d in GAMES:
            print(f"  - {d}")
        return 0
    # default / --check: pre-flight for run.sh
    if disabled():
        sys.stderr.write("! guardian off (LRD_GUARDIAN)\n")
        return 0
    m = scan()
    if m:
        show_block_notice(m, "preflight")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
