#!/usr/bin/env bash
# Install xml_curator_for_BP_MD and its dependencies on Debian / Ubuntu.
#
#   ./install.sh                     system packages + Python environment, then a self-check
#   ./install.sh --service           ... and run it as a systemd service (starts at boot)
#
# Options:
#   --service            install, enable and start a systemd service for this folder
#   --port N             port for the service (default 5008)
#   --user NAME          user the service runs as (default: the owner of this folder)
#   --name NAME          systemd service name (default xml-curator)
#   --local-only         the service listens on 127.0.0.1 only: usable from a browser on this computer, not the network
#   --no-apt             skip the system packages (install them yourself)
#   --no-libreoffice     skip LibreOffice (RTFs are then shown as plain text)
#   --yes                do not ask for confirmation
#   --dry-run            show what would be done, change nothing
#
# Nothing is downloaded from anywhere but the distribution's package repositories (apt) and PyPI (pip). Apart from the
# bundled Chart.js (MIT), this repository contains only the app's own code (MIT); see THIRD_PARTY_NOTICES.md.
# Safe to run again: it updates the packages and the service.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=5008
SERVICE=0
APT=1
LIBREOFFICE=1
YES=0
DRY=0
SERVICE_USER="$(stat -c %U "$APP_DIR")"
SERVICE_NAME="xml-curator"
BIND="0.0.0.0"

while [ $# -gt 0 ]; do
  case "$1" in
    --service) SERVICE=1 ;;
    --port) PORT="${2:?--port needs a number}"; shift ;;
    --user) SERVICE_USER="${2:?--user needs a name}"; shift ;;
    --name) SERVICE_NAME="${2:?--name needs a name}"; shift ;;
    --local-only) BIND="127.0.0.1" ;;
    --no-apt) APT=0 ;;
    --no-libreoffice) LIBREOFFICE=0 ;;
    --yes|-y) YES=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then echo "  [dry run] $*"; else "$@"; fi; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null || { [ "$APT" = 0 ] && [ "$SERVICE" = 0 ]; } || die "run as root or install sudo"
  SUDO="sudo"
fi

case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
case "$SERVICE_NAME" in ''|*[!A-Za-z0-9_.-]*) die "--name may contain letters, digits, '.', '_' and '-' only" ;; esac
id "$SERVICE_USER" >/dev/null 2>&1 || die "user '$SERVICE_USER' does not exist"

# Run as root for another user (e.g. by the WSL provisioning script): create the Python environment as that user, so
# the app's files belong to the user the service runs as.
AS_USER=()
if [ "$(id -u)" -eq 0 ] && [ "$SERVICE_USER" != "root" ]; then
  AS_USER=(runuser -u "$SERVICE_USER" -- env HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)")
fi

# ---- checks -------------------------------------------------------------------------------------------------------
say "xml_curator_for_BP_MD installer"
echo "  App folder: $APP_DIR"
[ -f "$APP_DIR/app.py" ] && [ -f "$APP_DIR/requirements.txt" ] || die "run this script from the folder it came in (with app.py and requirements.txt)"
if [ "$APT" = 1 ]; then
  command -v apt-get >/dev/null || die "apt-get not found: this script supports Debian/Ubuntu. Install the packages listed in THIRD_PARTY_NOTICES.md yourself and run it with --no-apt."
fi

APT_PACKAGES=(python3 python3-venv python3-pip ghostscript poppler-utils tesseract-ocr tesseract-ocr-eng fonts-dejavu-core)
[ "$LIBREOFFICE" = 1 ] && APT_PACKAGES+=(libreoffice-writer)

# ---- what will be installed, and under which licences -------------------------------------------------------------
say "This will install"
if [ "$APT" = 1 ]; then
  cat <<EOF
  System packages (from your distribution's repositories, with apt):
    Ghostscript ............ AGPL-3.0       renders scanned pages (compression, OCR)
    poppler-utils .......... GPL-2.0/3.0    PDF analysis and text checks
    Tesseract OCR + English  Apache-2.0     built-in OCR
    DejaVu fonts ........... Bitstream Vera fonts for text-only PDFs
EOF
  [ "$LIBREOFFICE" = 1 ] && echo "    LibreOffice Writer ..... MPL-2.0        RTF previews"
  echo "    Python 3 + venv ........ PSF-2.0"
fi
cat <<EOF
  Python packages (from PyPI, with pip, into $APP_DIR/.venv):
    Flask, Werkzeug, Jinja2, click, itsdangerous, MarkupSafe (BSD-3-Clause), blinker (MIT), gunicorn (MIT),
    Pillow (MIT-CMU), striprtf (BSD-3-Clause), pikepdf (MPL-2.0), img2pdf (LGPL-3.0), fonttools (MIT),
    reportlab (BSD), olefile (BSD-2-Clause), lxml (BSD-3-Clause), charset-normalizer (MIT), packaging (Apache-2.0/BSD)
  Each package is installed unmodified under its own licence; the app itself is MIT. Details: THIRD_PARTY_NOTICES.md
EOF
if [ "$SERVICE" = 1 ]; then
  echo "  A systemd service '$SERVICE_NAME' on port $PORT, running as '$SERVICE_USER', started at boot,"
  if [ "$BIND" = "127.0.0.1" ]; then
    echo "  reachable from this computer only (--local-only)."
  else
    echo "  reachable from other computers that can reach this one (use --local-only to prevent that)."
  fi
fi
if [ "$YES" = 0 ] && [ "$DRY" = 0 ]; then
  printf '\nContinue? [y/N] '
  read -r answer
  case "$answer" in y|Y|yes|YES) ;; *) echo "Nothing was changed."; exit 0 ;; esac
fi

# ---- system packages ----------------------------------------------------------------------------------------------
if [ "$APT" = 1 ]; then
  say "Installing system packages"
  # A broken third-party repository makes 'apt-get update' fail even though the packages needed here come from the
  # distribution's own repositories, so carry on and let the install step decide.
  run $SUDO apt-get update -q || warn "apt-get update reported errors (often a third-party repository in your apt sources); continuing"
  run $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${APT_PACKAGES[@]}"
fi

# ---- Python environment -------------------------------------------------------------------------------------------
say "Setting up the Python environment"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11 or newer is needed (found $("$PY" -V 2>&1))"
ok "$("$PY" -V)"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  run "${AS_USER[@]}" "$PY" -m venv "$APP_DIR/.venv"
fi
run "${AS_USER[@]}" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
run "${AS_USER[@]}" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "Python packages installed in .venv"

# ---- self-check ---------------------------------------------------------------------------------------------------
say "Checking the installation"
if [ "$DRY" = 0 ]; then
  (cd "$APP_DIR" && "${AS_USER[@]}" .venv/bin/python - <<'PY'
import importlib, shutil
for m in ('flask', 'gunicorn', 'PIL', 'striprtf', 'pikepdf', 'img2pdf', 'fontTools', 'reportlab', 'olefile'):
    importlib.import_module(m)
print('  \033[32m✓\033[0m Python modules import')
from curator import ocr
missing = [t for t in ('gs', 'pdftotext', 'pdfinfo', 'pdfimages', 'pdftoppm') if not shutil.which(t)]
print('  \033[33m!\033[0m missing PDF tools: ' + ', '.join(missing) if missing else '  \033[32m✓\033[0m Ghostscript and poppler found')
print('  \033[32m✓\033[0m LibreOffice found (RTF previews)' if shutil.which('soffice') else '  \033[33m!\033[0m LibreOffice not found: RTFs are shown as plain text')
try:
    print('  \033[32m✓\033[0m OCR engine: ' + ocr.choose_engine()[1])
except ocr.OcrError as e:
    print('  \033[33m!\033[0m ' + str(e))
PY
  )
else
  echo "  [dry run] import checks skipped"
fi

# ---- service ------------------------------------------------------------------------------------------------------
if [ "$SERVICE" = 1 ]; then
  say "Setting up the '$SERVICE_NAME' service"
  command -v systemctl >/dev/null || die "systemctl not found: start it with  .venv/bin/gunicorn --workers 3 --threads 3 --timeout 300 --bind 0.0.0.0:$PORT app:app"
  UNIT="/etc/systemd/system/$SERVICE_NAME.service"
  UNIT_TEXT="[Unit]
Description=Gunicorn instance to serve xml_curator_for_BP_MD Flask App
After=network.target

[Service]
User=$SERVICE_USER
Group=$(id -gn "$SERVICE_USER")
WorkingDirectory=$APP_DIR
# The app runs Ghostscript, poppler-utils, Tesseract and LibreOffice, so the system bin directories must be on PATH.
Environment=\"PATH=$APP_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin\"
# Optional settings (see README): CURATOR_OCR, CURATOR_LITEPARSE_URL, CURATOR_DATA, CURATOR_MAX_UPLOAD_MB
ExecStart=$APP_DIR/.venv/bin/gunicorn --workers 3 --threads 3 --timeout 300 --bind $BIND:$PORT app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target"
  if [ "$DRY" = 1 ]; then
    echo "  [dry run] would write $UNIT:"
    echo "$UNIT_TEXT" | sed 's/^/    /'
  else
    if [ -f "$UNIT" ] && ! diff -q <(echo "$UNIT_TEXT") "$UNIT" >/dev/null 2>&1; then
      $SUDO cp "$UNIT" "$UNIT.bak-$(date +%Y%m%d-%H%M%S)"
      warn "an existing $UNIT was different; the old one was kept as $UNIT.bak-…"
    fi
    echo "$UNIT_TEXT" | $SUDO tee "$UNIT" >/dev/null
    # the service user must be able to write the data folder
    mkdir -p "$APP_DIR/data" 2>/dev/null || $SUDO mkdir -p "$APP_DIR/data"
    [ "$(stat -c %U "$APP_DIR/data")" = "$SERVICE_USER" ] || $SUDO chown -R "$SERVICE_USER": "$APP_DIR/data"
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
    $SUDO systemctl restart "$SERVICE_NAME"
    sleep 2
    if systemctl is-active --quiet "$SERVICE_NAME"; then
      ok "service running on port $PORT ($([ "$BIND" = 127.0.0.1 ] && echo 'this computer only' || echo 'all network interfaces'))"
    else
      die "the service did not start; see: journalctl -u $SERVICE_NAME -n 50"
    fi
  fi
fi

# ---- done ---------------------------------------------------------------------------------------------------------
say "Done"
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ "$SERVICE" = 1 ] && [ "$BIND" = "127.0.0.1" ]; then
  echo "  Open http://localhost:$PORT/ in a browser on this computer."
  echo "  Manage it with: systemctl status|restart|stop $SERVICE_NAME   ·   logs: journalctl -u $SERVICE_NAME -f"
elif [ "$SERVICE" = 1 ]; then
  echo "  Open http://${HOST_IP:-localhost}:$PORT/ in a browser."
  echo "  Manage it with: systemctl status|restart|stop $SERVICE_NAME   ·   logs: journalctl -u $SERVICE_NAME -f"
else
  echo "  Start it with:  cd \"$APP_DIR\" && .venv/bin/python app.py      (then open http://${HOST_IP:-localhost}:5008/)"
  echo "  Or run ./install.sh --service to run it as a service that starts at boot."
fi
