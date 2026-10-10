#!/usr/bin/env bash
# Deploy xml_curator_for_BP_MD on an existing Ubuntu system (bare metal, VM, cloud, or a WSL2 Ubuntu you already have).
#
# Run as your normal (sudo-capable) user, from anywhere:
#     chmod +x setup_xml_curator.sh && ./setup_xml_curator.sh
#
# Does:
#   0. Checks TCP 5008 is free (offers the next free port if not) and asks who may reach the app
#   1. Installs git and curl if missing (sudo)
#   2. Clones (or fast-forward updates) https://github.com/auroscope/xml_curator_for_BP_MD -> $HOME/xml_curator_for_BP_MD
#   3. Runs the repository's install.sh: system packages (Ghostscript, poppler-utils, Tesseract, DejaVu fonts,
#      LibreOffice Writer), .venv with requirements.txt, and the 'xml-curator' systemd service (gunicorn, starts at boot)
#      running as you
#   4. Opens the port in ufw, for your local network only or from anywhere (you choose)
#   5. Checks the app responds
#
# Non-interactive overrides (export before running):
#   PORT=5008  APP_DIR=$HOME/xml_curator_for_BP_MD  BRANCH=main  SERVICE_NAME=xml-curator
#   FIREWALL_SOURCE=lan|any|none|<CIDR such as 192.168.1.0/24>   LIBREOFFICE=yes|no
# It does NOT harden the operating system or SSH (see create_wsl2_ubuntu_sandbox.ps1 for a hardened WSL2 instance).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/auroscope/xml_curator_for_BP_MD.git}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-5008}"
APP_DIR="${APP_DIR:-$HOME/xml_curator_for_BP_MD}"
SERVICE_NAME="${SERVICE_NAME:-xml-curator}"
APP_NAME="xml_curator_for_BP_MD"

say()  { printf '\n\033[36m=== %s ===\033[0m\n' "$*"; }
ok()   { printf '\033[32m[+] %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

if [[ $EUID -eq 0 && "${ALLOW_ROOT:-}" != 1 ]]; then
  die "Run as your normal user (not root/sudo). The app is installed in your home directory and runs as you; sudo is used only where needed."
fi
SUDO=""; [[ $EUID -eq 0 ]] || SUDO="sudo"
if [[ -n "$SUDO" ]]; then
  command -v sudo >/dev/null || die "sudo is required."
  sudo -v || die "sudo authentication failed."
fi
RUN_USER="$(id -un)"

# =========================================================
say "0. Checking TCP port $PORT"
port_listeners() { $SUDO ss -Hltnp "sport = :$1" 2>/dev/null; }   # with sudo, other users' processes are named too
port_in_use()    { [[ -n "$(port_listeners "$1")" ]]; }
port_is_ours() {   # the listener belongs to an earlier install of this service
  local pid
  pid=$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null || true)
  [[ -n "$pid" && "$pid" != "0" ]] || return 1
  port_listeners "$1" | grep -Eq "pid=${pid}[,)]" && return 0
  # gunicorn workers are children of the main process
  local kids; kids=$(pgrep -P "$pid" 2>/dev/null | paste -sd'|' -)
  [[ -n "$kids" ]] && port_listeners "$1" | grep -Eq "pid=(${kids})[,)]"
}
next_free_port() {
  local p
  for ((p = $1 + 1; p <= $1 + 100 && p <= 65535; p++)); do
    port_in_use "$p" || { echo "$p"; return; }
  done
}
while true; do
  if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
    warn "Port '$PORT' is not usable. Choose 1024-65535 (ports below 1024 need root)."
  elif ! port_in_use "$PORT"; then
    ok "Port $PORT is free."; break
  elif port_is_ours "$PORT"; then
    ok "Port $PORT is held by an earlier install of $APP_NAME; it will be restarted."; break
  else
    warn "Port $PORT is already in use by another process:"
    port_listeners "$PORT" | sed 's/^/      /' || true
  fi
  base="$PORT"; [[ "$base" =~ ^[0-9]+$ ]] || base=5008
  suggestion=$(next_free_port "$base" 2>/dev/null || true)
  reply=""
  read -r -p "Enter another port, or q to quit [${suggestion:-none}]: " reply || die "No interactive input available; set PORT=<free port> and re-run."
  reply="${reply:-$suggestion}"
  [[ "$reply" == "q" || "$reply" == "Q" || -z "$reply" ]] && die "Aborted; nothing was changed."
  PORT="$reply"
done

# =========================================================
say "0. Who may reach the app on TCP $PORT"
detect_lan_cidr() {
  local dev
  dev=$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
  [[ -n "$dev" ]] && ip -4 route show dev "$dev" scope link 2>/dev/null | awk '{print $1; exit}'
}
valid_cidr() {
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]|[12][0-9]|3[0-2])$ ]] || return 1
  (( 10#${BASH_REMATCH[1]} <= 255 && 10#${BASH_REMATCH[2]} <= 255 && 10#${BASH_REMATCH[3]} <= 255 && 10#${BASH_REMATCH[4]} <= 255 ))
}
LAN_CIDR=$(detect_lan_cidr || true)
FW_SOURCE="${FIREWALL_SOURCE:-}"
if [[ -z "$FW_SOURCE" ]]; then
  echo "  1) Local network only${LAN_CIDR:+ ($LAN_CIDR detected)}   [recommended]"
  echo "  2) From anywhere"
  echo "  3) Do not change the firewall"
  choice=""; read -r -p "Choose 1, 2 or 3 [1]: " choice || true
  case "${choice:-1}" in
    2) FW_SOURCE="any" ;;
    3) FW_SOURCE="none" ;;
    *) FW_SOURCE="lan" ;;
  esac
fi
if [[ "$FW_SOURCE" == "lan" ]]; then
  FW_SOURCE="$LAN_CIDR"
  if [[ -z "${FIREWALL_SOURCE:-}" ]]; then
    reply=""; read -r -p "Local network CIDR [${FW_SOURCE:-e.g. 192.168.1.0/24}]: " reply || true
    FW_SOURCE="${reply:-$FW_SOURCE}"
  fi
fi
if [[ "$FW_SOURCE" != "any" && "$FW_SOURCE" != "none" ]] && ! valid_cidr "$FW_SOURCE"; then
  die "Invalid network '$FW_SOURCE'. Use a CIDR like 192.168.1.0/24, 'any' or 'none'."
fi
ok "Port $PORT: $([[ "$FW_SOURCE" == none ]] && echo 'firewall left unchanged' || echo "allowed from $FW_SOURCE")"

LO_FLAG=()
case "${LIBREOFFICE:-}" in
  no|NO|n|N) LO_FLAG=(--no-libreoffice) ;;
  yes|YES|y|Y) ;;
  *)
    reply=""; read -r -p "Install LibreOffice Writer for RTF previews (a few hundred MB)? (Y/n) " reply || true
    [[ "$reply" =~ ^[nN] ]] && LO_FLAG=(--no-libreoffice) ;;
esac

# =========================================================
say "1. Prerequisites"
need=()
command -v git  >/dev/null || need+=(git)
command -v curl >/dev/null || need+=(curl)
if ((${#need[@]})); then
  $SUDO apt-get update -q || warn "apt-get update reported errors (often a third-party repository); continuing"
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${need[@]}"
fi
ok "git $(git --version | awk '{print $3}')"

# =========================================================
say "2. Fetching $APP_NAME into $APP_DIR"
mkdir -p "$(dirname "$APP_DIR")"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
  ok "Existing clone updated."
elif [[ -e "$APP_DIR" ]]; then
  die "$APP_DIR exists but is not a git clone. Move or remove it and re-run."
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  ok "Cloned."
fi
[[ -x "$APP_DIR/install.sh" ]] || die "install.sh not found in $APP_DIR."

# =========================================================
say "3. Installing dependencies and the '$SERVICE_NAME' service (install.sh)"
if port_in_use "$PORT" && ! port_is_ours "$PORT"; then
  die "TCP $PORT was taken by another process during setup. Re-run to pick a different port."
fi
$SUDO "$APP_DIR/install.sh" --yes --service --port "$PORT" --user "$RUN_USER" --name "$SERVICE_NAME" "${LO_FLAG[@]}"

# =========================================================
say "4. Firewall (ufw)"
if [[ "$FW_SOURCE" == "none" ]]; then
  ok "Left unchanged, as chosen."
elif command -v ufw >/dev/null; then
  # Re-runs: remove this script's earlier rules (tagged with the app name) so the scope is replaced, not stacked.
  mapfile -t old_rules < <($SUDO ufw status numbered | sed -n "s/^\[ *\([0-9]\+\)\].*# $APP_NAME\$/\1/p" | sort -rn)
  for n in "${old_rules[@]:-}"; do
    if [[ -n "$n" ]]; then $SUDO ufw --force delete "$n" >/dev/null; fi
  done
  if [[ "$FW_SOURCE" == "any" ]]; then
    $SUDO ufw allow "$PORT/tcp" comment "$APP_NAME"
  else
    $SUDO ufw allow from "$FW_SOURCE" to any port "$PORT" proto tcp comment "$APP_NAME"
  fi
  if $SUDO ufw status | grep -q '^Status: active'; then
    ok "Rule added; ufw is active."
  else
    warn "Rule added, but ufw is not active so nothing is being filtered. Enable it with 'sudo ufw enable' (allow SSH first)."
  fi
else
  warn "ufw is not installed; no firewall rule was added. Install it with 'sudo apt-get install ufw', or limit access another way."
fi

# =========================================================
say "5. Verification"
code="000"
for _ in {1..20}; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)
  [[ "$code" != "000" ]] && break
  sleep 1
done
systemctl --no-pager --lines=0 status "$SERVICE_NAME" 2>/dev/null | sed -n '1,3p' || true
[[ "$code" != "000" ]] || die "The app did not respond on port $PORT. Check: journalctl -u $SERVICE_NAME -n 50"
ok "App responded on http://127.0.0.1:$PORT/ (HTTP $code)."

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
printf '\n\033[32m=========================================================\n'
echo " $APP_NAME is running"
echo " Location   : $APP_DIR   (uploads and working copies: $APP_DIR/data)"
echo " URL        : http://${HOST_IP:-<host-ip>}:$PORT/"
echo " Service    : sudo systemctl status|restart|stop $SERVICE_NAME   (runs as $RUN_USER, starts at boot)"
echo " Logs       : journalctl -u $SERVICE_NAME -f"
echo " Update     : re-run this script (pulls the latest version and restarts the service)"
echo " Firewall   : $([[ "$FW_SOURCE" == none ]] && echo 'unchanged' || echo "TCP $PORT allowed from $FW_SOURCE (ufw)")"
printf '=========================================================\033[0m\n'
if [[ "$FW_SOURCE" == "any" ]]; then
  warn "Port $PORT is open to any address that can reach this host, and the app handles patient data."
  warn "To limit it later, re-run this script and choose 'Local network only'."
fi
