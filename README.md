# CPU Thermal Guard

Lightweight, dependency-free (Python stdlib) systemd daemon that pulses the fan
via **NBFC** — but only when the CPU stays genuinely hot. Transient spikes are
ignored; the fan kicks in just when temperature is sustained inside a hot band.

## How it works

A hysteresis/sustain state machine, evaluated every `POLL_INTERVAL` seconds:

| Stage | Condition | Action |
|-------|-----------|--------|
| Arm | `temp >= THRESHOLD` | start the sustain timer |
| Hold | `temp >= THRESHOLD - VARIANCE` | keep the timer (tolerate in-band dips) |
| Disarm | `temp < THRESHOLD - VARIANCE` | reset the timer |
| Activate | armed for `SUSTAIN_SECONDS` | run the escalating burst cycle |

The burst cycle mirrors a manual response: `BURST_SECONDS` fan ON, `SETTLE_SECONDS`
OFF, re-check; if still above the release band, add `ESCALATION_SECONDS` to the
next burst, and so on until the temperature drops below `THRESHOLD - VARIANCE`.

With defaults (`87.5C` / `2.5C` / sustain `10s`): a 90C blip lasting one reading
never triggers; oscillating 86<->89 keeps the timer armed; a steady 88C+ for 10s
activates.

## Install

### Arch / EndeavourOS / CachyOS

```bash
makepkg -si
```

### Debian / RPM

Grab the `.deb` / `.rpm` from the Releases page (built in CI via `fpm`).

## Usage

```bash
sudo systemctl enable --now cpu-thermal-guard.service
journalctl -u cpu-thermal-guard -f      # follow decisions
cpu-thermal-guard --check               # print sensor + current band
cpu-thermal-guard --dry-run             # log nbfc calls without running them
```

Tune everything in `/etc/cpu-thermal-guard.conf`, then
`sudo systemctl restart cpu-thermal-guard`.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `THRESHOLD` | `87.5` | arm temperature (C) |
| `VARIANCE` | `2.5` | tolerance band up/down (C) |
| `SUSTAIN_SECONDS` | `10` | time in band before activating |
| `POLL_INTERVAL` | `2` | idle poll interval (s) |
| `BURST_SECONDS` | `15` | initial fan burst (s) |
| `SETTLE_SECONDS` | `15` | cool-down before re-check (s) |
| `ESCALATION_SECONDS` | `30` | added to burst each cycle (s) |
| `NBFC_BIN` | `nbfc` | fan-control binary |
| `TEMP_INPUT` | auto | sensor path override |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |

## Notes

- The daemon runs as root, so it calls `nbfc` without `sudo`.
- If you keep `nbfc-linux`'s own service running, it will fight these manual
  start/stop calls — disable it (`systemctl disable --now nbfc_service`) or switch
  `fan_on`/`fan_off` to `nbfc set -s 100` / `nbfc set -a` for a fixed blast.
- Auto-detects `coretemp` "Package id 0", falling back to the `x86_pkg_temp`
  thermal zone.

## License

MIT
