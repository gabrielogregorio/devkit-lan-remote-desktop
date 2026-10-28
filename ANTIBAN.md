# Anti-ban escape hatch (guardian)

`server/guardian.py` is a **safety lock** that stops LAN Remote Desktop from running together with
online/competitive games protected by an anti-cheat.

## Why it exists

LAN Remote Desktop injects **mouse, keyboard and a _virtual_ Xbox 360 gamepad** (`045e:028e`)
through `/dev/uinput`, and it captures the screen. To an anti-cheat (Vanguard, EAC, BattlEye,
GameGuard, etc.) that scene, a "player" driven from **another device** with synthetic input and a
controller that does not physically exist, cannot be told apart from a **cheat or a bot**. The
likely result is an **account ban** (and, with Vanguard or Tarkov, a hardware/HWID ban).

The tool is made for **casual LAN use and remote work on your own network**, not for playing
competitive games from another device. The guardian makes sure nobody does that by mistake and
loses the account.

## What it does (3 layers)

1. **Shell pre-flight:** `run.sh` runs `python -m server.guardian --check` **before** touching
   uinput, TLS or ffmpeg. If the machine is not clean, the server **is never started**.
2. **Python pre-flight:** `server/main.py` calls `guardian.enforce_preflight()` **first**, _before_
   importing `aiohttp`, `video`, `audio` and `inject`. On a blocked PC **the control libraries
   (evdev/uinput) and the screen ones (ffmpeg) are never even loaded**. This also covers Windows,
   where the `.sh` does not run.
3. **Runtime monitor:** while the server is up it scans the processes every **~2 s**
   (`LRD_GUARDIAN_INTERVAL`). If an anti-cheat or a listed game **opens**, it brings everything down
   at once **without asking**: it releases `uinput` (the virtual gamepad **disappears**), kills
   `ffmpeg` (the capture **stops**) and ends the process (`os._exit(99)`). Reaction window: 0-2 s,
   and in practice the anti-cheat/loader starts well before the game is playable.

In every case it shows a **notice on screen**: a graphical dialog (`zenity`/`kdialog`/`notify-send`/
`xmessage` on Linux, `MessageBox` on Windows) **and** a red banner in the terminal, saying **which
program or game** triggered it and **why** (the tool controls mouse, screen, keyboard and gamepad,
so there is a ban risk). The notice follows the system language (English or Brazilian Portuguese;
`LRD_LANG` forces one).

Principle: **when in doubt, shut down.** Casual game latency is not worth an account.

## How it detects (Windows + Linux, no extra dependency)

- **Linux:** reads `/proc/<pid>/{comm,exe,cmdline}`. It takes the basename of the executable and of
  **every cmdline token**, so it also catches games running through **Proton/Wine** (the `.exe`
  shows up in the arguments) and native names (`cs2`, `dota2`, `tf_linux64`).
- **Windows:** `tasklist /fo csv` (catches `vgc.exe`, `BEService.exe`, `EasyAntiCheat.exe`, the
  games' `.exe`, etc.).
- **Others (macOS/BSD):** falls back to `ps`.

The match is by **exact basename** of the executable (case-insensitive), which avoids substring
false positives (e.g. the `rust`/`cargo` toolchain does **not** match the game `Rust.exe`).

## What is on the list

Anti-cheats (the strongest signal: it shuts down even without the game open, e.g. Vanguard's
`vgc.exe`, which starts with Windows):

> Riot Vanguard · Easy Anti-Cheat (EAC/EOS) · BattlEye · nProtect GameGuard · XignCode3 ·
> PunkBuster · FACEIT · ESEA · miHoYo ACE/mhyprot · Denuvo Anti-Cheat · Ricochet (CoD) · Sabreshield

Online/multiplayer games with a ban risk (the world's main ones):

> Valorant · League of Legends/TFT · Fortnite · CS2/CS:GO · PUBG · Apex Legends ·
> Call of Duty (MW/Warzone/BO) · Rainbow Six Siege · Overwatch 2 · Diablo IV · Destiny 2 · Rust ·
> DayZ · Escape from Tarkov · GTA V Online/FiveM · RDR2 Online · Battlefield (2042/V/1/4/6) ·
> Roblox · Genshin Impact · Honkai Star Rail · Zenless Zone Zero · Wuthering Waves ·
> Dead by Daylight · Halo Infinite/MCC · Paladins · Smite · War Thunder · World of Tanks ·
> World of Warships · Dota 2 · Team Fortress 2 · Lost Ark · New World · Black Desert · ARK ·
> The Finals · Marvel Rivals · Hunt: Showdown · Sea of Thieves · For Honor · The Division 2 ·
> Warframe · Fall Guys · Naraka: Bladepoint · Delta Force · Splitgate 2 · Throne and Liberty ·
> Once Human · Gray Zone Warfare · Hell Let Loose · Star Citizen · EA SPORTS FC/FIFA

Full, always current list: `python -m server.guardian --list`, or the `ANTICHEATS` / `GAMES`
dictionaries in `server/guardian.py`. **To add a game**, put the executable's basename (lowercase)
in the list.

## Useful commands

```bash
python -m server.guardian --check    # pre-flight: exit 0 = clean, 3 = blocked
python -m server.guardian --list     # everything it blocks
```

Tests: `tests/test_guardian.py` covers detection (Linux and Windows), the notices and turning it
off; `tests/test_script.py` makes sure that, with the guardian failing, `run.sh` neither makes a
certificate nor starts the server. See [TESTING.md](TESTING.md).

## Turning it off (at your own risk)

```bash
LRD_GUARDIAN=off ./run.sh
```

Accepts `off/0/false/no/disable/disabled`. **Not recommended**: without the guardian there is no
protection against a ban at all. Use it only if you are 100% sure no online game with an
anti-cheat will run on that machine.

## Honest limitations

- The list goes by **process name**: a new or renamed game can slip through until it is added. It
  is a **safety net**, not an absolute guarantee; the final responsibility for not using the tool
  with online games is yours.
- It detects what is **on the machine itself** (the one that runs the server and is controlled),
  which is exactly where the anti-cheat would run.
- The monitor window is ~2 s (`LRD_GUARDIAN_INTERVAL`); the pre-flight covers anything already open
  (zero window).
