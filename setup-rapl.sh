#!/usr/bin/env bash
# OPTIONAL (run with sudo): make CPU RAPL energy counters readable so CPU watts are
# measured by the hardware instead of estimated from utilisation.
# Trade-off: the kernel restricts these to root because fine-grained energy readings
# can leak information to local users (PLATYPUS side channel). Only do this on a
# single-user machine. Undo: sudo rm /etc/tmpfiles.d/evergy-rapl.conf and reboot.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
CONF=/etc/tmpfiles.d/evergy-rapl.conf
: > "$CONF"
for f in /sys/class/powercap/intel-rapl:*/energy_uj; do
  echo "z $f 0444 root root -" >> "$CONF"
done
systemd-tmpfiles --create "$CONF"
echo "RAPL readable. Now: systemctl --user restart evergy"
