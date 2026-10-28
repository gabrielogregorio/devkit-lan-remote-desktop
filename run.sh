#!/usr/bin/env bash
#
# LAN Remote Desktop - shares the Ubuntu screen with any device on the local
# network (browser) with mouse, keyboard and Xbox gamepad control. Low latency.
#
#   ./run.sh            start the server
#   ./run.sh setup      grant access to /dev/uinput (once, with sudo)
#   ./run.sh --port N   port (default 8443)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PORT="${LRD_PORT:-8443}"
CERT_DIR="$HERE/certs"
CERT="$CERT_DIR/cert.pem"
KEY="$CERT_DIR/key.pem"

c_blue=$'\e[1;34m'; c_grn=$'\e[1;32m'; c_yel=$'\e[1;33m'; c_red=$'\e[1;31m'; c_dim=$'\e[2m'; c_off=$'\e[0m'
say()  { printf '%s\n' "${c_blue}▶${c_off} $*"; }
ok()   { printf '%s\n' "${c_grn}✓${c_off} $*"; }
warn() { printf '%s\n' "${c_yel}!${c_off} $*"; }
die()  { printf '%s\n' "${c_red}✗${c_off} $*" >&2; exit 1; }

# ---------------------------------------------------------------- args
# while+shift (not `for a in "$@"` with a shift inside): changing the list while
# iterating over it makes "setup --port N" read the wrong argument.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:-$PORT}"; shift 2 2>/dev/null || shift;;
    --port=*) PORT="${1#*=}"; shift;;
    setup) DO_SETUP=1; shift;;
    *) shift;;
  esac
done

# ---------------------------------------------------------------- setup (uinput)
if [[ "${DO_SETUP:-}" == "1" ]]; then
  say "Granting access to /dev/uinput (needs sudo)…"
  sudo modprobe uinput || true
  echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput", TAG+="uaccess"' \
    | sudo tee /etc/udev/rules.d/99-lan-remote-desktop-uinput.rules >/dev/null
  sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput || true
  sudo usermod -aG input "$USER" || true
  sudo setfacl -m u:"$USER":rw /dev/uinput 2>/dev/null || true
  ok "Done. If permission is still missing, log out and back in once."
  exit 0
fi

# ---------------------------------------------------------------- requirements
command -v ffmpeg  >/dev/null || die "ffmpeg not found (sudo apt install ffmpeg)"
command -v openssl >/dev/null || die "openssl not found (sudo apt install openssl)"
command -v python3 >/dev/null || die "python3 not found"

# ---------------------------------------------------------------- X11 display
detect_x() {
  local pid env d xa
  for pid in $(pgrep -u "$USER" -x gnome-shell 2>/dev/null || true) \
             $(pgrep -u "$USER" -f 'cinnamon|plasmashell|xfwm4|Xorg' 2>/dev/null || true); do
    env="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null || true)"
    d="$(printf '%s\n' "$env" | sed -n 's/^DISPLAY=//p' | head -1)"
    xa="$(printf '%s\n' "$env" | sed -n 's/^XAUTHORITY=//p' | head -1)"
    [[ -n "$d" ]] && { echo "${d}|${xa}"; return; }
  done
  echo "${DISPLAY:-:0}|${XAUTHORITY:-}"
}
IFS='|' read -r LRD_DISPLAY LRD_XAUTHORITY <<< "$(detect_x)"
[[ -z "${LRD_XAUTHORITY:-}" && -f "/run/user/$(id -u)/gdm/Xauthority" ]] && LRD_XAUTHORITY="/run/user/$(id -u)/gdm/Xauthority"
[[ -z "${LRD_XAUTHORITY:-}" && -f "$HOME/.Xauthority" ]] && LRD_XAUTHORITY="$HOME/.Xauthority"
export LRD_DISPLAY LRD_XAUTHORITY
if ! DISPLAY="$LRD_DISPLAY" XAUTHORITY="$LRD_XAUTHORITY" xdpyinfo >/dev/null 2>&1; then
  warn "Could not confirm X access on DISPLAY=$LRD_DISPLAY (xdpyinfo). Trying anyway."
fi
ok "X11 display: ${c_dim}DISPLAY=$LRD_DISPLAY XAUTHORITY=$LRD_XAUTHORITY${c_off}"

# ---------------------------------------------------------------- python deps
if python3 -c "import aiohttp, evdev" >/dev/null 2>&1; then
  PY=python3
  ok "Python dependencies: ${c_dim}aiohttp + evdev (system)${c_off}"
else
  say "Creating the venv and installing dependencies…"
  [[ -d .venv ]] || python3 -m venv --system-site-packages .venv
  ./.venv/bin/pip -q install -r requirements.txt
  PY="./.venv/bin/python"
  ok "venv ready"
fi

# ---------------------------------------------------------------- escape hatch (anti-ban)
# Runs BEFORE touching uinput/TLS/ffmpeg. If an anti-cheat or an online game is
# running (e.g. Vanguard/vgc.exe), the guardian shows the notice and exits != 0,
# and the server is never started, so there is no ban risk. See server/guardian.py.
if ! "$PY" -m server.guardian --check; then
  die "LAN Remote Desktop blocked by the guardian (ban risk). Close the game/anti-cheat and try again."
fi

# ---------------------------------------------------------------- uinput
if ! "$PY" -c "from evdev import UInput; UInput().close()" >/dev/null 2>&1; then
  warn "No permission on /dev/uinput. Run once:  ${c_blue}./run.sh setup${c_off}"
  warn "Going on; mouse/keyboard/gamepad control may not work until then."
else
  ok "/dev/uinput access OK (mouse/keyboard/gamepad)"
fi

# ---------------------------------------------------------------- TLS (self-signed)
mkdir -p "$CERT_DIR"
LAN_IPS="$(hostname -I 2>/dev/null || echo 127.0.0.1)"
if [[ ! -f "$CERT" || ! -f "$KEY" ]]; then
  say "Creating a self-signed TLS certificate…"
  SAN="DNS:localhost,DNS:$(hostname),IP:127.0.0.1"
  for ip in $LAN_IPS; do SAN="$SAN,IP:$ip"; done
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$KEY" -out "$CERT" -subj "/CN=lan-remote-desktop" \
    -addext "subjectAltName=$SAN" >/dev/null 2>&1
  ok "Certificate created (${c_dim}certs/cert.pem${c_off})"
fi

# ---------------------------------------------------------------- go
PRIMARY_IP="$(echo "$LAN_IPS" | tr ' ' '\n' | grep -E '^192\.|^10\.|^172\.(1[6-9]|2[0-9]|3[01])\.' | grep -v '^172\.1[7-9]\.' | head -1)"
PRIMARY_IP="${PRIMARY_IP:-$(echo "$LAN_IPS" | awk '{print $1}')}"

echo
echo "${c_grn}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${c_off}"
echo "  ${c_grn}LAN Remote Desktop${c_off} ready - open it on the tablet/phone/laptop:"
echo
echo "     ${c_blue}https://${PRIMARY_IP}:${PORT}${c_off}"
echo
echo "  ${c_dim}other IPs:${c_off}"
for ip in $LAN_IPS; do
  [[ "$ip" == "$PRIMARY_IP" ]] && continue
  echo "     ${c_dim}https://${ip}:${PORT}${c_off}"
done
echo
echo "  ${c_yel}First time:${c_off} accept the certificate warning (self-signed)."
echo "  ${c_dim}Chrome/Edge recommended (WebCodecs). Ctrl+C to stop.${c_off}"
echo "${c_grn}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${c_off}"
echo

export LRD_HOST="${LRD_HOST:-0.0.0.0}" LRD_PORT="$PORT" LRD_CERT="$CERT" LRD_KEY="$KEY"
export PYTHONUNBUFFERED=1
exec "$PY" -u -m server.main
