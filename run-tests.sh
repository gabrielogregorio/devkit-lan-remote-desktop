#!/usr/bin/env bash
# Single runner for the LAN Remote Desktop test suite. Same commands the CI runs.
#
#   ./run-tests.sh            -> lint + server (pytest, 100% coverage gate) + web client (vitest + jsdom)
#   RUN_E2E=1 ./run-tests.sh  -> also the Playwright E2E (real server on 127.0.0.1:18443 + Chromium)
#
# Nothing here opens /dev/uinput: the unit tests fake it and the E2E server runs with
# LRD_INPUT=off, so no test moves the PC's mouse. The E2E needs certs/cert.pem (./run.sh makes it).
set -u
cd "$(dirname "$0")" || exit

PY="${PY:-.venv/bin/python}"
FAILED=0
declare -a RESULTS

bar() { printf '%s\n' "------------------------------------------------------------"; }
run_layer() {  # run_layer <name> <command...>
  local name="$1"; shift
  bar; echo ">> $name"; bar
  if "$@"; then RESULTS+=("PASS  $name"); else RESULTS+=("FAIL  $name"); FAILED=1; fi
}

# ---------- lint (ruff from the venv; shellcheck when installed) ----------
run_layer "lint     (ruff)" "$PY" -m ruff check .
if command -v shellcheck > /dev/null; then
  run_layer "lint     (shellcheck)" shellcheck --severity=warning ./*.sh
else
  RESULTS+=("SKIP  lint (shellcheck) - sudo apt install shellcheck")
fi

# ---------- server (pytest, fakes for uinput/ffmpeg/xrandr/proc, 100% gate) ----------
run_layer "server   (pytest + coverage)" "$PY" -m pytest --cov=server --cov-report=term

# ---------- web client (vitest + jsdom, thresholds in vitest.config.js) ----------
run_layer "web      (vitest)" npm run --silent test:web

# ---------- E2E (opt-in) ----------
if [ "${RUN_E2E:-0}" = "1" ]; then
  run_layer "e2e      (Playwright, real server)" npm run --silent test:e2e -- --reporter=line
else
  RESULTS+=("SKIP  e2e (Playwright) - RUN_E2E=1 to run it")
fi

# ---------- scoreboard ----------
echo ""; bar; echo "SCOREBOARD"; bar
for r in "${RESULTS[@]}"; do echo "  $r"; done
bar
if [ "$FAILED" -eq 0 ]; then echo "ALL GREEN ✅"; else echo "FAILURES ❌"; fi
exit "$FAILED"
