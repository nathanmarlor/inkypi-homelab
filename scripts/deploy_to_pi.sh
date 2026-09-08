#!/usr/bin/env bash
# Deploy this working tree to the InkyPi Raspberry Pi and (re)install the service.
#
#   scripts/deploy_to_pi.sh user@host
#
# What it does:
#   1. rsyncs the repo (minus venv, dev output, secrets) to ~/InkyPi on the Pi
#   2. first time: runs install/install.sh (system deps, venv, systemd unit); later: install/update.sh
#   3. copies .env (API tokens) and builds src/config/device.json from the dev playlist
#   4. restarts the inkypi service and prints its status
set -euo pipefail

TARGET="${1:?usage: scripts/deploy_to_pi.sh user@host}"
REMOTE_USER="${TARGET%%@*}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_DIR="InkyPi"

echo "==> syncing $HERE -> $TARGET:~/$REMOTE_DIR"
rsync -az --delete \
  --exclude venv/ --exclude mock_display_output/ --exclude '__pycache__/' \
  --exclude '.env' --exclude 'src/config/device.json' --exclude 'src/config/usage.json' --exclude 'src/static/images/*' \
  "$HERE/" "$TARGET:$REMOTE_DIR/"

if [ "${DEPLOY_QUICK:-0}" = "1" ]; then
  echo "==> quick mode: code sync only, skipping installer/updater"
elif ssh "$TARGET" 'test -f /etc/systemd/system/inkypi.service'; then
  echo "==> existing install found, running update.sh"
  ssh "$TARGET" "cd $REMOTE_DIR && sudo bash install/update.sh"
else
  echo "==> fresh install, running install.sh (answering 'n' to the reboot prompt)"
  ssh "$TARGET" "cd $REMOTE_DIR && echo n | sudo bash install/install.sh"
fi

echo "==> secrets and device config"
# the installer creates config files as root; make them writable by the deploy user
ssh "$TARGET" "sudo chown -R \$(id -un):\$(id -gn) $REMOTE_DIR/src/config 2>/dev/null; mkdir -p $REMOTE_DIR/src/static/images/plugins"
scp -q "$HERE/.env" "$TARGET:$REMOTE_DIR/.env"
python3 - "$HERE/src/config/device_dev.json" "$REMOTE_USER" > /tmp/inkypi-device.json <<'EOF'
import json, sys
dev = json.load(open(sys.argv[1]))
REMOTE_USER = sys.argv[2]
# Start from the installer's base config, keep the playlist and display settings from dev.
cfg = {
    "name": "InkyPi",
    "orientation": dev.get("orientation", "horizontal"),
    "inverted_image": False,
    "scheduler_sleep_time": 60,
    "startup": False,  # no splash on restart: it blocks the web port ~35 s and flashes the panel
    "timezone": dev.get("timezone", "Europe/London"),
    "time_format": dev.get("time_format", "24h"),
    "plugin_cycle_interval_seconds": dev.get("plugin_cycle_interval_seconds", 600),
    "image_settings": dev.get("image_settings", {}),
    "plugin_order": dev.get("plugin_order", []),
    "playlist_config": dev.get("playlist_config", {}),
    "buttons": dev.get("buttons", {}),
    "refresh_info": {"refresh_time": None, "image_hash": None, "refresh_type": None, "plugin_id": None},
}
# no display_type: the Inky driver auto-detects the panel and writes the resolution on first run
for p in cfg["playlist_config"].get("playlists", []):
    p["current_plugin_index"] = None
    for inst in p.get("plugins", []):
        # the Pi reads the usage snapshot pushed to it by the exporter, not the exporter's HTTP port
        if inst.get("plugin_id") == "ai_usage":
            inst.setdefault("plugin_settings", {})["usageUrl"] = f"/home/{REMOTE_USER}/InkyPi/src/config/usage.json"
json.dump(cfg, sys.stdout, indent=4)
EOF
scp -q /tmp/inkypi-device.json "$TARGET:$REMOTE_DIR/src/config/device.json"

echo "==> restarting service"
ssh "$TARGET" 'sudo systemctl restart inkypi.service; sleep 5; sudo systemctl --no-pager --lines=15 status inkypi.service; echo; hostname -I'
