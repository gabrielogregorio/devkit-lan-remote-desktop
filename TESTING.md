# Tests - LAN Remote Desktop

How the project is tested, what each layer guarantees and where a new test fits. Architecture
guide: [CLAUDE.md](CLAUDE.md).

## Run everything

```bash
./run-tests.sh             # ruff + ShellCheck + server (100% gate) + web client
RUN_E2E=1 ./run-tests.sh   # + end to end (real server + Chromium)
```

Or layer by layer:

```bash
.venv/bin/python -m pytest --cov=server    # server  (gate: 100%)
npm run test:web                           # client  (vitest + jsdom)
npm run test:e2e                           # end to end (real server + Chromium)
```

None of it needs X11, a GPU, `/dev/uinput` or the network, except `test:e2e`, which starts the real
`server.main` with `LRD_GUARDIAN=off LRD_INPUT=off` (so no test opens `/dev/uinput` or moves the
mouse) and needs `certs/cert.pem` (`./run.sh` makes it; the CI makes a throwaway one). The test that
captures the screen skips itself without `DISPLAY`. The unit tests force `LRD_GUARDIAN=off` and
`LRD_ENCODER=x264` (`tests/conftest.py`) so they do not depend on this machine's hardware.

Gates: `pyproject.toml` (`[tool.coverage]`) requires **100%** line + branch coverage on `server/`;
`vitest.config.js` requires 95% of lines/functions/statements and 90% of branches on the client.
New code comes with its test in the same commit.

## The layers (and why each one exists)

| Layer | Where | Answers |
|---|---|---|
| unit | `tests/test_*.py`, `web/__tests__/app.test.js` | does this function do what it promises? |
| integration | `tests/test_integration_ws.py` | do the parts talk over a real WebSocket? |
| property/fuzz | `tests/test_fuzz_input.py`, `tests/test_parsers_property.py`, `web/__tests__/protocol.test.js` | does it hold for *any* input, not only the example? |
| entry point | `tests/test_script.py` | does `./run.sh` do the right thing? |
| e2e | `web/e2e/*.spec.js` | does it work in a real browser against the real server? |
| i18n | `tests/test_i18n.py`, `web/__tests__/i18n.test.js` | does every user-facing string have its pt-BR line? |
| anti-drift | `tests/test_docs_sync.py` | do the docs still describe this code? |

### Server (`tests/`)

- **`test_main.py`** - `Hub`/`AudioHub`, queues, encoder lifecycle, routes.
- **`test_video.py`** - encoder detection, presets, `xrandr`, the ffmpeg command, AU slicing,
  supervision and retry.
- **`test_audio.py`** - Opus presets, `OggOpusParser`, audio sources.
- **`test_inject.py`** - evdev writes for mouse, keyboard and gamepad; the no-op injector.
- **`test_keymap.py`** - the `KeyboardEvent.code` → evdev code map.
- **`test_gamepad.py`** - remap profile persistence.
- **`test_guardian.py`** - anti-ban escape hatch (Linux and Windows).
- **`test_i18n.py`** - every `tr("…")` in `server/` has a pt-BR line with the same placeholders.
- **`test_tray_icon.py`** - the top bar icon without GTK: icons, the port/process checks, its texts
  and pt-BR catalog, `--install`/`--remove` and one icon per app.
- **`test_integration_ws.py`** - starts the aiohttp app and talks over the WebSockets the way the
  client does: keyframe gating, fan-out, `reset` on preset change, audio following (or not) the
  video preset, monitor switch, and the battery of malformed payloads that **must not** drop the
  input channel.
- **`test_fuzz_input.py`** - deterministic fuzz of the `Injector` and of the quality sanitizers.
  Invariant: what reaches `/dev/uinput` is **always** inside the range declared on the device, and
  every gamepad frame ends in `syn()`.
- **`test_parsers_property.py`** - the two incremental parsers (Ogg/Opus and H.264 Access Units).
  Core property: **the result does not depend on how the stream was split** - the same stream is
  cut in dozens of random ways and the output must be identical.
- **`test_script.py`** - runs `run.sh` for real in a sandbox (a copy in `tmp`, `PATH` with only
  fakes of `ffmpeg`/`openssl`/`python3`/`sudo`…). Covers the port, TLS, the guardian blocking,
  uinput without permission, `setup` mode.

### Client (`web/__tests__/`)

- **`app.test.js`** - by example: decoder, audio, mouse/touch/keyboard, on-screen keyboard, remap,
  UI, stats, boot.
- **`protocol.test.js`** - contract and property: every command sent is a `t` the server knows, no
  field becomes `null`/`NaN` in the JSON, and `mapPad`/`readSource` produce a valid frame for
  **any** controller (including a corrupt profile and a pad without arrays). It mirrors
  `test_fuzz_input.py`.
- **`gamepad-debug.test.js`** - the diagnostics page: rendering and the min/max and oscillation
  history (the broken trigger detector).
- **`i18n.test.js`** - every `t("…")` and `data-i18n` text has a pt-BR line with the same
  placeholders, no unused line, language detection.

### E2E (`web/e2e/`)

- **`smoke.spec.js`** - the UI loads, connects, the canvas gets a real frame, preset and sound work,
  the page in pt-BR.
- **`api.spec.js`** - HTTP contract against the real server: `/api/monitors` shape, the full gamepad
  profile cycle, 400 for an invalid body, the debug page, and garbage on `/ws/input` without
  dropping the channel.

## Writing a new test

**Find the right layer.** Pure logic → unit. "Do the parts talk?" → integration. "Does it hold for
any input?" → property. Only in the browser → e2e.

**Fuzz is deterministic.** Fixed seeds (`SEEDS = [...]`), no unseeded `random.random()` and no
`Date.now()`. If it failed once, it fails every time; otherwise the test becomes noise and someone
turns it off.

**Fakes only at the edge.** Fake `/dev/uinput`, `ffmpeg`, `xrandr`, `/proc` and subprocess; the rest
runs for real. The fakes live in `tests/helpers.py` (Python) and `web/__tests__/setup.js` (JS);
reuse them before making another.

**A robustness test asserts survival, not shape.** What matters is not "which error came back" but
that the connection stays alive, the encoder keeps running and nothing was written out of range.

**A new client function** that needs a test must go into the `module.exports` at the end of
`web/app.js` (the seam that allows testing without a bundler).

**Changed the protocol, an `/api/*` route, a preset or an `LRD_*` variable?** Update
`CLAUDE.md`/`README.md` with it: `tests/test_docs_sync.py` compares what the code exposes with what
the docs describe and fails if they drift.
