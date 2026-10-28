# CLAUDE.md - LAN Remote Desktop

Operating guide for any agent (Claude Code) working in this repository. Read it before editing.

## What it is

A tool that **shares the screen of an Ubuntu PC** (this host) with **any device on the local
network through the browser** (tablet, Android, another Ubuntu/laptop), with **mouse, keyboard and
Xbox gamepad control**. Focus: **high fps and the lowest possible input lag** on the LAN, started
with one command: `./run.sh`.

## Architecture (the central decision - do not regress to VNC/WebRTC without a reason)

```
X11 screen ──ffmpeg HW H.264──> Access Units (AUD) ──WSS /ws/video──> WebCodecs ──> <canvas>
  (NVENC > VAAPI > x264)                                               (HW decode on the device)

sound ──ffmpeg Opus──> 1 Opus pkt/msg ──WSS /ws/audio──> AudioDecoder ──> AudioContext
  (sink monitor)                                          (WebCodecs)

device ──mouse/keyboard/gamepad (JSON)──> WSS /ws/input ──> /dev/uinput (evdev) ──> Ubuntu
```

Why:
- **WebCodecs** (frame-by-frame decode) avoids the MSE/HLS buffer → the lowest lag in a browser,
  with nothing installed on the client.
- Every H.264 Access Unit starts with an **AUD** (NAL type 9), so the stream is sliced trivially and
  each AU becomes one `EncodedVideoChunk`.
- **Hardware encode**, autodetected, tuned *ultra-low-latency* (no B-frames, CBR, GOP ~1 s for a
  fast resync of new clients).
- **Audio:** ffmpeg only muxes Opus into Ogg, so `audio.py` has an **incremental Ogg parser**
  (`OggOpusParser`) that rebuilds the packets through lacing and drops the 2 headers
  (OpusHead/OpusTags); each Opus packet becomes one `EncodedAudioChunk`. The source uses the
  `@DEFAULT_MONITOR@` token (follows the default sink without `pactl`, which does not exist on pure
  PipeWire). The client schedules playback with a short jitter buffer (~60 ms).
- **uinput** gives a **virtual Xbox 360 gamepad** (`045e:028e`) that games and Steam see as real.

## Layout

```
run.sh              # the only entry point: detects display/IP, venv, TLS cert, starts the server
run-tests.sh        # every test layer, same commands as the CI
tray_icon.py        # top bar icon (system python3 + AppIndicator): starts/stops ./run.sh, QR code;
                    #   --install puts it in the login autostart, off; strings in its own PT_BR
server/
  main.py           # aiohttp: HTTPS + WSS /ws/video /ws/audio /ws/input; Hub + AudioHub; /api/*
  video.py          # detect_encoder(), presets, get_monitors(), ScreenEncoder (ffmpeg),
                    #   'vq' sanitizers (clamp_res/clamp_fps/clean_bitrate)
  inject.py         # Injector: absolute pointer + keyboard + Xbox 360 pad via uinput; NullInjector
  keymap.py         # KeyboardEvent.code -> evdev ecodes
  audio.py          # AudioEncoder (ffmpeg Opus), OggOpusParser, audio presets, list_monitors()
  gamepad.py        # remap profile persistence (~/.config/devkit-lan-remote-desktop/gamepad-map.json)
  guardian.py       # anti-ban escape hatch: blocks/brings down on anti-cheat or online game
  i18n.py           # guardian notice strings: English source + PT_BR, tr()
web/
  index.html / style.css / app.js   # WebCodecs client (no build, plain JS)
  i18n.js                           # page strings: English source + PT_BR, translator
  gamepad-debug.html                # gamepad diagnostics page
tests/              # pytest (server): unit, integration, fuzz, parsers, script, docs, i18n
web/__tests__/      # vitest + jsdom (client)
web/e2e/            # playwright (real server + real browser)
pyproject.toml      # deps, pytest, coverage gate, ruff; uv.lock + requirements*.txt are exports
vitest.config.js  playwright.config.js  package.json  package-lock.json
certs/  .venv/  node_modules/   # generated - NEVER commit (they are in .gitignore)
```

## Host requirements

- **X11** session (Xorg). `run.sh` autodetects `DISPLAY`/`XAUTHORITY` from the environ of
  `gnome-shell` (or cinnamon/plasmashell/xfwm4/Xorg), falling back to the gdm Xauthority or
  `~/.Xauthority`.
- Encoder: NVIDIA with the proprietary driver → `nvenc` (`h264_nvenc -tune ull`); Intel/AMD with
  VAAPI → `vaapi`; anything else → `x264`.
- `/dev/uinput` writable by the user (`./run.sh setup`: udev rule + `input` group + ACL).
- `ffmpeg`, `openssl`, `python3` on the system.

## How to run / test

```bash
./run.sh                 # production (port 8443)
./run.sh --port 9000
./run.sh setup           # grants /dev/uinput (once, with sudo)
```

**Before committing, run the suites** (details and a map of what each file covers in
**`TESTING.md`**):

```bash
./run-tests.sh             # ruff + shellcheck + pytest (100% gate) + vitest
RUN_E2E=1 ./run-tests.sh   # + playwright (starts the real server with LRD_INPUT=off + Chromium)
```

Quick syntax checks (they do **not** replace the suites):

```bash
python3 -m py_compile server/*.py && node --check web/app.js && bash -n run.sh
```

| Variable | Effect |
|---|---|
| `LRD_PRESET` | starting video preset: `ultra`/`high`/`medium`/`low`/`potato` |
| `LRD_ENCODER` | forces `nvenc`/`vaapi`/`x264` (skips the autodetect) |
| `LRD_RENDER_NODE` | VAAPI DRM node (default `/dev/dri/renderD128`) |
| `LRD_AUDIO_PRESET` | starting audio preset: `high`/`medium`/`low`/`voice` |
| `LRD_AUDIO_SOURCE` | forces the audio source (default `@DEFAULT_MONITOR@`) |
| `LRD_PORT` / `LRD_HOST` | port and bind (default `8443` / `0.0.0.0`) |
| `LRD_CERT` / `LRD_KEY` | TLS pair path (`run.sh` makes it in `certs/` when missing) |
| `LRD_DISPLAY` / `LRD_XAUTHORITY` | X display (`run.sh` autodetects it from `gnome-shell`) |
| `LRD_GAMEPAD_MAP` | remap profiles file (default `~/.config/devkit-lan-remote-desktop/`) |
| `LRD_INPUT` | `off` = no-op injector, never opens `/dev/uinput` (view only; the E2E uses it) |
| `LRD_GUARDIAN` | `off` turns the anti-ban escape hatch off - see `ANTIBAN.md` |
| `LRD_GUARDIAN_INTERVAL` | seconds between guardian scans (default `2.0`) |
| `LRD_LANG` | `en`/`pt-BR` for the guardian notices (default: system language) |

The tests use `LRD_GUARDIAN=off` and `LRD_ENCODER=x264` (`tests/conftest.py`) so they depend on
neither the hardware nor the escape hatch.

### Careful when testing locally
- WebCodecs needs a **secure context** → the server only makes sense over **HTTPS**. Without a
  cert it warns and WebCodecs does not run outside `localhost`.
- The encoder **only starts with ≥1 viewer** on `/ws/video` (and stops when the last one leaves).
  Do not expect ffmpeg running without a client.
- The WebCodecs render needs a real browser for visual validation; `py_compile`/`node --check` only
  catch syntax.
- `pytest` and `vitest` run without X11, GPU and `/dev/uinput` (everything is faked). Only the E2E
  starts a real server, with `LRD_INPUT=off`; the screen capture test skips itself without
  `DISPLAY`. Never run the E2E server with a real injector: garbage on the input channel would move the
  mouse.
- `web/app.js` ends with a **test seam**: `module.exports = {...}` when `module` exists
  (Node/vitest), else `boot()`. **Do not remove it** - it is what allows testing the client without
  a bundler. A new function that needs a test goes into that list.

## HTTP endpoints

- `GET /api/monitors` - current config: encoder, monitors, presets, `video` (current
  res/fps/bitrate), ranges (`res_options`/`fps_options`/`bitrate_options`), audio presets and
  `audio_manual`.
- `GET /api/gamepad-map` - every saved remap profile (by `gamepad.id`). `POST` with `{"id","map"}`
  saves; `DELETE` with `{"id"}` deletes. Persisted by `server/gamepad.py` in
  `~/.config/devkit-lan-remote-desktop/gamepad-map.json` (or `LRD_GAMEPAD_MAP`). The server only
  stores/serves the JSON (opaque blob).
- `GET /gamepad-debug.html` - diagnostics page: shows `mapping`, every `buttons[i].value` and
  `axes[i]` live. Use it to find out how a controller shows up before touching a mapping.

Gamepad - **pass-through by default**: if `gamepad.mapping === "standard"` (Xbox and most), the
client forwards it byte by byte in W3C order (already the order the server wants) and **ignores any
profile** - an Xbox never needs a remap. The remap (`🎮`) only applies to and only runs on a
**non-standard** controller (PS2/USB clones); `🎮↺` deletes the profile and goes back to
pass-through. LT/RT triggers are replicated exactly (`clamp01` on the client, `round(v*255)` on the
server) - no deadzone/rescale.

`pollPads()` only resends when the state changes **at the destination resolution** (axis → int16,
button/trigger → byte 0..255): the value on the wire is still the raw one, but comparing floats
would make the controller's micro-noise send a message 60×/s for nothing.

## WebSocket protocol

`/ws/video` (binary): server → client, each message = 1 H.264 Annex-B Access Unit. **Text**
messages = control, e.g. `{"t":"reset"}` (the client must recreate the `VideoDecoder` and wait for
the next keyframe).

`/ws/audio` (binary): server → client, each message = 1 Opus packet (48 kHz, stereo, 20 ms frame).
The client decodes with `AudioDecoder` and schedules it on the `AudioContext`. Only on with ≥1
listener.

`/ws/input` (JSON text): client → server.
- `{"t":"m","x":0..1,"y":0..1}` move the mouse (absolute, normalized inside the region)
- `{"t":"mr","dx","dy"}` relative move (pointer lock)
- `{"t":"md","b":N}` / `{"t":"mu","b":N}` mouse button down/up
- `{"t":"w","x","y"}` scroll
- `{"t":"k","code":"KeyA","down":true}` key
- `{"t":"g","a":[lx,ly,rx,ry],"b":[{"p":bool,"v":float},...],"lt":0..1,"rt":0..1}` gamepad.
  `lt`/`rt` are the triggers sent explicitly (the client detects them even on a non-standard
  mapping, e.g. a PS2/USB controller where they come as axes); the server falls back to
  `b[6]/b[7]` only when they are missing.
- `{"t":"q","preset":"high"}` preset shortcut: applies resolution+fps+bitrate at once. The audio
  follows (`main.AUDIO_FOR_QUALITY`) **only** if the user has not touched the audio by hand
  (`AudioHub.manual`). Restarts the encoders.
- `{"t":"vq","res":720,"fps":60,"bitrate":"7M"}` **independent video** quality (any subset of the
  3 fields); becomes the "custom" preset.
- `{"t":"aq","preset":"medium"}` **independent audio** quality (sets `AudioHub.manual=True` → turns
  off the `q` drag).
- `{"t":"mon","i":0}` switch monitor (`i:null` = primary)

### Input channel robustness (a rule, not a detail)

`/ws/input` is an **open LAN**: anything can arrive on it, and an exception escaping the handler
**drops the whole WebSocket** - the user loses mouse and keyboard until the client reconnects.
Therefore:

- a payload that is not a JSON object (`[]`, `5`, `null`, `"txt"`) is **ignored**;
- a non-string `preset` is ignored (`x in DICT` with a list/dict raises `TypeError: unhashable`);
- `vq` goes through `video.clamp_res` / `clamp_fps` / `clean_bitrate`: out of range is **clamped**,
  garbage **keeps the current value**. Ranges: height 144-2160, fps 5-240, bitrate 100 kbps-100 Mbps
  in ffmpeg format (`12M`/`3500k`/`800000`);
- `mon` with a non-integer index is ignored;
- the rest goes to `injector.handle()` inside `try/except` (mouse/keyboard fail clean before writing
  anything);
- `gamepad_state()` is **total**: wrong type/NaN/missing field becomes neutral (released/centered)
  and the frame **always** ends in `syn()` - a half-written frame is what makes stuck buttons and
  ghost triggers;
- `/api/gamepad-map` answers **400** (not 500) for JSON that is not an object.

The client (`web/app.js`) does the mirror: `padNum`/`clamp01` make sure no NaN/Infinity ever goes
out in the JSON, and `pollPads()` runs inside `try/catch` - if the exception escaped, the
`requestAnimationFrame` at the end would not run and **the gamepad would die for the rest of the
session**. When touching these paths, run `tests/test_fuzz_input.py` and
`web/__tests__/protocol.test.js`.

H.264 encoder tuned for low latency: GOP ~1 s (fast resync for a new client without a long black
screen; on the LAN bandwidth is plenty), small VBV (`_lowlat_bufsize` ~1/3 of the bitrate/s),
spatial-AQ on NVENC (sharpness at the same bitrate), `high` profile, `no-scenecut`+`forced-idr`. An
NVENC encode holds 1080p60 at 1x; the practical bottleneck is usually the **decode in the browser**,
so the useful lever is lowering resolution/bitrate (separate controls).

The client (`web/app.js`) **drops delta frames until the 1st keyframe** after
`configure()`/reset (`needKey`): `VideoDecoder` requires a keyframe as the 1st chunk, otherwise
Firefox and Chrome fail with "key frame is required".

## i18n

- Code, comments, logs, docs and commits in **English**.
- The page shows English or Brazilian Portuguese: `?lang=` wins, else the browser's first language
  (`pt*` → pt-BR). English text is the key; the pt-BR line lives in `PT_BR` in `web/i18n.js`. Use
  `t("…")` in `app.js` (with `{placeholders}`) or `data-i18n` attributes in the HTML.
- The guardian notices (terminal banner and dialog) go through `tr("…")` with `PT_BR` in
  `server/i18n.py`; language from `LRD_LANG`, then `LANGUAGE`/`LC_ALL`/`LC_MESSAGES`/`LANG`.
- `tests/test_i18n.py` and `web/__tests__/i18n.test.js` fail on a missing line, on different
  placeholders or on a line no longer used.

## Conventions

- The web client is **plain JS, no build/bundler** - keep it that way.
- Latency over features. Before adding buffering/quality, think about the lag.
- Never commit `certs/` (private key) or `.venv/`.
- **Every new protocol field is born with sanitizing and a test**: an unexpected value cannot drop a
  connection or the encoder (see "Robustness" above).
- **Docs together with the code.** `tests/test_docs_sync.py` fails if an endpoint, a message type, a
  preset or an `LRD_*` variable exists in the code and does not show up here/in the README. If the
  test complains, the docs are what is wrong.
- Server coverage has a **100%** gate (`pyproject.toml`); the client has thresholds in
  `vitest.config.js`. New code comes with a test in the same commit.
- Dependencies: change `pyproject.toml`, then `uv lock` and export both `requirements*.txt`
  (commands in the README). Node packages at exact versions in `package.json`.
- Commits: one line, English, imperative.

## Possible next steps (not done yet)

- Picking a specific **audio app** in the UI (today it captures the sink mix).
- A "whole desktop" mode (every monitor in one stream).
- **Wayland** capture through the PipeWire portal (today X11/`x11grab` only).
- Adaptive bitrate by RTT/loss (video and audio).
- Authentication (PIN/token + QR code) - today it is an open LAN behind the self-signed TLS.
- Measuring the real glass-to-glass latency (the bar shows the interval between frames).
