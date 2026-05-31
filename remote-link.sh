#!/usr/bin/env bash
#
# remote-link - compartilha a tela do Ubuntu para qualquer device da rede local
# (navegador) com controle de mouse/teclado/gamepad Xbox. Baixa latencia.
#
#   ./remote-link.sh            inicia o servidor
#   ./remote-link.sh setup      configura acesso ao /dev/uinput (uma vez, com sudo)
#   ./remote-link.sh --port N   porta (padrao 8443)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PORT="${RL_PORT:-8443}"
CERT_DIR="$HERE/certs"
CERT="$CERT_DIR/cert.pem"
KEY="$CERT_DIR/key.pem"

c_blue=$'\e[1;34m'; c_grn=$'\e[1;32m'; c_yel=$'\e[1;33m'; c_red=$'\e[1;31m'; c_dim=$'\e[2m'; c_off=$'\e[0m'
say()  { printf '%s\n' "${c_blue}▶${c_off} $*"; }
ok()   { printf '%s\n' "${c_grn}✓${c_off} $*"; }
warn() { printf '%s\n' "${c_yel}!${c_off} $*"; }
die()  { printf '%s\n' "${c_red}✗${c_off} $*" >&2; exit 1; }

# ---------------------------------------------------------------- args
for a in "$@"; do
  case "$a" in
    --port) shift; PORT="${1:-$PORT}";;
    --port=*) PORT="${a#*=}";;
    setup) DO_SETUP=1;;
  esac
done

# ---------------------------------------------------------------- setup (uinput)
if [[ "${DO_SETUP:-}" == "1" ]]; then
  say "Configurando acesso ao /dev/uinput (precisa de sudo)…"
  sudo modprobe uinput || true
  echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput", TAG+="uaccess"' \
    | sudo tee /etc/udev/rules.d/99-remote-link-uinput.rules >/dev/null
  sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput || true
  sudo usermod -aG input "$USER" || true
  sudo setfacl -m u:"$USER":rw /dev/uinput 2>/dev/null || true
  ok "Pronto. Se ainda faltar permissao, faça logout/login uma vez."
  exit 0
fi

# ---------------------------------------------------------------- pré-requisitos
command -v ffmpeg  >/dev/null || die "ffmpeg nao encontrado (sudo apt install ffmpeg)"
command -v openssl >/dev/null || die "openssl nao encontrado (sudo apt install openssl)"
command -v python3 >/dev/null || die "python3 nao encontrado"

# ---------------------------------------------------------------- display X11
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
IFS='|' read -r RL_DISPLAY RL_XAUTHORITY <<< "$(detect_x)"
[[ -z "${RL_XAUTHORITY:-}" && -f "/run/user/$(id -u)/gdm/Xauthority" ]] && RL_XAUTHORITY="/run/user/$(id -u)/gdm/Xauthority"
[[ -z "${RL_XAUTHORITY:-}" && -f "$HOME/.Xauthority" ]] && RL_XAUTHORITY="$HOME/.Xauthority"
export RL_DISPLAY RL_XAUTHORITY
if ! DISPLAY="$RL_DISPLAY" XAUTHORITY="$RL_XAUTHORITY" xdpyinfo >/dev/null 2>&1; then
  warn "Nao confirmei acesso ao X em DISPLAY=$RL_DISPLAY (xdpyinfo). Tentando mesmo assim."
fi
ok "Display X11: ${c_dim}DISPLAY=$RL_DISPLAY XAUTHORITY=$RL_XAUTHORITY${c_off}"

# ---------------------------------------------------------------- python deps
if python3 -c "import aiohttp, evdev" >/dev/null 2>&1; then
  PY=python3
  ok "Dependencias Python: ${c_dim}aiohttp + evdev (sistema)${c_off}"
else
  say "Criando venv e instalando dependencias…"
  [[ -d .venv ]] || python3 -m venv --system-site-packages .venv
  ./.venv/bin/pip -q install -r requirements.txt
  PY="./.venv/bin/python"
  ok "venv pronto"
fi

# ---------------------------------------------------------------- escape hatch (anti-ban)
# Roda ANTES de mexer em uinput/TLS/ffmpeg. Se achar anticheat ou jogo online
# rodando (ex.: Vanguard/vgc.exe), o guardian mostra o aviso e sai != 0 - aqui a
# gente nem inicia o servidor, pra nao arriscar banimento. Ver server/guardian.py.
if ! "$PY" -m server.guardian --check; then
  die "remote-link bloqueado pelo guardian (risco de banimento). Feche o jogo/anticheat e tente de novo."
fi

# ---------------------------------------------------------------- uinput
if ! "$PY" -c "from evdev import UInput; UInput().close()" >/dev/null 2>&1; then
  warn "Sem permissao em /dev/uinput. Rode uma vez:  ${c_blue}./remote-link.sh setup${c_off}"
  warn "Vou continuar; o controle de mouse/teclado/gamepad pode nao funcionar ate isso."
else
  ok "Acesso ao /dev/uinput OK (mouse/teclado/gamepad)"
fi

# ---------------------------------------------------------------- TLS (self-signed)
mkdir -p "$CERT_DIR"
LAN_IPS="$(hostname -I 2>/dev/null || echo 127.0.0.1)"
if [[ ! -f "$CERT" || ! -f "$KEY" ]]; then
  say "Gerando certificado TLS self-signed…"
  SAN="DNS:localhost,DNS:$(hostname),IP:127.0.0.1"
  for ip in $LAN_IPS; do SAN="$SAN,IP:$ip"; done
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$KEY" -out "$CERT" -subj "/CN=remote-link" \
    -addext "subjectAltName=$SAN" >/dev/null 2>&1
  ok "Certificado criado (${c_dim}certs/cert.pem${c_off})"
fi

# ---------------------------------------------------------------- go
PRIMARY_IP="$(echo "$LAN_IPS" | tr ' ' '\n' | grep -E '^192\.|^10\.|^172\.(1[6-9]|2[0-9]|3[01])\.' | grep -v '^172\.1[7-9]\.' | head -1)"
PRIMARY_IP="${PRIMARY_IP:-$(echo "$LAN_IPS" | awk '{print $1}')}"

echo
echo "${c_grn}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${c_off}"
echo "  ${c_grn}remote-link${c_off} pronto - abra no tablet/celular/notebook:"
echo
echo "     ${c_blue}https://${PRIMARY_IP}:${PORT}${c_off}"
echo
echo "  ${c_dim}outros IPs:${c_off}"
for ip in $LAN_IPS; do
  [[ "$ip" == "$PRIMARY_IP" ]] && continue
  echo "     ${c_dim}https://${ip}:${PORT}${c_off}"
done
echo
echo "  ${c_yel}1ª vez:${c_off} aceite o aviso de certificado (self-signed)."
echo "  ${c_dim}Chrome/Edge recomendado (WebCodecs). Ctrl+C para parar.${c_off}"
echo "${c_grn}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${c_off}"
echo

export RL_HOST="${RL_HOST:-0.0.0.0}" RL_PORT="$PORT" RL_CERT="$CERT" RL_KEY="$KEY"
export PYTHONUNBUFFERED=1
exec "$PY" -u -m server.main
