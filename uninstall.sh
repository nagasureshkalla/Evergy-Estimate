#!/usr/bin/env bash
# Removes services and program files. Pass --purge to also delete config and history.
set -u
systemctl --user disable --now evergy-tray.service evergy.service evergy-rates.timer evergy-rates.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/evergy.service" "$HOME/.config/systemd/user/evergy-tray.service" \
      "$HOME/.config/systemd/user/evergy-rates.service" "$HOME/.config/systemd/user/evergy-rates.timer" \
      "$HOME/.config/autostart/evergy-tray.desktop" "$HOME/.local/bin/evergy"
rm -rf "$HOME/.local/share/evergy-estimate"
systemctl --user daemon-reload
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/.config/evergy" "$HOME/.local/state/evergy"
fi
echo "Uninstalled."
