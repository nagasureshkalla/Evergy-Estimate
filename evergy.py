#!/usr/bin/python3
"""Evergy Estimate - 24/7 system power & electricity cost monitor.

Subcommands:
  daemon   sample the system forever, integrate energy, log to SQLite
  tui      live terminal dashboard
  status   one-shot summary
  report   daily energy/cost table
  tray     top-bar indicator (see evergy_tray.py)
  config   print config path (and create it if missing)
"""
import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta

import psutil

APP = "evergy"
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), APP)
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), APP)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.toml")
DB_PATH = os.path.join(STATE_DIR, "energy.db")
LIVE_PATH = os.path.join(STATE_DIR, "live.json")
TARIFF_PATH = os.path.join(CONFIG_DIR, "tariff.json")
TARIFF_PENDING = os.path.join(STATE_DIR, "tariff_pending.json")
TARIFF_LOG = os.path.join(STATE_DIR, "tariff_checks.jsonl")

# Evergy residential prices for Overland Park, KS as provided by the user on 2026-10-22.
# The daily check (evergy update-rates) verifies/replaces this in tariff.json.
DEFAULT_TARIFF = {
    "utility": "Evergy - Overland Park, KS",
    "summer_start": "06-01",
    "summer_end": "09-30",
    "peak_start_hour": 16,
    "peak_end_hour": 20,
    "seasons": {
        "non_summer": {"tiers": [{"up_to_kwh": 600, "rate": 0.12},
                                 {"up_to_kwh": 1000, "rate": 0.08},
                                 {"up_to_kwh": None, "rate": 0.07}],
                       "peak_adder": 0.003},
        "summer": {"tiers": [{"up_to_kwh": 600, "rate": 0.14},
                             {"up_to_kwh": 1000, "rate": 0.14},
                             {"up_to_kwh": None, "rate": 0.15}],
                   "peak_adder": 0.01},
    },
    "source": "user-provided prices",
    "updated": "2026-10-22T17:45:00",
}


# --------------------------------------------------------------------------- config

def _default_cpu_watts():
    """Rough package-power envelope from core count (used only without RAPL)."""
    cores = psutil.cpu_count(logical=False) or 4
    if cores >= 16:
        peak = 142.0
    elif cores >= 8:
        peak = 105.0
    elif cores >= 6:
        peak = 88.0
    else:
        peak = 65.0
    return round(peak * 0.18, 1), peak


def default_config_text():
    idle, peak = _default_cpu_watts()
    return f"""# Evergy Estimate configuration. Edit, then: systemctl --user restart evergy
# All wattages are estimates you can tune to your hardware.

[cost]
rate_per_kwh = 0.14        # flat price, used only when [tariff] enabled = false
currency = "$"

[tariff]
enabled = true             # tiered/seasonal/peak prices from tariff.json
household_other_kwh_per_month = 0  # rest of the house on the same meter; decides which tier the PC lands in
auto_update = true         # daily web check of current prices (evergy-rates.timer, 6 AM or at boot)
wait_for_internet_hours = 12
max_auto_change_pct = 25   # bigger jumps from a lookup are held for review: evergy rates --accept
lookup_model = "sonnet"

[sampling]
interval_seconds = 5       # how often the daemon samples
retention_days = 400       # minute-level history kept in the database

[cpu]
# Used only when RAPL energy counters are not readable (run setup-rapl.sh to enable them).
idle_watts = {idle}
max_watts = {peak}

[memory]
watts_per_gb_idle = 0.15   # DRAM refresh/standby
watts_per_gb_active = 0.12 # extra at 100% of RAM in use

[disk]
hdd_idle_watts = 4.5
hdd_active_watts = 3.0     # extra at 100% busy
ssd_idle_watts = 0.4
ssd_active_watts = 2.5
nvme_idle_watts = 1.5
nvme_active_watts = 5.0

[platform]
base_watts = 30.0          # motherboard, chipset, fans, USB, NIC
extra_watts = 0.0          # anything else on the same meter (monitor, speakers...)
psu_efficiency = 0.88      # wall power = component power / efficiency

[tray]
label = "{{wall_w:.0f}}W {{currency}}{{cost_day:.2f}}/d"
refresh_seconds = 2
"""


def load_config():
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            f.write(default_config_text())
    defaults = tomllib.loads(default_config_text())
    try:
        with open(CONFIG_PATH, "rb") as f:
            user = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"warning: bad config {CONFIG_PATH}: {e}; using defaults", file=sys.stderr)
        user = {}
    for section, values in user.items():
        if isinstance(values, dict):
            defaults.setdefault(section, {}).update(values)
    return defaults


# --------------------------------------------------------------------------- sensors

class Rapl:
    """CPU package energy from /sys/class/powercap (Intel and AMD Zen)."""

    def __init__(self):
        self.zones = []
        base = "/sys/class/powercap"
        if os.path.isdir(base):
            for name in sorted(os.listdir(base)):
                # top-level package zones only: intel-rapl:0, intel-rapl:1 ...
                if name.count(":") != 1:
                    continue
                path = os.path.join(base, name)
                try:
                    with open(os.path.join(path, "energy_uj")) as f:
                        int(f.read())
                    with open(os.path.join(path, "max_energy_range_uj")) as f:
                        rng = int(f.read())
                except (OSError, ValueError):
                    continue
                self.zones.append((path, rng))
        self.last = None

    @property
    def available(self):
        return bool(self.zones)

    def _read(self):
        vals = []
        for path, _ in self.zones:
            with open(os.path.join(path, "energy_uj")) as f:
                vals.append(int(f.read()))
        return time.monotonic(), vals

    def watts(self):
        now, vals = self._read()
        prev, self.last = self.last, (now, vals)
        if prev is None:
            return None
        dt = now - prev[0]
        if dt <= 0:
            return None
        total_uj = 0
        for (_, rng), a, b in zip(self.zones, prev[1], vals):
            d = b - a
            if d < 0:  # counter wrapped
                d += rng
            total_uj += d
        return total_uj / 1e6 / dt


def read_gpus():
    gpus = []
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,power.draw,utilization.gpu,memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4,
            ).stdout
            for line in out.strip().splitlines():
                p = [x.strip() for x in line.split(",")]
                if len(p) < 6:
                    continue
                gpus.append({
                    "name": p[0].replace("NVIDIA ", "").replace("GeForce ", ""),
                    "w": _num(p[1]), "pct": _num(p[2]),
                    "mem_used_gb": _num(p[3]) / 1024, "mem_total_gb": _num(p[4]) / 1024,
                    "temp": _num(p[5]),
                })
        except (OSError, subprocess.SubprocessError):
            pass
    # AMD / Intel discrete GPUs exposing hwmon power
    drm = "/sys/class/drm"
    if os.path.isdir(drm):
        for card in sorted(os.listdir(drm)):
            if "-" in card or not card.startswith("card"):
                continue
            dev = os.path.join(drm, card, "device")
            hw = os.path.join(dev, "hwmon")
            if not os.path.isdir(hw) or "nvidia" in os.path.realpath(os.path.join(dev, "driver")):
                continue
            for h in os.listdir(hw):
                for fn in ("power1_average", "power1_input"):
                    v = _read_int(os.path.join(hw, h, fn))
                    if v is not None:
                        gpus.append({
                            "name": card, "w": v / 1e6,
                            "pct": float(_read_int(os.path.join(dev, "gpu_busy_percent")) or 0),
                            "mem_used_gb": (_read_int(os.path.join(dev, "mem_info_vram_used")) or 0) / 2**30,
                            "mem_total_gb": (_read_int(os.path.join(dev, "mem_info_vram_total")) or 0) / 2**30,
                            "temp": (_read_int(os.path.join(hw, h, "temp1_input")) or 0) / 1000,
                        })
                        break
    return gpus


def _num(s):
    try:
        return float(s)
    except ValueError:
        return 0.0


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def physical_disks():
    disks = {}
    for name in os.listdir("/sys/block"):
        if not os.path.exists(f"/sys/block/{name}/device"):
            continue  # skips loop, dm, zram, md
        if name.startswith("nvme"):
            kind = "nvme"
        elif _read_int(f"/sys/block/{name}/queue/rotational") == 1:
            kind = "hdd"
        else:
            kind = "ssd"
        disks[name] = kind
    return disks


# --------------------------------------------------------------------------- sampler

class Sampler:
    def __init__(self, cfg):
        self.cfg = cfg
        self.rapl = Rapl()
        if self.rapl.available:
            self.rapl.watts()  # prime
        self.disks = physical_disks()
        self.last_io = self._io()
        self.last_t = time.monotonic()
        psutil.cpu_percent(None)

    def _io(self):
        try:
            return psutil.disk_io_counters(perdisk=True) or {}
        except Exception:
            return {}

    def sample(self):
        c = self.cfg
        now = time.monotonic()
        dt = max(now - self.last_t, 1e-3)
        self.last_t = now

        # CPU
        cpu_pct = psutil.cpu_percent(None)
        cpu_w = self.rapl.watts() if self.rapl.available else None
        cpu_src = "RAPL"
        if cpu_w is None:
            idle, peak = c["cpu"]["idle_watts"], c["cpu"]["max_watts"]
            cpu_w = idle + (peak - idle) * cpu_pct / 100
            cpu_src = "model"
        freq = psutil.cpu_freq()
        temps = {}
        try:
            temps = psutil.sensors_temperatures()
        except Exception:
            pass
        cpu_temp = None
        for key in ("k10temp", "coretemp", "zenpower", "cpu_thermal"):
            if temps.get(key):
                cpu_temp = temps[key][0].current
                break

        # GPU
        gpus = read_gpus()
        gpu_w = sum(g["w"] for g in gpus)
        gpu_pct = max((g["pct"] for g in gpus), default=0.0)

        # Memory
        vm = psutil.virtual_memory()
        total_gb = vm.total / 2**30
        used_frac = (vm.total - vm.available) / vm.total
        mem_w = total_gb * (c["memory"]["watts_per_gb_idle"] + c["memory"]["watts_per_gb_active"] * used_frac)

        # Disks
        io = self._io()
        disk_w, disk_list, busy_max = 0.0, [], 0.0
        for name, kind in sorted(self.disks.items()):
            busy = 0.0
            if name in io and name in self.last_io:
                busy = min(1.0, (io[name].busy_time - self.last_io[name].busy_time) / 1000 / dt)
                busy = max(busy, 0.0)
            w = c["disk"][f"{kind}_idle_watts"] + c["disk"][f"{kind}_active_watts"] * busy
            disk_w += w
            busy_max = max(busy_max, busy)
            disk_list.append({"name": name, "kind": kind, "busy": busy * 100, "w": w})
        self.last_io = io

        base_w = c["platform"]["base_watts"] + c["platform"]["extra_watts"]
        dc_w = cpu_w + gpu_w + mem_w + disk_w + base_w
        eff = min(max(c["platform"]["psu_efficiency"], 0.5), 1.0)
        wall_w = dc_w / eff

        return {
            "ts": time.time(),
            "cpu": {"w": cpu_w, "pct": cpu_pct, "source": cpu_src,
                    "freq_mhz": freq.current if freq else None, "temp": cpu_temp,
                    "cores": psutil.cpu_count()},
            "gpus": gpus, "gpu": {"w": gpu_w, "pct": gpu_pct},
            "memory": {"w": mem_w, "pct": vm.percent, "used_gb": (vm.total - vm.available) / 2**30,
                       "total_gb": total_gb, "swap_pct": psutil.swap_memory().percent},
            "disk": {"w": disk_w, "busy": busy_max * 100, "list": disk_list},
            "platform": {"w": base_w},
            "psu_loss_w": wall_w - dc_w,
            "psu_efficiency": eff,
            "dc_w": dc_w,
            "wall_w": wall_w,
        }


# --------------------------------------------------------------------------- storage

def open_db():
    os.makedirs(STATE_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS minutes (
        ts INTEGER NOT NULL, seconds REAL, wh REAL, cost REAL,
        wall_w REAL, cpu_w REAL, gpu_w REAL, mem_w REAL, disk_w REAL, base_w REAL,
        cpu_pct REAL, gpu_pct REAL, mem_pct REAL)""")
    db.execute("CREATE INDEX IF NOT EXISTS minutes_ts ON minutes(ts)")
    return db


def period_starts(now=None):
    now = datetime.fromtimestamp(now or time.time())
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    return {"today": day.timestamp(), "week": week.timestamp(), "month": month.timestamp()}


def totals(db):
    st = period_starts()
    out = {}
    for key, since in list(st.items()) + [("total", 0)]:
        wh, cost = db.execute("SELECT COALESCE(SUM(wh),0), COALESCE(SUM(cost),0) FROM minutes WHERE ts >= ?",
                              (since,)).fetchone()
        out[key] = {"kwh": wh / 1000, "cost": cost}
    first = db.execute("SELECT MIN(ts) FROM minutes").fetchone()[0]
    out["since"] = first
    # projection: average wall power over the last 24h -> 30.44-day month
    row = db.execute("SELECT SUM(wh), SUM(seconds) FROM minutes WHERE ts >= ?", (time.time() - 86400,)).fetchone()
    out["avg24_w"] = (row[0] * 3600 / row[1]) if row[1] else None
    return out


def write_live(obj):
    tmp = LIVE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, LIVE_PATH)


def read_live(max_age=None):
    try:
        with open(LIVE_PATH) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if max_age is not None and time.time() - d.get("ts", 0) > max_age:
        return None
    return d


def enrich(s, cfg, tot, pending_wh=0.0, pending_cost=0.0, tariff=None):
    """Attach cost figures to a sample."""
    t = {k: dict(v) for k, v in tot.items() if isinstance(v, dict)}
    for k in t:
        t[k]["kwh"] += pending_wh / 1000
        t[k]["cost"] += pending_cost
    now = datetime.now()
    month_kwh = t.get("month", {}).get("kwh", 0.0)
    info = rate_info(cfg, tariff if tariff is not None else load_tariff(), now, month_kwh)
    rate = info["rate"]
    s["currency"] = cfg["cost"]["currency"]
    s["rate_per_kwh"] = rate
    s["rate_info"] = info
    s["rate_check"] = last_check()
    s["cost_hour"] = s["wall_w"] / 1000 * rate
    s["cost_day"] = s["cost_hour"] * 24
    s["cost_month_at_rate"] = s["cost_day"] * 30.44
    s["totals"] = t
    s["since"] = tot.get("since")
    avg = tot.get("avg24_w") or s["wall_w"]
    s["avg24_w"] = avg
    proj_kwh = avg / 1000 * 24 * 30.44
    s["projected_month_kwh"] = proj_kwh
    s["projected_month_cost"] = projected_cost(cfg, info, proj_kwh, now)
    return s


# --------------------------------------------------------------------------- tariff

def validate_tariff(t):
    """Raise ValueError unless t is a sane tiered tariff."""
    for season in ("non_summer", "summer"):
        sd = t["seasons"][season]
        tiers = sd["tiers"]
        if not tiers:
            raise ValueError(f"{season}: no tiers")
        prev = 0
        for i, tr in enumerate(tiers):
            r, up = tr["rate"], tr["up_to_kwh"]
            if not (isinstance(r, (int, float)) and 0.02 <= r <= 0.60):
                raise ValueError(f"{season} tier {i + 1}: rate {r} outside $0.02-$0.60/kWh")
            last = i == len(tiers) - 1
            if last and up is not None:
                raise ValueError(f"{season}: last tier must be unbounded")
            if not last and (up is None or up <= prev):
                raise ValueError(f"{season} tier {i + 1}: bad upper bound {up}")
            prev = up or prev
        if not (0 <= sd.get("peak_adder", 0) <= 0.2):
            raise ValueError(f"{season}: peak adder {sd.get('peak_adder')} out of range")
    return t


def load_tariff():
    try:
        with open(TARIFF_PATH) as f:
            return validate_tariff(json.load(f))
    except FileNotFoundError:
        save_tariff(DEFAULT_TARIFF)
        return json.loads(json.dumps(DEFAULT_TARIFF))
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"warning: bad {TARIFF_PATH}: {e}; using built-in prices", file=sys.stderr)
        return json.loads(json.dumps(DEFAULT_TARIFF))


def save_tariff(t):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = TARIFF_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(t, f, indent=2)
    os.replace(tmp, TARIFF_PATH)


def season_of(t, when):
    md = when.strftime("%m-%d")
    return "summer" if t["summer_start"] <= md <= t["summer_end"] else "non_summer"


def tiered_cost(tiers, kwh):
    cost, lo = 0.0, 0.0
    for tr in tiers:
        hi = tr["up_to_kwh"] if tr["up_to_kwh"] is not None else float("inf")
        take = min(kwh, hi) - lo
        if take <= 0:
            break
        cost += take * tr["rate"]
        lo = hi
    return cost


def _month_fraction(when):
    start = when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return (when - start) / (nxt - start)


def rate_info(cfg, t, when, month_kwh):
    """Marginal price of the next kWh right now."""
    if not cfg["tariff"]["enabled"]:
        r = cfg["cost"]["rate_per_kwh"]
        return {"rate": r, "energy_rate": r, "peak_adder": 0.0, "peak": False, "season": "flat",
                "tier": 1, "tier_upto": None, "source": "config rate_per_kwh", "updated": None}
    season = season_of(t, when)
    sd = t["seasons"][season]
    pos = month_kwh + cfg["tariff"]["household_other_kwh_per_month"] * _month_fraction(when)
    tier = len(sd["tiers"]) - 1
    for i, tr in enumerate(sd["tiers"]):
        if tr["up_to_kwh"] is None or pos < tr["up_to_kwh"]:
            tier = i
            break
    energy = sd["tiers"][tier]["rate"]
    peak = t["peak_start_hour"] <= when.hour < t["peak_end_hour"]
    adder = sd.get("peak_adder", 0.0) if peak else 0.0
    return {"rate": energy + adder, "energy_rate": energy, "peak_adder": adder, "peak": peak,
            "season": season, "tier": tier + 1, "tier_upto": sd["tiers"][tier]["up_to_kwh"],
            "season_peak_adder": sd.get("peak_adder", 0.0),
            "peak_hours": f"{t['peak_start_hour']}:00-{t['peak_end_hour']}:00",
            "source": t.get("source"), "updated": t.get("updated")}


def projected_cost(cfg, info, kwh, when):
    """PC's share of a month's bill: tiers stack on top of the rest of the household."""
    if info["season"] == "flat":
        return kwh * info["rate"]
    t = load_tariff()
    sd = t["seasons"][info["season"]]
    other = cfg["tariff"]["household_other_kwh_per_month"]
    peak_frac = (t["peak_end_hour"] - t["peak_start_hour"]) / 24
    return (tiered_cost(sd["tiers"], other + kwh) - tiered_cost(sd["tiers"], other)
            + kwh * peak_frac * sd.get("peak_adder", 0.0))


def last_check():
    try:
        with open(TARIFF_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            return json.loads(f.read().decode().strip().splitlines()[-1])
    except (OSError, ValueError, IndexError):
        return None


def log_check(entry):
    os.makedirs(STATE_DIR, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
    with open(TARIFF_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def notify(title, body):
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Evergy Estimate", "-i", "dialog-information", title, body],
                       capture_output=True, timeout=10)


def fmt_tariff(t):
    lines = []
    md = lambda s, shift=0: (datetime.strptime(f"2001-{s}", "%Y-%m-%d") + timedelta(days=shift)).strftime("%b %-d")
    for season, label in (("non_summer", f"Non-summer ({md(t['summer_end'], 1)} - {md(t['summer_start'], -1)})"),
                          ("summer", f"Summer ({md(t['summer_start'])} - {md(t['summer_end'])})")):
        sd = t["seasons"][season]
        lines.append(label)
        lo = 0
        for tr in sd["tiers"]:
            up = tr["up_to_kwh"]
            rng = f"{lo:>5.0f}-{up:<5.0f} kWh" if up is not None else f"{lo:>5.0f}+      kWh"
            lines.append(f"   {rng}  ${tr['rate']:.4f}/kWh")
            lo = up or lo
        lines.append(f"   peak {t['peak_start_hour']}:00-{t['peak_end_hour']}:00 adder  ${sd.get('peak_adder', 0):.4f}/kWh")
    return "\n".join(lines)


LOOKUP_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["found", "non_summer_tiers", "summer_tiers", "non_summer_peak_adder", "summer_peak_adder",
                 "sources", "notes"],
    "properties": {
        "found": {"type": "boolean"},
        "non_summer_tiers": {"type": "array", "items": {"type": "object", "required": ["up_to_kwh", "rate"],
                             "properties": {"up_to_kwh": {"type": ["number", "null"]}, "rate": {"type": "number"}}}},
        "summer_tiers": {"type": "array", "items": {"type": "object", "required": ["up_to_kwh", "rate"],
                         "properties": {"up_to_kwh": {"type": ["number", "null"]}, "rate": {"type": "number"}}}},
        "non_summer_peak_adder": {"type": "number"},
        "summer_peak_adder": {"type": "number"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}


def _lookup_prompt(current):
    return f"""Today is {datetime.now():%Y-%m-%d}. Find the CURRENT Evergy residential electricity prices in
dollars per kWh for Overland Park, Kansas (Johnson County), standard residential service. Search the web
(evergy.com rate pages and tariffs, Kansas Corporation Commission, CURB, news about rate changes).

Report the price a customer effectively pays per kWh: the per-kWh energy charge PLUS per-kWh riders
(fuel/energy cost adjustment, transmission, etc.) when you can find them, so it is comparable to the
currently configured values below. Exclude the fixed monthly customer charge.

For non-summer (Oct 1 - May 31) and summer (Jun 1 - Sep 30) give the tiers in order: each tier's
cumulative upper bound in monthly kWh (null for the last, unbounded tier) and its rate. Give the daily
4-8 pm peak adder per kWh for each season (0 if the plan has none).

Currently configured (may be outdated or wrong - verify, do not just copy):
{json.dumps(current["seasons"])}

If you cannot find credible current figures, set found=false and explain in notes. Never invent numbers.
List the URLs you relied on in sources."""


def _run_lookup(cfg, current):
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not os.path.exists(claude):
        raise RuntimeError("claude CLI not found")
    os.makedirs(STATE_DIR, exist_ok=True)
    # Web tools only: the lookup can read pages but cannot touch this machine.
    cmd = [claude, "-p", _lookup_prompt(current),
           "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
           "--json-schema", json.dumps(LOOKUP_SCHEMA), "--output-format", "json",
           "--no-session-persistence", "--model", cfg["tariff"]["lookup_model"]]
    p = subprocess.run(cmd, cwd=STATE_DIR, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=20 * 60)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError(f"lookup failed (exit {p.returncode}): {(p.stderr or p.stdout)[-300:]}")
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"lookup error: {str(out.get('result') or out.get('subtype'))[:300]}")
    return out["structured_output"], out.get("total_cost_usd")


def _online(timeout=5):
    import socket
    for host in ("api.anthropic.com", "www.google.com"):
        try:
            socket.create_connection((host, 443), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def _max_change_pct(old, new):
    worst = 0.0
    for season in ("non_summer", "summer"):
        o, n = old["seasons"][season]["tiers"], new["seasons"][season]["tiers"]
        for i, tr in enumerate(n):
            ref = o[min(i, len(o) - 1)]["rate"]
            worst = max(worst, abs(tr["rate"] - ref) / ref * 100)
    return worst


def cmd_update_rates(cfg, force=False):
    """Daily price check. Exit 0 when done for today, 1 to have systemd retry later."""
    if not cfg["tariff"]["auto_update"] and not force:
        print("auto_update disabled in config")
        return 0
    lc = last_check()
    today = datetime.now().strftime("%Y-%m-%d")
    if not force and lc and lc.get("ts", "").startswith(today) and lc.get("status") != "error":
        print(f"already checked today ({lc['ts']}, {lc['status']})")
        return 0

    deadline = time.time() + cfg["tariff"]["wait_for_internet_hours"] * 3600
    while not _online():
        if time.time() > deadline:
            log_check({"status": "error", "message": "no internet connection"})
            return 1
        print("waiting for internet...", flush=True)
        time.sleep(60)

    current = load_tariff()
    try:
        res, usd = _run_lookup(cfg, current)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        log_check({"status": "error", "message": str(e)})
        print(f"lookup failed: {e}")
        return 1

    base = {k: current[k] for k in ("utility", "summer_start", "summer_end", "peak_start_hour", "peak_end_hour")}
    sources = [s for s in res.get("sources", []) if s.startswith("http")]
    entry = {"sources": sources[:5], "notes": res.get("notes", "")[:1500], "lookup_cost_usd": usd}
    if not res.get("found") or not sources:
        entry.update(status="not_found", message="no credible prices found; keeping current prices")
        print(log_check(entry)["message"])
        return 0
    new = {**base, "seasons": {
        "non_summer": {"tiers": res["non_summer_tiers"], "peak_adder": res["non_summer_peak_adder"]},
        "summer": {"tiers": res["summer_tiers"], "peak_adder": res["summer_peak_adder"]}},
        "source": sources[0], "updated": datetime.now().isoformat(timespec="seconds")}
    try:
        validate_tariff(new)
    except (ValueError, KeyError, TypeError) as e:
        entry.update(status="rejected", message=f"lookup result failed validation: {e}", proposed=new["seasons"])
        print(log_check(entry)["message"])
        return 0

    if new["seasons"] == current["seasons"]:
        entry.update(status="unchanged", message="prices confirmed, no change")
        print(log_check(entry)["message"])
        return 0

    change = _max_change_pct(current, new)
    if change > cfg["tariff"]["max_auto_change_pct"] and not force:
        with open(TARIFF_PENDING, "w") as f:
            json.dump(new, f, indent=2)
        entry.update(status="needs_review", proposed=new["seasons"],
                     message=f"lookup suggests prices changed by up to {change:.0f}%; held for review")
        print(log_check(entry)["message"])
        notify("Evergy prices need review",
               f"Daily check found prices up to {change:.0f}% different.\nRun: evergy rates  (then evergy rates --accept)")
        return 0

    save_tariff(new)
    entry.update(status="updated", previous=current["seasons"], message=f"prices updated (max change {change:.1f}%)")
    print(log_check(entry)["message"])
    notify("Evergy prices updated", f"{entry['message']}\nsource: {sources[0]}")
    return 0


def cmd_rates(cfg, accept=False, reset=False):
    if reset:
        save_tariff(DEFAULT_TARIFF)
        print("Restored the built-in (user-provided) prices.")
    if accept:
        try:
            with open(TARIFF_PENDING) as f:
                new = validate_tariff(json.load(f))
        except (OSError, ValueError, KeyError) as e:
            print(f"nothing to accept ({e})")
            return
        save_tariff(new)
        os.remove(TARIFF_PENDING)
        log_check({"status": "updated", "message": "pending prices accepted by user"})
        print("Accepted.")
    t = load_tariff()
    print(f"{t['utility']}  (source: {t.get('source')}, updated {t.get('updated')})")
    print(fmt_tariff(t))
    info = rate_info(cfg, t, datetime.now(), 0)
    print(f"\nRight now: {info['season']} season, peak={'yes' if info['peak'] else 'no'} -> "
          f"${info['rate']:.4f}/kWh for tier-1 usage")
    lc = last_check()
    if lc:
        print(f"\nLast price check: {lc['ts']}  {lc['status']}: {lc.get('message', '')}")
        for s in lc.get("sources", []):
            print(f"   {s}")
        if lc.get("notes"):
            print(f"   notes: {lc['notes']}")
    if os.path.exists(TARIFF_PENDING):
        with open(TARIFF_PENDING) as f:
            print("\nPENDING (run `evergy rates --accept` to apply):")
            print(fmt_tariff(json.load(f)))


# --------------------------------------------------------------------------- daemon

def run_daemon(cfg):
    interval = max(1.0, float(cfg["sampling"]["interval_seconds"]))
    db = open_db()
    sampler = Sampler(cfg)
    tariff, tariff_mtime = load_tariff(), _mtime(TARIFF_PATH)
    tot = totals(db)
    acc = _new_acc()
    minute = int(time.time() // 60 * 60)
    last_prune = 0
    print(f"evergy daemon: interval={interval}s cpu={'RAPL' if sampler.rapl.available else 'model'} "
          f"gpus={len(read_gpus())} disks={sampler.disks} db={DB_PATH}", flush=True)
    time.sleep(min(interval, 2))
    last = time.monotonic()
    # shutdown/reboot sends SIGTERM: save the partial minute instead of dropping it
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while True:
            s = sampler.sample()
            now = time.monotonic()
            dt = now - last
            last = now
            if dt > interval * 4:  # suspend/resume or stall: don't bill the gap
                dt = interval
            wh = s["wall_w"] * dt / 3600
            acc["seconds"] += dt
            acc["wh"] += wh
            month_kwh = tot.get("month", {}).get("kwh", 0.0) + acc["wh"] / 1000
            acc["cost"] += wh / 1000 * rate_info(cfg, tariff, datetime.now(), month_kwh)["rate"]
            for k, v in (("wall_w", s["wall_w"]), ("cpu_w", s["cpu"]["w"]), ("gpu_w", s["gpu"]["w"]),
                         ("mem_w", s["memory"]["w"]), ("disk_w", s["disk"]["w"]), ("base_w", s["platform"]["w"]),
                         ("cpu_pct", s["cpu"]["pct"]), ("gpu_pct", s["gpu"]["pct"]), ("mem_pct", s["memory"]["pct"])):
                acc[k] += v * dt

            cur_minute = int(time.time() // 60 * 60)
            if cur_minute != minute:
                _flush(db, minute, acc)
                acc = _new_acc()
                minute = cur_minute
                tot = totals(db)
                if _mtime(TARIFF_PATH) != tariff_mtime:  # daily price check rewrote it
                    tariff, tariff_mtime = load_tariff(), _mtime(TARIFF_PATH)
                if time.time() - last_prune > 3600:
                    db.execute("DELETE FROM minutes WHERE ts < ?",
                               (time.time() - cfg["sampling"]["retention_days"] * 86400,))
                    db.commit()
                    last_prune = time.time()

            s["interval"] = interval
            s["daemon_pid"] = os.getpid()
            write_live(enrich(s, cfg, tot, acc["wh"], acc["cost"], tariff))
            time.sleep(max(0.2, interval - (time.monotonic() - now)))
    finally:
        _flush(db, minute, acc)
        db.close()


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _new_acc():
    return {k: 0.0 for k in ("seconds", "wh", "cost", "wall_w", "cpu_w", "gpu_w", "mem_w", "disk_w",
                             "base_w", "cpu_pct", "gpu_pct", "mem_pct")}


def _flush(db, minute, acc):
    sec = acc["seconds"]
    if sec <= 0:
        return
    avg = {k: acc[k] / sec for k in acc if k not in ("seconds", "wh", "cost")}
    db.execute("INSERT INTO minutes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (minute, sec, acc["wh"], acc["cost"], avg["wall_w"], avg["cpu_w"], avg["gpu_w"],
                avg["mem_w"], avg["disk_w"], avg["base_w"], avg["cpu_pct"], avg["gpu_pct"], avg["mem_pct"]))
    db.commit()


def live_or_local(cfg, sampler_holder):
    """Daemon snapshot if fresh, else sample in-process (TUI works without the daemon)."""
    interval = cfg["sampling"]["interval_seconds"]
    d = read_live(max_age=interval * 3 + 2)
    if d:
        d["_source"] = "daemon"
        return d
    if sampler_holder.get("s") is None:
        sampler_holder["s"] = Sampler(cfg)
        time.sleep(0.5)
    tot = {"since": None, "avg24_w": None}
    if os.path.exists(DB_PATH):
        try:
            tot = totals(open_db())
        except sqlite3.Error:
            pass
    d = enrich(sampler_holder["s"].sample(), cfg, tot)
    d["_source"] = "local (daemon offline)"
    return d


# --------------------------------------------------------------------------- formatting helpers

def money(v, cur):
    if abs(v) < 1:
        return f"{cur}{v:.3f}"
    return f"{cur}{v:,.2f}"


def bar(pct, width):
    pct = max(0.0, min(100.0, pct or 0))
    full = pct / 100 * width
    n = int(full)
    parts = " ▏▎▍▌▋▊▉"
    rem = parts[int((full - n) * 8)] if n < width else ""
    return ("█" * n + rem).ljust(width)


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values, width):
    if not values:
        return ""
    if len(values) > width:  # bucket average
        step = len(values) / width
        values = [sum(values[int(i * step):int((i + 1) * step)] or [0]) /
                  max(1, len(values[int(i * step):int((i + 1) * step)])) for i in range(width)]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    return "".join(SPARK[int((v - lo) / span * 7)] for v in values)


def rate_line(d):
    i = d.get("rate_info") or {}
    if i.get("season") == "flat":
        return f"{i['rate'] * 100:.2f}¢/kWh (flat)"
    up = f"≤{i['tier_upto']:.0f}" if i.get("tier_upto") is not None else "top"
    peak = f" + peak {i['peak_adder'] * 100:.1f}¢ ({i['peak_hours']})" if i.get("peak") else \
        f", off-peak (peak {i.get('peak_hours')} +{i.get('season_peak_adder', 0) * 100:.1f}¢)"
    season = "summer" if i.get("season") == "summer" else "non-summer"
    return f"{i['rate'] * 100:.2f}¢/kWh  {season} tier {i['tier']} ({up} kWh/mo) {i['energy_rate'] * 100:.1f}¢{peak}"


def check_line(d):
    c = d.get("rate_check")
    if not c:
        return "not checked yet (daily at 6 AM, or at boot if missed)"
    return f"{c['ts'].replace('T', ' ')[:16]}  {c['status']}"


# --------------------------------------------------------------------------- status / report

def cmd_status(cfg):
    d = live_or_local(cfg, {})
    cur = d["currency"]
    print(f"Source: {d['_source']}")
    print(f"Wall power   {d['wall_w']:7.1f} W   {money(d['cost_hour'], cur)}/h   "
          f"{money(d['cost_day'], cur)}/day   {money(d['cost_month_at_rate'], cur)}/month at this rate")
    print(f"  CPU        {d['cpu']['w']:7.1f} W   {d['cpu']['pct']:5.1f}%  ({d['cpu']['source']})")
    for g in d["gpus"]:
        print(f"  GPU        {g['w']:7.1f} W   {g['pct']:5.1f}%  {g['name']}")
    print(f"  Memory     {d['memory']['w']:7.1f} W   {d['memory']['pct']:5.1f}%")
    print(f"  Disks      {d['disk']['w']:7.1f} W   {d['disk']['busy']:5.1f}% busy")
    print(f"  Platform   {d['platform']['w']:7.1f} W")
    print(f"  PSU loss   {d['psu_loss_w']:7.1f} W")
    print(f"Price now    {rate_line(d)}")
    for k, label in (("today", "Today"), ("week", "This week"), ("month", "This month"), ("total", "All time")):
        t = d["totals"].get(k)
        if t:
            print(f"{label:<12} {t['kwh']:8.3f} kWh   {money(t['cost'], cur)}")
    print(f"Price check  {check_line(d)}")
    print(f"Projected month (24h avg {d['avg24_w']:.0f} W): {d['projected_month_kwh']:.1f} kWh  "
          f"{money(d['projected_month_cost'], cur)}")


def cmd_report(cfg, days):
    if not os.path.exists(DB_PATH):
        print("No data yet - is the daemon running? (systemctl --user status evergy)")
        return
    db = open_db()
    cur = cfg["cost"]["currency"]
    since = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)).timestamp()
    rows = db.execute("""SELECT date(ts,'unixepoch','localtime') d, SUM(wh)/1000, SUM(cost), SUM(seconds)/3600,
                         SUM(wall_w*seconds)/SUM(seconds), MAX(wall_w), SUM(cpu_w*seconds)/SUM(seconds),
                         SUM(gpu_w*seconds)/SUM(seconds)
                         FROM minutes WHERE ts >= ? GROUP BY d ORDER BY d""", (since,)).fetchall()
    print(f"{'Date':<12}{'kWh':>9}{'Cost':>11}{'Hours':>7}{'Avg W':>8}{'Peak W':>8}{'CPU W':>7}{'GPU W':>7}")
    tk = tc = 0
    for d, kwh, cost, hrs, avg, peak, cw, gw in rows:
        tk += kwh
        tc += cost
        print(f"{d:<12}{kwh:>9.3f}{money(cost, cur):>11}{hrs:>7.1f}{avg:>8.0f}{peak:>8.0f}{cw:>7.0f}{gw:>7.0f}")
    print("-" * 69)
    print(f"{'Total':<12}{tk:>9.3f}{money(tc, cur):>11}")


# --------------------------------------------------------------------------- TUI

def cmd_tui(cfg):
    import curses
    curses.wrapper(lambda scr: _tui(scr, cfg))


def _tui(scr, cfg):
    import curses
    curses.curs_set(0)
    curses.use_default_colors()
    for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED,
                           curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_BLUE), 1):
        curses.init_pair(i, c, -1)
    GREEN, YELLOW, RED, CYAN, MAG, BLUE = (curses.color_pair(i) for i in range(1, 7))
    BOLD, DIM = curses.A_BOLD, curses.A_DIM
    scr.timeout(1000)
    holder = {}
    procs = {}
    hist, hist_t = [], 0
    view_hours = 1

    def level(p):
        return GREEN if p < 50 else YELLOW if p < 80 else RED

    while True:
        d = live_or_local(cfg, holder)
        cur = d["currency"]
        h, w = scr.getmaxyx()
        scr.erase()

        def put(y, x, text, attr=0):
            if 0 <= y < h - 0 and x < w:
                try:
                    scr.addnstr(y, x, text, max(0, w - x - 1), attr)
                except curses.error:
                    pass

        bw = max(10, min(30, w - 52))
        put(0, 1, "⚡ Evergy Estimate", BOLD | CYAN)
        put(0, 20, "system power & electricity cost", DIM)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        put(0, max(1, w - len(stamp) - 2), stamp, DIM)
        put(1, 1, "─" * (w - 3), DIM)

        put(2, 1, "TOTAL @ WALL", BOLD)
        put(2, 15, f"{d['wall_w']:7.1f} W", BOLD | YELLOW)
        put(2, 27, f"{money(d['cost_hour'], cur)}/hour  ·  {money(d['cost_day'], cur)}/day  ·  "
                   f"{money(d['cost_month_at_rate'], cur)}/month at this rate", BOLD)

        y = 4
        cpu = d["cpu"]
        extra = f"{cpu['cores']} threads"
        if cpu.get("freq_mhz"):
            extra += f"  {cpu['freq_mhz']/1000:.2f} GHz"
        if cpu.get("temp"):
            extra += f"  {cpu['temp']:.0f}°C"
        rows = [("CPU", cpu["pct"], cpu["w"], f"{extra}  [{cpu['source']}]")]
        for g in d["gpus"]:
            rows.append(("GPU", g["pct"], g["w"],
                         f"{g['name']}  {g['temp']:.0f}°C  VRAM {g['mem_used_gb']:.1f}/{g['mem_total_gb']:.0f} GB"))
        if not d["gpus"]:
            rows.append(("GPU", 0, 0, "none detected"))
        m = d["memory"]
        rows.append(("MEMORY", m["pct"], m["w"], f"{m['used_gb']:.1f}/{m['total_gb']:.0f} GB  swap {m['swap_pct']:.0f}%"))
        dk = d["disk"]
        rows.append(("DISK", dk["busy"], dk["w"],
                     "  ".join(f"{x['name']}:{x['busy']:.0f}%" for x in dk["list"])))
        for name, pct, watts, info in rows:
            put(y, 1, f"{name:<7}", BOLD)
            put(y, 9, bar(pct, bw), level(pct))
            put(y, 10 + bw, f"{pct:5.1f}%", level(pct))
            put(y, 18 + bw, f"{watts:6.1f} W", BOLD)
            put(y, 28 + bw, info, DIM)
            y += 1
        put(y, 1, f"{'BASE':<7}", BOLD)
        put(y, 18 + bw, f"{d['platform']['w']:6.1f} W", BOLD)
        put(y, 28 + bw, "motherboard, fans, chipset, extras", DIM)
        y += 1
        put(y, 1, f"{'PSU':<7}", BOLD)
        put(y, 18 + bw, f"{d['psu_loss_w']:6.1f} W", BOLD)
        put(y, 28 + bw, f"conversion loss at {d['psu_efficiency']*100:.0f}% efficiency", DIM)
        y += 2

        # share of wall power
        share_w = max(10, w - 4)
        parts = [(d["cpu"]["w"], BLUE), (d["gpu"]["w"], MAG), (m["w"], CYAN), (dk["w"], GREEN),
                 (d["platform"]["w"] + d["psu_loss_w"], DIM)]
        x = 1
        for watts, col in parts:
            n = int(round(watts / d["wall_w"] * share_w)) if d["wall_w"] else 0
            put(y, x, "█" * n, col)
            x += n
        y += 1
        legend = [("CPU", BLUE), ("GPU", MAG), ("MEM", CYAN), ("DISK", GREEN), ("BASE+PSU", DIM)]
        x = 1
        for (label, col), (watts, _) in zip(legend, parts):
            txt = f"■ {label} {watts / d['wall_w'] * 100 if d['wall_w'] else 0:.0f}%  "
            put(y, x, txt, col)
            x += len(txt)
        y += 2

        # energy & cost table
        put(y, 1, f"{'ENERGY & COST':<20}{'kWh':>10}{'Cost':>12}", BOLD | CYAN)
        y += 1
        for k, label in (("today", "Today"), ("week", "This week"), ("month", "This month"), ("total", "All time")):
            t = d["totals"].get(k)
            if t:
                put(y, 1, f"{label:<20}{t['kwh']:>10.3f}{money(t['cost'], cur):>12}")
                y += 1
        put(y, 1, f"{'Projected month':<20}{d['projected_month_kwh']:>10.1f}{money(d['projected_month_cost'], cur):>12}",
            YELLOW)
        put(y, 45, f"(24h avg {d['avg24_w']:.0f} W, PC's share of the bill)", DIM)
        y += 1
        put(y, 1, "Price now  ", BOLD)
        put(y, 12, rate_line(d), YELLOW if (d.get("rate_info") or {}).get("peak") else 0)
        y += 1
        put(y, 1, f"Price check {check_line(d)}", DIM)
        if d.get("since"):
            put(y, 48, f"tracking since {datetime.fromtimestamp(d['since']):%Y-%m-%d %H:%M}", DIM)
        y += 2

        # history sparkline (from DB, refreshed every 30s)
        if time.time() - hist_t > 30 and os.path.exists(DB_PATH):
            try:
                hist = [r[0] for r in open_db().execute(
                    "SELECT wall_w FROM minutes WHERE ts >= ? ORDER BY ts", (time.time() - view_hours * 3600,))]
            except sqlite3.Error:
                hist = []
            hist_t = time.time()
        if y < h - 2:
            label = f"WALL POWER, LAST {view_hours}H" if view_hours < 24 else f"WALL POWER, LAST {view_hours // 24}D"
            put(y, 1, label, BOLD | CYAN)
            if hist:
                put(y, 30, f"min {min(hist):.0f} W  avg {sum(hist)/len(hist):.0f} W  max {max(hist):.0f} W", DIM)
                put(y + 1, 1, sparkline(hist, w - 4), YELLOW)
            else:
                put(y + 1, 1, "collecting... (needs the daemon running for a few minutes)", DIM)
            y += 3

        # top processes with estimated CPU watts
        if y < h - 3:
            put(y, 1, f"{'TOP PROCESSES':<28}{'CPU%':>7}{'MEM%':>7}{'~CPU W':>8}", BOLD | CYAN)
            y += 1
            ncpu = psutil.cpu_count() or 1
            seen = {}
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if p.pid not in procs:
                        procs[p.pid] = p
                        p.cpu_percent(None)
                        continue
                    seen[p.pid] = (procs[p.pid].cpu_percent(None) / ncpu, p.memory_percent(), p.info["name"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            for pid in list(procs):
                if pid not in seen and not psutil.pid_exists(pid):
                    procs.pop(pid, None)
            total_pct = sum(v[0] for v in seen.values()) or 1
            dyn_cpu_w = d["cpu"]["w"]
            for pid, (pct, mem, name) in sorted(seen.items(), key=lambda kv: -kv[1][0])[:max(0, h - y - 2)]:
                est = dyn_cpu_w * pct / total_pct if d["cpu"]["pct"] > 0 else 0
                put(y, 1, f"{(name or '?')[:20]:<20}{pid:>8}{pct:>7.1f}{mem:>7.1f}{est:>8.1f}")
                y += 1

        put(h - 1, 1, f"q quit  ·  h history 1h/24h/7d  ·  source: {d['_source']}  ·  config: {CONFIG_PATH}", DIM)
        scr.refresh()
        ch = scr.getch()
        if ch in (ord("q"), ord("Q"), 27):
            return
        if ch in (ord("h"), ord("H")):
            view_hours = {1: 24, 24: 168, 168: 1}[view_hours]
            hist_t = 0


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(prog="evergy", description="System power & electricity cost monitor")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("daemon", help="run the 24/7 sampler")
    sub.add_parser("tui", help="live terminal dashboard (default)")
    sub.add_parser("status", help="one-shot summary")
    r = sub.add_parser("report", help="daily energy/cost table")
    r.add_argument("--days", type=int, default=14)
    sub.add_parser("tray", help="top-bar indicator")
    u = sub.add_parser("update-rates", help="look up current Evergy prices on the web (run daily by a timer)")
    u.add_argument("--force", action="store_true", help="run even if already checked today; skip change guard")
    rr = sub.add_parser("rates", help="show electricity prices and the last price check")
    rr.add_argument("--accept", action="store_true", help="apply prices held for review")
    rr.add_argument("--reset", action="store_true", help="restore the built-in user-provided prices")
    sub.add_parser("config", help="print config path")
    args = ap.parse_args()
    cfg = load_config()
    if args.cmd == "daemon":
        try:
            run_daemon(cfg)
        except KeyboardInterrupt:
            pass
    elif args.cmd == "status":
        cmd_status(cfg)
    elif args.cmd == "report":
        cmd_report(cfg, args.days)
    elif args.cmd == "tray":
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import evergy_tray
        evergy_tray.main()
    elif args.cmd == "update-rates":
        sys.exit(cmd_update_rates(cfg, args.force))
    elif args.cmd == "rates":
        cmd_rates(cfg, args.accept, args.reset)
    elif args.cmd == "config":
        print(CONFIG_PATH)
    else:
        cmd_tui(cfg)


if __name__ == "__main__":
    main()
