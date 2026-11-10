#!/usr/bin/env bash
# Installs Evergy Estimate for the current user: 24/7 systemd daemon + top-bar indicator + `evergy` CLI.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/evergy-estimate"
BIN="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
AUTOSTART="$HOME/.config/autostart"
PY=/usr/bin/python3

$PY -c "import psutil" 2>/dev/null || { echo "Missing psutil: sudo apt install python3-psutil"; exit 1; }
$PY -c "import gi; gi.require_version('AyatanaAppIndicator3','0.1')" 2>/dev/null \
  || echo "warning: tray needs: sudo apt install gir1.2-ayatanaappindicator3-0.1 (daemon/TUI still work)"

mkdir -p "$DEST" "$BIN" "$UNIT_DIR" "$AUTOSTART"
install -m 755 "$SRC/evergy.py" "$SRC/evergy_tray.py" "$DEST/"
install -m 644 "$SRC/evergy-icon.svg" "$DEST/"
ln -sf "$DEST/evergy.py" "$BIN/evergy"

cat > "$UNIT_DIR/evergy.service" <<UNIT
[Unit]
Description=Evergy Estimate - system power & energy cost sampler

[Service]
ExecStart=$PY $DEST/evergy.py daemon
Restart=always
RestartSec=5
Nice=10

[Install]
WantedBy=default.target
UNIT

cat > "$UNIT_DIR/evergy-tray.service" <<UNIT
[Unit]
Description=Evergy Estimate top-bar indicator
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=$PY $DEST/evergy_tray.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
UNIT

# Daily electricity price check: 6 AM, or at the next boot/resume if 6 AM was missed.
# The job itself waits for internet and retries on failure.
cat > "$UNIT_DIR/evergy-rates.service" <<UNIT
[Unit]
Description=Evergy Estimate - daily electricity price check

[Service]
Type=oneshot
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$PY $DEST/evergy.py update-rates
TimeoutStartSec=infinity
Restart=on-failure
RestartSec=30min
Nice=10
UNIT

cat > "$UNIT_DIR/evergy-rates.timer" <<UNIT
[Unit]
Description=Evergy Estimate - check electricity prices daily at 6 AM

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true
RandomizedDelaySec=2min

[Install]
WantedBy=timers.target
UNIT

# Older autostart entry (if any) would start a second copy.
rm -f "$AUTOSTART/evergy-tray.desktop"

systemctl --user daemon-reload
systemctl --user enable --now evergy.service evergy-rates.timer
systemctl --user restart evergy.service
systemctl --user enable evergy-tray.service
if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true
  systemctl --user restart evergy-tray.service
fi

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "Tip: run 'sudo loginctl enable-linger $USER' so the daemon also runs while you are logged out."
fi
echo "Installed. Commands: evergy (dashboard) | evergy status | evergy report | evergy config"
