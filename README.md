# netshell-mtu

Smart path-MTU optimizer for Linux servers and VPN nodes. Finds the largest packet each link can carry without fragmentation, applies it, and keeps it healthy with a background autopilot.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/NetShell-IR/netshell-mtu/main/netshell-mtu.py -o netshell-mtu.py
sudo python3 netshell-mtu.py
```

The first run offers to optimize everything and enable autopilot. After that the `netshell-mtu` command is available system-wide.

## Features

- Binary-search path-MTU discovery (about 8 probes instead of dozens), bound to each interface with `ping -I`
- IPv4 and IPv6, multiple probe targets, retries against packet loss
- Tunnel-aware: WireGuard, GRE, VXLAN, GENEVE, IPIP, SIT are calculated from the uplink MTU
- Autopilot via systemd timer (cron fallback): cheap health check every few minutes, full re-scan only when packets stop passing or on schedule, re-applies after reboot
- Per-interface modes: `auto`, `pinned`, `ignore`; docker/veth/lo are never touched
- Automatic rollback of temporary changes, even on Ctrl+C or SIGTERM
- Optional TCP MSS clamping for routed/VPN traffic
- Restore original MTUs, full uninstall, log with rotation
- Auto-installs dependencies on apt, dnf, yum, apk, pacman, zypper
- Migrates settings from v1 and removes its old cron job
- Pure Python standard library, no pip packages

## Commands

```
netshell-mtu                  interactive dashboard
netshell-mtu auto [iface...]  discover and apply
netshell-mtu scan [iface...]  discover only
netshell-mtu set eth0 1450    pin a fixed MTU
netshell-mtu mode wg0 ignore  auto | ignore
netshell-mtu status
netshell-mtu install          enable autopilot
netshell-mtu restore
netshell-mtu log
netshell-mtu uninstall [-y]
```

Config: `/etc/netshell-mtu/config.json` · Log: `/var/log/netshell-mtu.log`

## Requirements

Linux with Python 3.7+ and root access. Works on Ubuntu, Debian, CentOS/RHEL, Fedora, Alpine, Arch and openSUSE.

## Contributing

Bug reports and pull requests are welcome at [github.com/netshell/netshell-mtu](https://github.com/netshell/netshell-mtu/issues).

## License

MIT
