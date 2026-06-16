#!/usr/bin/env python3
# =================================================================================
# CPU Thermal Guard
# =================================================================================
# Description: Sustain-gated NBFC fan pulser for Linux. Polls the CPU package
#              temperature and only acts when it stays inside a "hot band" for a
#              sustained window, then pulses the fan with an escalating burst
#              duration until the temperature settles back down.
# Author:      ZauJulio
# License:     MIT
# =================================================================================

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

CONFIG_FILE = "/etc/cpu-thermal-guard.conf"
LOG = logging.getLogger("cpu-thermal-guard")


# --- [ Configuration ] -----------------------------------------------------------


@dataclass
class Config:
    threshold: float = 87.5       # arm temperature (C)
    variance: float = 2.5         # tolerance band up/down (C)
    sustain_seconds: float = 10.0  # continuous time in the hot band before acting
    poll_interval: float = 2.0    # idle poll interval (s)
    burst_seconds: float = 15.0   # initial fan burst (s)
    settle_seconds: float = 15.0  # cool-down wait before re-check (s)
    escalation_seconds: float = 30.0  # added to burst each escalation cycle (s)
    nbfc_bin: str = "nbfc"
    temp_input: str = ""          # override sensor path (mostly for testing)
    log_level: str = "INFO"

    @property
    def release(self) -> float:
        """Lower edge of the hot band; below this we disarm/stop."""
        return self.threshold - self.variance


# Maps config keys to (attribute, caster). Keeps the .conf shell-sourceable.
_FIELDS = {
    "THRESHOLD": ("threshold", float),
    "VARIANCE": ("variance", float),
    "SUSTAIN_SECONDS": ("sustain_seconds", float),
    "POLL_INTERVAL": ("poll_interval", float),
    "BURST_SECONDS": ("burst_seconds", float),
    "SETTLE_SECONDS": ("settle_seconds", float),
    "ESCALATION_SECONDS": ("escalation_seconds", float),
    "NBFC_BIN": ("nbfc_bin", str),
    "TEMP_INPUT": ("temp_input", str),
    "LOG_LEVEL": ("log_level", str),
}


def load_config(path: str) -> Config:
    """Parse a lightweight KEY=VALUE file. Env vars take precedence."""
    cfg = Config()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                field = _FIELDS.get(key.strip())
                if field:
                    attr, cast = field
                    setattr(cfg, attr, cast(value.strip().strip('"').strip("'")))

    # Environment overrides (useful for the systemd unit and quick tests).
    for key, (attr, cast) in _FIELDS.items():
        if key in os.environ:
            setattr(cfg, attr, cast(os.environ[key]))
    return cfg


# --- [ Sensor discovery ] --------------------------------------------------------


def discover_temp_input(override: str = "") -> str:
    """Resolve the CPU package temperature sysfs input path once."""
    if override:
        return override

    # Preferred: coretemp "Package id 0".
    hwmon_root = "/sys/class/hwmon"
    if os.path.isdir(hwmon_root):
        for hw in sorted(os.listdir(hwmon_root)):
            base = os.path.join(hwmon_root, hw)
            try:
                name = open(os.path.join(base, "name")).read().strip()
            except OSError:
                continue
            if name != "coretemp":
                continue
            for entry in sorted(os.listdir(base)):
                if entry.startswith("temp") and entry.endswith("_label"):
                    label = open(os.path.join(base, entry)).read().strip()
                    if label == "Package id 0":
                        return os.path.join(base, entry.replace("_label", "_input"))

    # Fallback: x86_pkg_temp thermal zone.
    thermal_root = "/sys/class/thermal"
    if os.path.isdir(thermal_root):
        for zone in sorted(os.listdir(thermal_root)):
            type_path = os.path.join(thermal_root, zone, "type")
            try:
                if open(type_path).read().strip() == "x86_pkg_temp":
                    return os.path.join(thermal_root, zone, "temp")
            except OSError:
                continue

    raise RuntimeError("No CPU package temperature sensor found")


def read_temp(path: str) -> float:
    """Return the current temperature in Celsius (millidegrees / 1000)."""
    with open(path) as fh:
        return int(fh.read().strip()) / 1000.0


# --- [ Guard ] -------------------------------------------------------------------


class Guard:
    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.temp_input = discover_temp_input(cfg.temp_input)
        self._running = True
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

    def _stop(self, *_args: object) -> None:
        self._running = False

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep so SIGTERM exits promptly."""
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(min(0.5, deadline - time.monotonic()))

    def _nbfc(self, *args: str) -> None:
        if self.dry_run:
            LOG.info("dry-run: nbfc %s", " ".join(args))
            return
        try:
            subprocess.run(
                [self.cfg.nbfc_bin, *args],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            LOG.error("nbfc binary not found: %s", self.cfg.nbfc_bin)

    def fan_on(self) -> None:
        self._nbfc("start")

    def fan_off(self) -> None:
        self._nbfc("stop")

    def pulse_cycle(self) -> None:
        """Escalating burst loop: burst -> settle -> re-check, +escalation each round."""
        cfg = self.cfg
        duration = cfg.burst_seconds
        while self._running:
            LOG.warning("temp=%.1fC -> fan ON for %.0fs", read_temp(self.temp_input), duration)
            self.fan_on()
            self._sleep(duration)
            self.fan_off()
            self._sleep(cfg.settle_seconds)
            if not self._running:
                break
            current = read_temp(self.temp_input)
            if current < cfg.release:
                LOG.info("temp=%.1fC -> below release %.1fC, stopping", current, cfg.release)
                break
            duration += cfg.escalation_seconds

    def run(self) -> None:
        cfg = self.cfg
        LOG.info(
            "started: sensor=%s threshold=%.1fC release=%.1fC sustain=%.0fs poll=%.0fs",
            self.temp_input, cfg.threshold, cfg.release, cfg.sustain_seconds, cfg.poll_interval,
        )
        hot_since: float | None = None
        try:
            while self._running:
                temp = read_temp(self.temp_input)

                if temp >= cfg.release:
                    # Inside the tolerance band: arm only once threshold is crossed.
                    if temp >= cfg.threshold and hot_since is None:
                        hot_since = time.monotonic()
                        LOG.info("temp=%.1fC >= %.1fC -> arming sustain timer", temp, cfg.threshold)
                    if hot_since is not None and time.monotonic() - hot_since >= cfg.sustain_seconds:
                        LOG.warning("sustained hot for %.0fs -> activating", cfg.sustain_seconds)
                        self.pulse_cycle()
                        hot_since = None
                else:
                    if hot_since is not None:
                        LOG.info("temp=%.1fC < release %.1fC -> disarming", temp, cfg.release)
                    hot_since = None

                self._sleep(cfg.poll_interval)
        finally:
            self.fan_off()
            LOG.info("stopped")


# --- [ CLI ] ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sustain-gated NBFC fan pulser")
    parser.add_argument("-c", "--config", default=CONFIG_FILE, help="config file path")
    parser.add_argument("--dry-run", action="store_true", help="log nbfc calls without executing")
    parser.add_argument("--check", action="store_true", help="print current temp + sensor and exit")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if args.check:
        path = discover_temp_input(cfg.temp_input)
        print(f"sensor: {path}")
        print(f"temp:   {read_temp(path):.1f}C")
        print(f"band:   arm>={cfg.threshold}C  release<{cfg.release}C")
        return 0

    try:
        Guard(cfg, dry_run=args.dry_run).run()
    except RuntimeError as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
