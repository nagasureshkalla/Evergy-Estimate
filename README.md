# Evergy Estimate

A monitor that runs all the time and shows how much power your PC uses and what it costs.
It shows this in the GNOME top bar, in a live terminal dashboard, and in daily reports.

| Part | What it does |
|---|---|
| `evergy.service` (systemd user, always on) | Samples every 5 s, adds up energy (Wh) and cost, and logs per-minute history to `~/.local/state/evergy/energy.db` |
| `evergy-tray.service` | Top-bar indicator `⚡ 125W $0.42/d` with a dropdown breakdown (CPU/GPU/RAM/disk, today/week/month/all-time, projected month) |
| `evergy` | Live terminal dashboard (`q` quits, `h` switches history between 1h, 24h and 7d) |
| `evergy status` / `evergy report --days 30` | One-shot summary / daily kWh and cost table |
| `evergy-rates.timer` | Daily electricity price check at 6 AM (or at the next boot/resume if 6 AM was missed) |
| `evergy rates [--accept\|--reset]` | Show current prices and the last price check / apply held prices / restore the built-in prices |

## Install / uninstall
    ./install.sh            # copies to ~/.local/share/evergy-estimate, enables both services
    ./uninstall.sh [--purge]

## How power is calculated
- **GPU**: real reading from `nvidia-smi power.draw` (AMD and Intel use hwmon `power1_average`)
- **CPU**: hardware RAPL counters if readable (`sudo ./setup-rapl.sh`), otherwise `idle + (max-idle) × utilisation`
- **Memory**: W/GB at idle + W/GB scaled by RAM in use
- **Disks**: per-drive idle + active watts × busy % (NVMe, SSD and HDD have different profiles)
- **Base**: motherboard, fans and chipset (fixed) + `extra_watts` (for example a monitor)
- **Wall** = sum of components ÷ PSU efficiency

## Electricity prices (Evergy, Overland Park KS)
Prices are in `~/.config/evergy/tariff.json`: tiered by monthly kWh, with separate summer (Jun 1 – Sep 30) and non-summer rates, plus a 4–8 pm peak charge.
Each sample is charged at the *marginal* price at that moment. Set `household_other_kwh_per_month` if you want the PC's usage stacked on top of the rest of the house's usage when picking its tier.

The daily check (`evergy update-rates`) runs `claude -p` with **web search and fetch only** (no local tools). It returns a JSON result, and that result is validated:
- Not found, no sources, or out-of-range numbers → the result is rejected and current prices are kept
- Change ≤ `max_auto_change_pct` (25%) → applied, with a desktop notification
- Bigger change → held for review: run `evergy rates`, then `evergy rates --accept`

Every check is logged to `~/.local/state/evergy/tariff_checks.jsonl`. With `[tariff] enabled = false` it uses a flat `rate_per_kwh` instead. Edit `~/.config/evergy/config.toml`, then run `systemctl --user restart evergy evergy-tray`.
These are estimates. To calibrate, compare against a plug-in power meter (Kill-A-Watt) and adjust `base_watts` / `psu_efficiency`.
