#!/usr/bin/python3
"""Evergy Estimate top-bar indicator (GNOME AppIndicator / Ayatana).

Reads the daemon's live snapshot and shows wall power + cost in the top bar,
with a per-component breakdown in the dropdown menu.
"""
import os
import shutil
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator
from gi.repository import GLib, Gtk

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import evergy  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
ICON = os.path.join(HERE, "evergy-icon.svg")


class Tray:
    def __init__(self):
        self.cfg = evergy.load_config()
        icon = ICON if os.path.exists(ICON) else "utilities-system-monitor-symbolic"
        self.ind = AppIndicator.Indicator.new("evergy-estimate", icon,
                                              AppIndicator.IndicatorCategory.SYSTEM_SERVICES)
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.ind.set_title("Evergy Estimate")

        self.menu = Gtk.Menu()
        self.items = {}
        for key in ("wall", "rate", "price", "check", None, "cpu", "gpu", "mem", "disk", "base", None,
                    "today", "week", "month", "total", "proj", None):
            if key is None:
                self.menu.append(Gtk.SeparatorMenuItem())
                continue
            it = Gtk.MenuItem(label="…")
            it.set_sensitive(False)
            self.items[key] = it
            self.menu.append(it)
        for label, cb in (("Open dashboard (terminal)", self.open_tui),
                          ("Daily report (terminal)", self.open_report),
                          ("Electricity prices (terminal)", self.open_rates),
                          ("Check prices now", self.check_prices),
                          ("Edit settings…", self.edit_config),
                          ("Restart monitor daemon", self.restart_daemon),
                          ("Hide indicator", lambda *_: Gtk.main_quit())):
            it = Gtk.MenuItem(label=label)
            it.connect("activate", cb)
            self.menu.append(it)
        self.menu.show_all()
        self.ind.set_menu(self.menu)

        self.refresh()
        GLib.timeout_add_seconds(max(1, int(self.cfg["tray"]["refresh_seconds"])), self.refresh)

    def set(self, key, text):
        self.items[key].set_label(text)

    def refresh(self):
        interval = self.cfg["sampling"]["interval_seconds"]
        d = evergy.read_live(max_age=interval * 3 + 2)
        if not d:
            self.ind.set_label("⚡ offline", "⚡ 000W $0.00/d")
            self.set("wall", "Monitor daemon not running")
            self.set("rate", "systemctl --user start evergy")
            return True
        cur = d["currency"]
        m = evergy.money
        try:
            label = self.cfg["tray"]["label"].format(**d)
        except (KeyError, ValueError, IndexError):
            label = f"{d['wall_w']:.0f}W"
        self.ind.set_label(label, "000W $0.00/d")

        self.set("wall", f"Wall power: {d['wall_w']:.1f} W  (components {d['dc_w']:.1f} W)")
        self.set("rate", f"Cost now: {m(d['cost_hour'], cur)}/h · {m(d['cost_day'], cur)}/day · "
                         f"{m(d['cost_month_at_rate'], cur)}/mo")
        self.set("price", f"Price: {evergy.rate_line(d)}")
        self.set("check", f"Price check: {evergy.check_line(d)}")
        c = d["cpu"]
        self.set("cpu", f"CPU     {c['pct']:5.1f}%   {c['w']:6.1f} W   ({c['source']})")
        if d["gpus"]:
            g = d["gpus"][0]
            more = f" +{len(d['gpus']) - 1}" if len(d["gpus"]) > 1 else ""
            self.set("gpu", f"GPU     {d['gpu']['pct']:5.1f}%   {d['gpu']['w']:6.1f} W   {g['name']}{more} {g['temp']:.0f}°C")
        else:
            self.set("gpu", "GPU     none detected")
        mm = d["memory"]
        self.set("mem", f"Memory  {mm['pct']:5.1f}%   {mm['w']:6.1f} W   {mm['used_gb']:.1f}/{mm['total_gb']:.0f} GB")
        self.set("disk", f"Disk    {d['disk']['busy']:5.1f}%   {d['disk']['w']:6.1f} W")
        self.set("base", f"Base + PSU loss      {d['platform']['w'] + d['psu_loss_w']:6.1f} W")
        t = d["totals"]
        for key, label in (("today", "Today"), ("week", "This week"), ("month", "This month"), ("total", "All time")):
            if key in t:
                self.set(key, f"{label:<11} {t[key]['kwh']:8.2f} kWh   {m(t[key]['cost'], cur)}")
        self.set("proj", f"Projected month: {d['projected_month_kwh']:.0f} kWh  {m(d['projected_month_cost'], cur)}")
        return True

    def _terminal(self, *args):
        cmd = [sys.executable, os.path.join(HERE, "evergy.py"), *args]
        if args and args[0] in ("report", "rates"):
            cmd = ["bash", "-c", " ".join(cmd) + "; echo; read -n1 -rsp 'Press any key to close'"]
        for term, pre in (("gnome-terminal", ["--"]), ("x-terminal-emulator", ["-e"]),
                          ("konsole", ["-e"]), ("xterm", ["-e"])):
            if shutil.which(term):
                subprocess.Popen([term, *pre, *cmd], start_new_session=True)
                return

    def open_tui(self, *_):
        self._terminal("tui")

    def open_report(self, *_):
        self._terminal("report")

    def open_rates(self, *_):
        self._terminal("rates")

    def check_prices(self, *_):
        # same unit the daily timer runs; result arrives as a desktop notification
        subprocess.Popen(["systemctl", "--user", "start", "evergy-rates.service"])

    def edit_config(self, *_):
        evergy.load_config()
        subprocess.Popen(["xdg-open", evergy.CONFIG_PATH], start_new_session=True)

    def restart_daemon(self, *_):
        subprocess.Popen(["systemctl", "--user", "restart", "evergy.service"])
        self.cfg = evergy.load_config()


def main():
    Tray()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, Gtk.main_quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 15, Gtk.main_quit)
    Gtk.main()


if __name__ == "__main__":
    main()
