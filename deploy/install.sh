#!/usr/bin/env bash
# Install (or update) the always-on Mr Tofu stock watcher on a Linux box
# (Raspberry Pi OS, Ubuntu, Debian). Run with:
#   curl -fsSL https://raw.githubusercontent.com/NZDurriez/tofu-stock-alerts/main/deploy/install.sh | sudo bash
# Re-running it later updates to the latest version and keeps your settings.
set -euo pipefail

REPO="https://github.com/NZDurriez/tofu-stock-alerts.git"
APP_DIR="/opt/tofu-stock-alerts"
DATA_DIR="/var/lib/tofu-stock-alerts"
ENV_FILE="/etc/tofu-stock-alerts.env"
SERVICE="tofu-stock-alerts"
USER_NAME="tofuwatch"

if [ "$(id -u)" -ne 0 ]; then echo "Please run with sudo." >&2; exit 1; fi

echo "==> Installing python3, curl and git"
if command -v apt-get >/dev/null; then
  apt-get update -qq && apt-get install -y -qq python3 curl git >/dev/null
elif command -v dnf >/dev/null; then
  dnf install -y -q python3 curl git
fi

echo "==> Creating service user and folders"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$DATA_DIR"
chown "$USER_NAME:$USER_NAME" "$DATA_DIR"

echo "==> Getting the latest watcher code"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q origin main && git -C "$APP_DIR" reset -q --hard origin/main
else
  git clone -q "$REPO" "$APP_DIR"
fi
# Start from GitHub's latest snapshot so the first run doesn't re-announce old stock
[ -f "$DATA_DIR/state.json" ] || cp "$APP_DIR/state.json" "$DATA_DIR/state.json"
chown "$USER_NAME:$USER_NAME" "$DATA_DIR/state.json"

if [ ! -f "$ENV_FILE" ]; then
  echo
  echo "==> First-time setup"
  # Read from the terminal even when this script is piped from curl
  read -rsp "Paste your Discord webhook URL (hidden), then press Enter: " HOOK </dev/tty; echo
  read -rp  "Your Discord user ID to ping (blank for none): " UID_PING </dev/tty
  read -rp  "Seconds between checks [20]: " INTERVAL </dev/tty
  HOOK="$(echo "$HOOK" | tr -d '[:space:]')"
  case "$HOOK" in https://discord.com/api/webhooks/*|https://discordapp.com/api/webhooks/*) ;;
    *) echo "That doesn't look like a Discord webhook URL; run the installer again." >&2; exit 1;;
  esac
  umask 077
  cat > "$ENV_FILE" <<EOF
# Settings for the Mr Tofu stock watcher. Edit, then: sudo systemctl restart $SERVICE
DISCORD_WEBHOOK_URL=$HOOK
DISCORD_PING=${UID_PING:+<@$UID_PING>}
WATCH_KEYWORDS=delta reign, booster bundle, elite trainer box, etb, booster box, booster display, display, booster case, booster pack, booster packs, sleeved booster, enhanced booster, premium booster, half booster box, blister, collection box, premium collection, ultra premium collection, special collection, poster collection, binder collection, surprise box, gift box, tin, mini tin, build & battle, build and battle, battle deck, starter deck, starter set, double pack, bundle, pre-order, preorder, pre order, pre-release, prerelease, collection, figure collection, commander deck, opus, op, eb, prb, vault
WATCH_INTERVAL=${INTERVAL:-20}
WATCH_MINUTES=-1
STATE_FILE=$DATA_DIR/state.json
SEND_STARTUP=true
EOF
  chmod 600 "$ENV_FILE"
else
  echo "==> Keeping your existing settings in $ENV_FILE"
fi

echo "==> Installing the background service"
cp "$APP_DIR/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"
sleep 5
systemctl --no-pager --lines=5 status "$SERVICE" || true

echo
echo "Done! The watcher is running and will start automatically on boot."
echo "  Live log:      journalctl -u $SERVICE -f"
echo "  Stop / start:  sudo systemctl stop $SERVICE   /   sudo systemctl start $SERVICE"
echo "  Settings:      sudo nano $ENV_FILE   (then: sudo systemctl restart $SERVICE)"
echo "  Update later:  run this installer again"
