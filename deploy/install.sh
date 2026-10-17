#!/usr/bin/env bash
# Install — or update — NTFAL on a Debian/Ubuntu VPS as one systemd service
# (deploy/ntfal-bot.service: every horizon kept fresh, the dashboard on
# 127.0.0.1:8000, the Telegram report and, only if config.toml enables it, the
# trade bot). Run it as root from the clone, e.g. /opt/ntfal:
#
#   sudo /opt/ntfal/deploy/install.sh             install, or re-install after an edit
#   sudo /opt/ntfal/deploy/install.sh --update    git pull first, then the same steps
#
# Options:
#   --update     pull the latest code (fast-forward only) before installing
#   --dev        also install the test tools (pytest, ruff, black)
#   --no-start   install and enable the service, but do not (re)start it
#   --harden     firewall allowing SSH only (ufw), fail2ban, automatic security updates
#
# Idempotent: re-running it upgrades the dependencies and restarts the service.
# It never overwrites .env or config.toml. The code stays owned by root (the
# service cannot modify it); the service user owns only cache/ and .env.
# See docs/DEPLOIEMENT.md.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="ntfal"
SERVICE_NAME="ntfal-bot"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
VENV_PY="$APP_DIR/.venv/bin/python"

UPDATE=0
DEV=0
START=1
HARDEN=0
for arg in "$@"; do
  case "$arg" in
    --update) UPDATE=1 ;;
    --dev) DEV=1 ;;
    --no-start) START=0 ;;
    --harden) HARDEN=1 ;;
    -h | --help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $arg (see --help)" >&2
      exit 2
      ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die "run as root: sudo $0 $*"
command -v apt-get >/dev/null || die "this script targets Debian/Ubuntu (no apt-get)"
[[ -f "$APP_DIR/bot.py" && -f "$APP_DIR/pyproject.toml" ]] || die "$APP_DIR is not an NTFAL clone"
case "$APP_DIR" in
  /root/* | /home/*)
    die "the service cannot read $APP_DIR (ProtectHome hides home directories): move the clone to /opt/ntfal"
    ;;
esac

say "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv ca-certificates >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
  die "Python 3.11 or newer is required, found $(python3 --version 2>&1)"

if [[ $UPDATE -eq 1 ]]; then
  say "Latest code (git pull --ff-only)"
  # As root, with root's git credentials (deploy key or token) for a private repo.
  # --autostash keeps your edits to tracked files (config.toml) across the pull.
  git -c safe.directory="$APP_DIR" -C "$APP_DIR" pull --ff-only --autostash
fi

say "Service user '$SERVICE_USER'"
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "exists"
else
  useradd --system --user-group --home-dir "$APP_DIR" --no-create-home \
    --shell /usr/sbin/nologin "$SERVICE_USER"
  echo "created"
fi

say "Python environment ($APP_DIR/.venv)"
[[ -x "$VENV_PY" ]] || python3 -m venv "$APP_DIR/.venv"
"$VENV_PY" -m pip install --quiet --upgrade pip
# The project is not a package: its dependencies are read from pyproject.toml —
# the scanner's, the "bot" extra's (order signing, testnet/mainnet only) and,
# with --dev, the test tools.
REQUIREMENTS="$(mktemp)"
trap 'rm -f "$REQUIREMENTS"' EXIT
"$VENV_PY" - "$APP_DIR/pyproject.toml" "$DEV" >"$REQUIREMENTS" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as f:
    project = tomllib.load(f)
deps = project["project"]["dependencies"] + project["project"]["optional-dependencies"]["bot"]
if sys.argv[2] == "1":
    deps += project["dependency-groups"]["dev"]
print("\n".join(deps))
PY
"$VENV_PY" -m pip install --quiet --upgrade -r "$REQUIREMENTS"

say "Files and permissions"
if [[ ! -f "$APP_DIR/.env" ]]; then
  install -m 600 "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "created .env from .env.example"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$APP_DIR/cache"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/cache"
# Everything else: root's, readable by the service, writable by nobody else.
find "$APP_DIR" \( -path "$APP_DIR/.env" -o -path "$APP_DIR/cache" \) -prune \
  -o -exec chown root:root {} + -exec chmod go+rX,go-w {} +
echo "code: root (read-only to the service) · cache/ and .env: $SERVICE_USER · .env: 600"

say "Checking the install (config.toml, .env, imports) as '$SERVICE_USER'"
# What bot.py needs at startup, so a gap fails here instead of in a restart loop.
# It names a missing secret, never prints one.
MODE="$(cd "$APP_DIR" && runuser -u "$SERVICE_USER" -- "$VENV_PY" -c '
import os, sys
import app.main, bot
from config import load_config
cfg = load_config()
e, t = cfg.execution, cfg.telegram
missing = []
if e.enabled and e.mode != "dry_run":
    missing += [] if os.environ.get("HL_API_PRIVATE_KEY", "").strip() else ["HL_API_PRIVATE_KEY"]
    missing += [] if cfg.positions.address else ["HL_ADDRESS"]
if t.enabled:
    missing += [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(k, "").strip()]
if missing:
    sys.exit("missing in .env: " + ", ".join(missing))
print(f"execution {e.mode.upper()}" if e.enabled else "analysis only (execution disabled)")
')" || die "the check failed: fix the error above, then re-run this script"
echo "ok — $MODE"

say "systemd service '$SERVICE_NAME'"
sed -e "s|/opt/ntfal|$APP_DIR|g" \
  -e "s|^User=.*|User=$SERVICE_USER|" \
  -e "s|^Group=.*|Group=$SERVICE_USER|" \
  "$APP_DIR/deploy/ntfal-bot.service" >"$UNIT_PATH"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
if [[ $START -eq 1 ]]; then
  systemctl restart "$SERVICE_NAME"
  echo "restarted — $MODE"
else
  echo "enabled, not started (--no-start): systemctl start $SERVICE_NAME"
fi

if [[ $HARDEN -eq 1 ]]; then
  say "Hardening: ufw (SSH only), fail2ban, unattended-upgrades"
  apt-get install -y -qq ufw fail2ban unattended-upgrades >/dev/null
  ufw allow OpenSSH >/dev/null
  ufw --force enable >/dev/null
  systemctl enable --now fail2ban >/dev/null 2>&1
  echo "firewall on: only SSH is reachable (the dashboard stays on 127.0.0.1)"
fi

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

Done. Next:
  - secrets:   nano $APP_DIR/.env          (HL_ADDRESS, then TELEGRAM_*, then HL_API_PRIVATE_KEY)
  - settings:  nano $APP_DIR/config.toml   ([telegram], [execution]: start with mode = "dry_run")
  - apply:     systemctl restart $SERVICE_NAME
  - log:       journalctl -u $SERVICE_NAME -f
  - dashboard: from your computer, ssh -L 8000:127.0.0.1:8000 root@${HOST_IP:-<server-ip>}
               then open http://127.0.0.1:8000
  - update:    sudo $APP_DIR/deploy/install.sh --update
EOF
