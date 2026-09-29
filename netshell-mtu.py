#!/usr/bin/env python3
import argparse
import fcntl
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler

APP = "netshell-mtu"
VERSION = "2.0.0"
REPO = "https://github.com/netshell/netshell-mtu"
RAW_URL = "https://raw.githubusercontent.com/netshell/netshell-mtu/main/netshell-mtu.py"
SYS_NET = "/sys/class/net"
CONF_DIR = "/etc/netshell-mtu"
CONF_FILE = os.path.join(CONF_DIR, "config.json")
STATE_FILE = os.path.join(CONF_DIR, "state.json")
LOG_FILE = "/var/log/netshell-mtu.log"
LOCK_FILE = "/run/netshell-mtu.lock"
BIN_PATH = "/usr/local/bin/netshell-mtu"
UNIT_DIR = "/etc/systemd/system"
SERVICE_FILE = os.path.join(UNIT_DIR, APP + ".service")
TIMER_FILE = os.path.join(UNIT_DIR, APP + ".timer")
CRON_FILE = "/etc/cron.d/netshell-mtu"

HIDDEN_PREFIX = ("veth", "docker", "br-", "virbr", "vmnet", "cni", "flannel", "cali", "kube", "vnet", "tap", "fwbr", "fwpr", "fwln", "lxc")
HIDDEN_KINDS = ("loopback", "veth", "dummy", "ifb", "nlmon")
KNOWN_KINDS = ("wireguard", "vlan", "bridge", "bond", "team", "tun", "vxlan", "gretap", "ip6gretap", "gre", "ip6gre", "ipip", "sit", "ip6tnl", "geneve", "veth", "macvlan", "ipvlan", "l2tp", "dummy", "vrf")
TUNNEL_OVERHEAD = {"wireguard": 80, "gre": 24, "gretap": 38, "ip6gre": 48, "ip6gretap": 62, "ipip": 20, "sit": 20, "ip6tnl": 40, "vxlan": 50, "geneve": 50}
OVERHEAD = {4: 28, 6: 48}
MODES = ("auto", "manual", "ignore")

DEFAULTS = {
    "targets4": ["1.1.1.1", "8.8.8.8", "9.9.9.9"],
    "targets6": ["2606:4700:4700::1111", "2001:4860:4860::8888"],
    "family": "auto",
    "floor": 1280,
    "ceiling": 1500,
    "margin": 0,
    "tries": 3,
    "timeout": 1,
    "interval": 5,
    "full_scan_hours": 12,
    "mss_clamp": False,
    "interfaces": {},
}
LIMITS = {"floor": (576, 9000), "ceiling": (576, 9000), "margin": (0, 200), "tries": (1, 10), "timeout": (1, 10), "interval": (1, 59), "full_scan_hours": (1, 168)}

PKG = {
    "apt-get": (["apt-get", "update", "-qq"], ["apt-get", "install", "-y", "-qq"], {"ping": "iputils-ping", "ip": "iproute2"}),
    "dnf": (None, ["dnf", "install", "-y", "-q"], {"ping": "iputils", "ip": "iproute"}),
    "yum": (None, ["yum", "install", "-y", "-q"], {"ping": "iputils", "ip": "iproute"}),
    "apk": (None, ["apk", "add", "--no-cache"], {"ping": "iputils", "ip": "iproute2"}),
    "pacman": (None, ["pacman", "-S", "--noconfirm", "--needed"], {"ping": "iputils", "ip": "iproute2"}),
    "zypper": (None, ["zypper", "-n", "install"], {"ping": "iputils", "ip": "iproute2"}),
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
UTF = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "").startswith("utf")
SYM = {"ok": "✔", "warn": "▲", "err": "✘", "info": "•", "ask": "›", "up": "●", "down": "○", "h": "─", "bar": "■", "empty": "·", "arrow": "→",
       "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "v": "│"}
if not UTF:
    SYM = {"ok": "+", "warn": "!", "err": "x", "info": "*", "ask": ">", "up": "*", "down": "o", "h": "-", "bar": "#", "empty": ".", "arrow": "->",
           "tl": "+", "tr": "+", "bl": "+", "br": "+", "v": "|"}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if UTF else "|/-\\"
BOLD, DIM, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = "1", "2", "31", "32", "33", "34", "35", "36"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

QUIET = False
PROGRESS_ON = False
SPIN_POS = 0
log = logging.getLogger(APP)


def paint(text, *codes):
    if not USE_COLOR or not codes:
        return str(text)
    return "\x1b[" + ";".join(codes) + "m" + str(text) + "\x1b[0m"


def vlen(text):
    return len(ANSI_RE.sub("", str(text)))


def clear_line():
    global PROGRESS_ON
    if PROGRESS_ON:
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()
        PROGRESS_ON = False


def say(sym, color, msg, level):
    getattr(log, level)(ANSI_RE.sub("", str(msg)))
    if not QUIET:
        clear_line()
        print("  " + paint(sym, BOLD, color) + " " + str(msg))


def ok(msg):
    say(SYM["ok"], GREEN, msg, "info")


def info(msg):
    say(SYM["info"], CYAN, msg, "info")


def warn(msg):
    say(SYM["warn"], YELLOW, msg, "warning")


def fail(msg):
    say(SYM["err"], RED, msg, "error")


def note(msg):
    if not QUIET:
        clear_line()
        print("  " + paint(SYM["warn"] + " " + msg, YELLOW))


def progress(name, text, step=None, total=None):
    global PROGRESS_ON, SPIN_POS
    if QUIET or not sys.stdout.isatty():
        return
    frame = SPIN[SPIN_POS % len(SPIN)]
    SPIN_POS += 1
    bar = ""
    if step is not None and total:
        width = 18
        filled = min(width, int(width * step / total))
        bar = "  " + paint(SYM["bar"] * filled, CYAN) + paint(SYM["empty"] * (width - filled), DIM)
    sys.stdout.write("\r\x1b[2K  " + paint(frame, CYAN) + " " + paint(name, BOLD) + "  " + text + bar)
    sys.stdout.flush()
    PROGRESS_ON = True


def setup_logging(to_stdout):
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(handler)
    except OSError:
        pass
    if to_stdout:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(stream)
    if not log.handlers:
        log.addHandler(logging.NullHandler())


def run(cmd, timeout=30, env=None, inp=None):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, env=env, input=inp)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def sysread(path, default=""):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return default


def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def valid_ip(text, family):
    try:
        return ipaddress.ip_address(text).version == family
    except ValueError:
        return False


def load_json(path, default):
    try:
        with open(path) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_text(path, text, mode=0o644):
    with open(path, "w") as handle:
        handle.write(text)
    os.chmod(path, mode)


def remove_quiet(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def load_config():
    raw = load_json(CONF_FILE, {})
    cfg = json.loads(json.dumps(DEFAULTS))
    for key, value in raw.items():
        if key in cfg:
            cfg[key] = value
    for key, (low, high) in LIMITS.items():
        cfg[key] = min(high, max(low, to_int(cfg.get(key), DEFAULTS[key])))
    if cfg["floor"] >= cfg["ceiling"]:
        cfg["floor"], cfg["ceiling"] = DEFAULTS["floor"], DEFAULTS["ceiling"]
    for fam in (4, 6):
        key = "targets%d" % fam
        items = cfg[key] if isinstance(cfg[key], list) else []
        cfg[key] = [t for t in items if isinstance(t, str) and valid_ip(t, fam)] or list(DEFAULTS[key])
    cfg["family"] = str(cfg["family"])
    if cfg["family"] not in ("auto", "4", "6"):
        cfg["family"] = "auto"
    cfg["mss_clamp"] = bool(cfg["mss_clamp"])
    if not isinstance(cfg["interfaces"], dict):
        cfg["interfaces"] = {}
    cfg["interfaces"] = {k: v for k, v in cfg["interfaces"].items() if isinstance(v, dict)}
    return cfg


def save_config(cfg):
    save_json(CONF_FILE, cfg)


class Lock:
    def __init__(self, wait=True):
        self.wait = wait
        self.fd = None

    def __enter__(self):
        path = LOCK_FILE if os.path.isdir(os.path.dirname(LOCK_FILE)) else "/tmp/netshell-mtu.lock"
        self.fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        return False


class Pinger:
    def __init__(self):
        self.bin = None
        self.family_flag = True
        self.bin6 = None

    def _works(self, base):
        res = run(base + ["-M", "do", "-c", "1", "-W", "1", "-s", "16", "127.0.0.1"], timeout=5)
        text = (res.stdout + res.stderr).lower()
        return res.returncode != 127 and not any(w in text for w in ("invalid", "unrecognized", "illegal", "usage", "busybox", "unknown option"))

    def usable(self):
        self.bin = shutil.which("ping")
        if not self.bin:
            return False
        if self._works([self.bin, "-4"]):
            self.family_flag = True
            return True
        if self._works([self.bin]):
            self.family_flag = False
            self.bin6 = shutil.which("ping6")
            return True
        return False

    def command(self, fam, size, iface, target, timeout):
        if self.family_flag:
            base = [self.bin, "-6" if fam == 6 else "-4"]
        elif fam == 6:
            base = [self.bin6 or self.bin]
        else:
            base = [self.bin]
        return base + ["-M", "do", "-c", "1", "-W", str(timeout), "-s", str(size), "-I", iface, target]


PING = Pinger()


def ensure_tools():
    need = []
    if not shutil.which("ip"):
        need.append("ip")
    if not PING.usable():
        need.append("ping")
    if not need:
        return True
    manager = next((m for m in PKG if shutil.which(m)), None)
    if not manager:
        fail("Missing tools: %s. Install iproute2 and iputils manually." % ", ".join(need))
        return False
    update, install, names = PKG[manager]
    packages = [names[n] for n in need]
    info("Installing %s with %s ..." % (" ".join(packages), manager))
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    if update:
        run(update, timeout=300, env=env)
    res = run(install + packages, timeout=900, env=env)
    if shutil.which("ip") and PING.usable():
        ok("Dependencies ready")
        return True
    fail("Could not install dependencies: %s" % (res.stderr.strip().splitlines() or ["unknown error"])[-1])
    return False


def ensure_root():
    if os.geteuid() == 0:
        return
    sudo = shutil.which("sudo")
    if sudo:
        os.execvp(sudo, [sudo, sys.executable, os.path.realpath(__file__)] + sys.argv[1:])
    print("This tool needs root. Run it with sudo or as root.")
    sys.exit(1)


def classify(name, info_kind, dev_type):
    if name == "lo" or dev_type == "772":
        return "loopback"
    if info_kind:
        return info_kind
    if os.path.isdir("%s/%s/wireless" % (SYS_NET, name)) or os.path.isdir("%s/%s/phy80211" % (SYS_NET, name)):
        return "wifi"
    if dev_type == "512":
        return "ppp"
    if dev_type == "65534":
        return "tun"
    if os.path.exists("%s/%s/device" % (SYS_NET, name)):
        return "physical"
    return "virtual"


def _ip_json():
    res = run(["ip", "-j", "-d", "addr", "show"])
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return None
    return [d for d in data if isinstance(d, dict) and d.get("ifname")] if isinstance(data, list) else None


def _ip_text():
    out = []
    try:
        names = sorted(os.listdir(SYS_NET))
    except OSError:
        return out
    kinds = "|".join(sorted(KNOWN_KINDS, key=len, reverse=True))
    for name in names:
        base = "%s/%s" % (SYS_NET, name)
        flags = sysread(base + "/flags", "0x0")
        try:
            up = int(flags, 16) & 1
        except ValueError:
            up = 0
        detail = run(["ip", "-d", "link", "show", "dev", name]).stdout
        limits = re.search(r"minmtu (\d+) maxmtu (\d+)", detail)
        kind = re.search(r"\n\s+(%s)\b(?!_)" % kinds, detail)
        master = re.search(r" master (\S+)", detail)
        addrs = []
        for line in run(["ip", "-o", "addr", "show", "dev", name]).stdout.splitlines():
            parts = line.split()
            if len(parts) > 3 and parts[2] in ("inet", "inet6"):
                scope = parts[parts.index("scope") + 1] if "scope" in parts[:-1] else ""
                addrs.append({"family": parts[2], "scope": scope})
        out.append({
            "ifname": name,
            "mtu": sysread(base + "/mtu", "0"),
            "operstate": sysread(base + "/operstate", "unknown"),
            "flags": ["UP"] if up else [],
            "min_mtu": limits.group(1) if limits else 68,
            "max_mtu": limits.group(2) if limits else 0,
            "linkinfo": {"info_kind": kind.group(1)} if kind else {},
            "addr_info": addrs,
            "master": master.group(1) if master else None,
        })
    return out


def get_interfaces(show_hidden=False):
    raw = _ip_json()
    if raw is None:
        raw = _ip_text()
    result = []
    for d in raw:
        name = d["ifname"]
        link = d.get("linkinfo") or {}
        kind = classify(name, link.get("info_kind", ""), sysread("%s/%s/type" % (SYS_NET, name)))
        addrs = d.get("addr_info") or []
        hidden = kind in HIDDEN_KINDS or name.startswith(HIDDEN_PREFIX)
        if hidden and not show_hidden:
            continue
        result.append({
            "name": name,
            "mtu": to_int(d.get("mtu")),
            "up": "UP" in (d.get("flags") or []),
            "kind": kind,
            "min": to_int(d.get("min_mtu"), 68),
            "max": to_int(d.get("max_mtu"), 0),
            "v4": any(a.get("family") == "inet" and a.get("scope") == "global" for a in addrs),
            "v6": any(a.get("family") == "inet6" and a.get("scope") == "global" for a in addrs),
            "hidden": hidden,
            "master": d.get("master"),
        })
    return result


def get_mtu(name):
    return to_int(sysread("%s/%s/mtu" % (SYS_NET, name)), 0)


def set_mtu(name, mtu):
    res = run(["ip", "link", "set", "dev", name, "mtu", str(mtu)])
    return res.returncode == 0, (res.stderr or res.stdout).strip()


def default_iface(fam):
    match = re.search(r"\bdev (\S+)", run(["ip", "-%d" % fam, "route", "show", "default"]).stdout)
    return match.group(1) if match else None


def iface_mode(cfg, dev):
    mode = cfg["interfaces"].get(dev["name"], {}).get("mode")
    if mode in MODES:
        return mode
    if dev["hidden"] or dev["master"]:
        return "ignore"
    return "auto"


def mtu_bounds(dev):
    low = max(68, dev["min"] or 68)
    high = dev["max"] if 0 < dev["max"] < 65536 else 65535
    return low, high


class Prober:
    def __init__(self, cfg):
        self.cfg = cfg

    def families(self, dev):
        pref = self.cfg["family"]
        return [f for f in (4, 6) if dev["v%d" % f] and pref in ("auto", str(f))]

    def floor(self, dev, fam):
        return max(self.cfg["floor"], dev["min"] or 0, 1280 if fam == 6 else 68)

    def ping(self, fam, iface, target, mtu):
        size = mtu - OVERHEAD[fam]
        if size < 0:
            return False
        tries = self.cfg["tries"]
        timeout = self.cfg["timeout"]
        for attempt in range(tries):
            if run(PING.command(fam, size, iface, target, timeout), timeout=timeout + 5).returncode == 0:
                return True
            if attempt + 1 < tries:
                time.sleep(0.15)
        return False

    def reachable(self, fam, iface, limit=1):
        found = []
        for target in self.cfg["targets%d" % fam]:
            progress(iface, "IPv%d  reaching %s" % (fam, target))
            if self.ping(fam, iface, target, OVERHEAD[fam] + 56):
                found.append(target)
                if len(found) >= limit:
                    break
        return found

    def search(self, fam, iface, target, low, high):
        progress(iface, "IPv%d  testing %d via %s" % (fam, low, target))
        if not self.ping(fam, iface, target, low):
            return None
        total = max(1, (high - low).bit_length())
        step = 0
        while low < high:
            mid = (low + high + 1) // 2
            step += 1
            progress(iface, "IPv%d  testing %s" % (fam, paint(str(mid), BOLD)), step, total)
            if self.ping(fam, iface, target, mid):
                low = mid
            else:
                high = mid - 1
        return low

    def health(self, dev, mtu):
        reached = False
        for fam in self.families(dev):
            targets = self.reachable(fam, dev["name"])
            if not targets:
                continue
            reached = True
            progress(dev["name"], "IPv%d  verifying %d" % (fam, mtu))
            if not self.ping(fam, dev["name"], targets[0], mtu):
                return "degraded"
        return "ok" if reached else "offline"

    def discover(self, dev):
        name = dev["name"]
        fams = self.families(dev)
        if not fams:
            return None, "no global IP address to probe from"
        ceiling = self.cfg["ceiling"]
        if dev["max"] > 0:
            ceiling = min(ceiling, dev["max"])
        reasons = []
        plan = []
        for fam in fams:
            targets = self.reachable(fam, name, limit=2)
            if targets:
                plan.append((fam, targets))
            else:
                reasons.append("no IPv%d target answers through %s" % (fam, name))
        if not plan:
            clear_line()
            return None, "; ".join(reasons)
        original = get_mtu(name)
        raised = False
        results = []
        try:
            if original < ceiling:
                success, _ = set_mtu(name, ceiling)
                if success:
                    raised = True
                    time.sleep(0.4)
                else:
                    ceiling = original
            for fam, targets in plan:
                floor = min(self.floor(dev, fam), ceiling)
                best = None
                for target in targets:
                    found = self.search(fam, name, target, floor if best is None else best, ceiling)
                    if found is not None:
                        best = found if best is None else max(best, found)
                    if best == ceiling:
                        break
                if best is None:
                    reasons.append("IPv%d path MTU is below %d" % (fam, floor))
                else:
                    results.append(best)
        finally:
            if raised:
                set_mtu(name, original)
            clear_line()
        if not results:
            return None, "; ".join(reasons) or "discovery failed"
        return min(results), None


def pick_underlay(devs):
    by_name = {d["name"]: d for d in devs}
    for fam in (4, 6):
        name = default_iface(fam)
        dev = by_name.get(name)
        if dev and dev["kind"] not in TUNNEL_OVERHEAD and dev["kind"] != "tun":
            return dev
    for dev in devs:
        if dev["kind"] in ("physical", "wifi", "ppp", "bond", "team", "vlan", "bridge", "virtual") and dev["up"] and (dev["v4"] or dev["v6"]) and not dev["master"] and not dev["hidden"]:
            return dev
    return None


def apply_mtu(state, name, before, new, why):
    current = get_mtu(name)
    if new == current:
        ok("%s: %d is already optimal  %s" % (paint(name, BOLD), new, paint(why, DIM)))
        return True
    state.setdefault("original", {}).setdefault(name, before)
    success, err = set_mtu(name, new)
    if success:
        ok("%s: MTU %d %s %s  %s" % (paint(name, BOLD), current, SYM["arrow"], paint(str(new), BOLD, GREEN), paint(why, DIM)))
        return True
    fail("%s: could not set MTU %d (%s)" % (name, new, err or "unknown error"))
    return False


def mss_clamp(enable):
    changed = False
    for tool in ("iptables", "ip6tables"):
        binary = shutil.which(tool)
        if not binary:
            continue

        def rule(op):
            return [binary, "-w", "5", "-t", "mangle", op, "FORWARD", "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN", "-j", "TCPMSS", "--clamp-mss-to-pmtu"]

        exists = run(rule("-C")).returncode == 0
        if enable and not exists:
            changed = run(rule("-A")).returncode == 0 or changed
        elif not enable:
            for _ in range(10):
                if run(rule("-D")).returncode != 0:
                    break
                changed = True
    return changed


def optimize(cfg, state, only=None, watch=False, dry=False):
    devs = get_interfaces(True)
    names = {d["name"] for d in devs}
    for name in only or []:
        if name not in names:
            fail("Interface %s not found" % name)
    prober = Prober(cfg)
    now = time.time()
    records = state.setdefault("ifaces", {})
    targets = [d for d in devs if not only or d["name"] in only]
    targets.sort(key=lambda d: 1 if d["kind"] in TUNNEL_OVERHEAD else 0)
    underlay = pick_underlay(devs)
    found = {}
    summary = []
    for dev in targets:
        name = dev["name"]
        conf = cfg["interfaces"].get(name, {})
        mode = iface_mode(cfg, dev)
        if mode == "ignore" and not only:
            continue
        before = get_mtu(name)
        rec = records.setdefault(name, {})
        if mode == "manual":
            want = to_int(conf.get("mtu"))
            if want and want != before and not dry:
                apply_mtu(state, name, before, want, "pinned value")
            rec.update(checked=now, result="pinned %d" % (want or before))
            summary.append((name, before, want or before, "pinned"))
            continue
        if not dev["up"]:
            info("%s: link is down, skipped" % name)
            summary.append((name, before, before, "link down"))
            continue
        if dev["kind"] in TUNNEL_OVERHEAD:
            if not underlay:
                warn("%s: no underlying uplink found to calculate from" % name)
                continue
            base = found.get(underlay["name"], get_mtu(underlay["name"]))
            overhead = to_int(conf.get("overhead"), TUNNEL_OVERHEAD[dev["kind"]])
            new = base - overhead
            low, high = mtu_bounds(dev)
            new = min(high, new)
            if new < max(low, 576):
                warn("%s: calculated MTU %d is too small, skipped" % (name, new))
                continue
            why = "%s %d - %s overhead %d" % (underlay["name"], base, dev["kind"], overhead)
            if not dry:
                apply_mtu(state, name, before, new, why)
            found[name] = new
            rec.update(checked=now, scanned=now, mtu=new, result="calculated %d" % new)
            summary.append((name, before, new, why))
            continue
        if watch and now - rec.get("scanned", 0) < cfg["full_scan_hours"] * 3600:
            health = prober.health(dev, before)
            clear_line()
            rec["checked"] = now
            if health == "ok":
                rec["result"] = "healthy %d" % before
                found[name] = before
                summary.append((name, before, before, "healthy"))
                continue
            if health == "offline":
                rec["result"] = "offline"
                warn("%s: no probe target reachable, MTU left at %d" % (name, before))
                summary.append((name, before, before, "offline"))
                continue
            warn("%s: %d-byte packets stopped passing, re-discovering" % (name, before))
        best, reason = prober.discover(dev)
        rec["checked"] = now
        if best is None:
            rec["result"] = "failed"
            warn("%s: %s" % (name, reason))
            summary.append((name, before, before, reason))
            continue
        new = max(prober.floor(dev, 4), best - cfg["margin"]) if cfg["margin"] else best
        why = "path MTU %d" % best + (" - margin %d" % cfg["margin"] if cfg["margin"] else "")
        if dry:
            info("%s: path MTU is %d (dry run, nothing changed)" % (paint(name, BOLD), best))
        else:
            apply_mtu(state, name, before, new, why)
        found[name] = new
        rec.update(scanned=now, mtu=new, result="optimal %d" % new)
        summary.append((name, before, new, why))
    if cfg["mss_clamp"] and not dry and mss_clamp(True):
        ok("TCP MSS clamping rule added")
    state["last_run"] = now
    save_json(STATE_FILE, state)
    return summary


def has_systemd():
    return os.path.isdir("/run/systemd/system") and shutil.which("systemctl") is not None


def service_status():
    if has_systemd() and os.path.exists(TIMER_FILE):
        return run(["systemctl", "is-active", APP + ".timer"]).stdout.strip() == "active", "systemd"
    if os.path.exists(CRON_FILE):
        return True, "cron"
    return False, None


def install_service(cfg):
    src = os.path.realpath(__file__)
    if os.path.islink(BIN_PATH):
        os.remove(BIN_PATH)
    if not (os.path.exists(BIN_PATH) and os.path.samefile(src, BIN_PATH)):
        shutil.copyfile(src, BIN_PATH)
    os.chmod(BIN_PATH, 0o755)
    python = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    if has_systemd():
        write_text(SERVICE_FILE, "\n".join([
            "[Unit]",
            "Description=netshell MTU optimizer",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=oneshot",
            "ExecStart=%s %s watch" % (python, BIN_PATH),
            "Nice=10",
            "TimeoutStartSec=600",
            "",
        ]))
        write_text(TIMER_FILE, "\n".join([
            "[Unit]",
            "Description=netshell MTU optimizer autopilot",
            "",
            "[Timer]",
            "OnBootSec=45s",
            "OnUnitActiveSec=%dmin" % cfg["interval"],
            "AccuracySec=15s",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]))
        remove_quiet(CRON_FILE)
        run(["systemctl", "daemon-reload"])
        res = run(["systemctl", "enable", "--now", APP + ".timer"])
        if res.returncode != 0:
            raise OSError(res.stderr.strip() or "systemctl failed")
        run(["systemctl", "restart", APP + ".timer"])
        return "systemd timer"
    if not os.path.isdir(os.path.dirname(CRON_FILE)):
        raise OSError("neither systemd nor /etc/cron.d is available")
    write_text(CRON_FILE, "\n".join([
        "SHELL=/bin/sh",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "*/%d * * * * root %s %s watch >/dev/null 2>&1" % (cfg["interval"], python, BIN_PATH),
        "@reboot root sleep 45 && %s %s watch >/dev/null 2>&1" % (python, BIN_PATH),
        "",
    ]))
    return "cron"


def remove_service():
    if has_systemd():
        run(["systemctl", "disable", "--now", APP + ".timer"])
    removed = [remove_quiet(p) for p in (SERVICE_FILE, TIMER_FILE, CRON_FILE)]
    if has_systemd():
        run(["systemctl", "daemon-reload"])
    return any(removed)


def legacy_cleanup(cfg, state):
    users = ["root"]
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        users.append(sudo_user)
    if shutil.which("crontab"):
        for user in users:
            res = run(["crontab", "-l", "-u", user])
            if res.returncode != 0:
                continue
            lines = res.stdout.splitlines()
            keep = [l for l in lines if not ("--no-interact" in l and "mtu" in l.lower())]
            if len(keep) != len(lines):
                run(["crontab", "-u", user, "-"], inp="\n".join(keep).strip() + "\n")
                ok("Removed the old v1 cron job for user %s (it reset MTU every 5 minutes)" % user)
    for user in users:
        home = os.path.expanduser("~" + user)
        manual = os.path.join(home, ".mtu_optimizer_manual.json")
        data = load_json(manual, {})
        if data.get("interface") and to_int(data.get("mtu")):
            cfg["interfaces"].setdefault(data["interface"], {"mode": "manual", "mtu": to_int(data["mtu"])})
            info("Migrated pinned MTU %s for %s from v1" % (data["mtu"], data["interface"]))
            save_config(cfg)
        for old in (".mtu_optimizer_config.json", ".mtu_optimizer_manual.json"):
            remove_quiet(os.path.join(home, old))
    state["legacy_done"] = True
    save_json(STATE_FILE, state)


def restore_all(cfg, state, pause_auto=True):
    original = state.get("original", {})
    if not original:
        info("Nothing to restore, no MTU was changed by this tool")
        return
    names = {d["name"] for d in get_interfaces(True)}
    for name, mtu in original.items():
        if name not in names:
            warn("%s no longer exists" % name)
            continue
        success, err = set_mtu(name, to_int(mtu))
        if success:
            ok("%s restored to %s" % (name, mtu))
        else:
            fail("%s: %s" % (name, err))
        if pause_auto:
            cfg["interfaces"].setdefault(name, {})["mode"] = "ignore"
    if pause_auto:
        save_config(cfg)
        info("Restored interfaces are set to 'ignore' so autopilot leaves them alone")


def set_manual(cfg, state, name, mtu):
    devs = {d["name"]: d for d in get_interfaces(True)}
    dev = devs.get(name)
    if not dev:
        fail("Interface %s not found" % name)
        return False
    low, high = mtu_bounds(dev)
    if not low <= mtu <= high:
        fail("%s accepts MTU between %d and %d" % (name, low, high))
        return False
    if not apply_mtu(state, name, dev["mtu"], mtu, "pinned by you"):
        return False
    cfg["interfaces"][name] = {"mode": "manual", "mtu": mtu}
    save_config(cfg)
    save_json(STATE_FILE, state)
    return True


def ago(ts):
    if not ts:
        return "never"
    sec = max(0, int(time.time() - ts))
    if sec < 60:
        return "%ds ago" % sec
    if sec < 3600:
        return "%dm ago" % (sec // 60)
    if sec < 86400:
        return "%dh ago" % (sec // 3600)
    return "%dd ago" % (sec // 86400)


def banner():
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[2J\x1b[H")
    lines = [
        paint("netshell", BOLD, BLUE) + paint("  " + SYM["info"] + "  MTU Optimizer", BOLD) + "  " + paint("v" + VERSION, DIM),
        paint("path-MTU discovery  " + SYM["info"] + "  autopilot  " + SYM["info"] + "  zero config", DIM),
        paint(REPO.replace("https://", ""), DIM, CYAN),
    ]
    width = max(vlen(l) for l in lines) + 4
    print()
    print("  " + paint(SYM["tl"] + SYM["h"] * width + SYM["tr"], BLUE))
    for line in lines:
        print("  " + paint(SYM["v"], BLUE) + "  " + line + " " * (width - vlen(line) - 2) + paint(SYM["v"], BLUE))
    print("  " + paint(SYM["bl"] + SYM["h"] * width + SYM["br"], BLUE))


def section(title):
    print()
    print("  " + paint(title, BOLD, BLUE))
    print("  " + paint(SYM["h"] * vlen(title), DIM))


def table(headers, rows):
    widths = [vlen(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], vlen(cell))

    def fmt(row):
        return "  " + "   ".join(str(c) + " " * (widths[i] - vlen(c)) for i, c in enumerate(row))

    print(paint(fmt(headers), BOLD))
    print("  " + paint(SYM["h"] * (sum(widths) + 3 * (len(widths) - 1)), DIM))
    for row in rows:
        print(fmt(row))


def ask(prompt, default=None):
    suffix = paint(" [%s]" % default, DIM) if default not in (None, "") else ""
    try:
        value = input("  " + paint(SYM["ask"], BOLD, MAGENTA) + " " + prompt + suffix + ": ").strip()
    except EOFError:
        raise KeyboardInterrupt
    return value or ("" if default is None else str(default))


def ask_int(prompt, default, low, high):
    while True:
        value = ask(prompt, default)
        if value.isdigit() and low <= int(value) <= high:
            return int(value)
        note("Enter a number between %d and %d" % (low, high))


def confirm(prompt, default=False):
    value = ask(prompt + paint(" (%s)" % ("Y/n" if default else "y/N"), DIM)).lower()
    if not value:
        return default
    return value in ("y", "yes")


def menu(items):
    for key, label, hint in items:
        extra = "  " + paint(hint, DIM) if hint else ""
        print("    " + paint("[%s]" % key, BOLD, CYAN) + " " + label + extra)
    print()
    keys = {i[0] for i in items}
    while True:
        choice = ask("Choose")
        if choice in keys:
            return choice
        note("Invalid choice")


def pause():
    if sys.stdin.isatty():
        try:
            input(paint("\n  Press Enter to continue ...", DIM))
        except EOFError:
            pass


def pick_iface(prompt):
    devs = get_interfaces(False)
    if not devs:
        fail("No usable interfaces found")
        return None
    print()
    for i, dev in enumerate(devs, 1):
        state = paint(SYM["up"], GREEN) if dev["up"] else paint(SYM["down"], RED)
        print("    %s %s %-16s %s  MTU %d" % (paint("[%d]" % i, BOLD, CYAN), state, dev["name"], paint("%-10s" % dev["kind"], DIM), dev["mtu"]))
    print("    %s back" % paint("[0]", BOLD, CYAN))
    print()
    choice = ask_int(prompt, None, 0, len(devs))
    return devs[choice - 1] if choice else None


def show_status(cfg, state):
    devs = get_interfaces(True)
    visible = [d for d in devs if not d["hidden"]]
    records = state.get("ifaces", {})
    rows = []
    for dev in visible:
        mode = iface_mode(cfg, dev)
        if dev["master"]:
            mode_text = paint("slave of " + dev["master"], DIM)
        elif mode == "auto":
            mode_text = paint("auto", GREEN)
        elif mode == "manual":
            mode_text = paint("pinned", MAGENTA)
        else:
            mode_text = paint("ignore", DIM)
        link = paint(SYM["up"] + " up", GREEN) if dev["up"] else paint(SYM["down"] + " down", RED)
        rec = records.get(dev["name"])
        last = "%s %s %s" % (rec.get("result", "-"), paint(SYM["info"], DIM), paint(ago(rec.get("checked")), DIM)) if rec else paint("not scanned yet", DIM)
        rows.append([paint(dev["name"], BOLD), dev["kind"], link, paint(str(dev["mtu"]), BOLD), mode_text, last])
    section("Interfaces")
    if rows:
        table(["Interface", "Type", "Link", "MTU", "Mode", "Last result"], rows)
    else:
        print("  " + paint("No interfaces found", DIM))
    hidden = len(devs) - len(visible)
    if hidden:
        print("  " + paint("+ %d hidden virtual interface(s) (docker, veth, lo ...) are never touched" % hidden, DIM))
    active, how = service_status()
    print()
    if active:
        pilot = paint(SYM["up"] + " on", BOLD, GREEN) + paint("  %s, check every %d min, full re-scan every %d h" % (how, cfg["interval"], cfg["full_scan_hours"]), DIM)
    elif how:
        pilot = paint(SYM["down"] + " installed but inactive", BOLD, YELLOW)
    else:
        pilot = paint(SYM["down"] + " off", BOLD, RED) + paint("  enable it from the menu", DIM)
    print("  %-12s %s" % ("Autopilot", pilot))
    print("  %-12s %s" % ("MSS clamp", paint("on", GREEN) if cfg["mss_clamp"] else paint("off", DIM)))
    print("  %-12s %s" % ("Last run", ago(state.get("last_run"))))


def print_summary(rows, started):
    if not rows:
        info("Nothing to do, no interface in auto or pinned mode")
        return
    section("Summary")
    out = []
    for name, before, after, why in rows:
        if after > before:
            after_text = paint(str(after) + " " + "\u2191" if UTF else str(after) + " +", GREEN)
        elif after < before:
            after_text = paint(str(after) + " " + "\u2193" if UTF else str(after) + " -", YELLOW)
        else:
            after_text = str(after)
        out.append([paint(name, BOLD), str(before), after_text, paint(why, DIM)])
    table(["Interface", "Before", "After", "Details"], out)
    print("\n  " + paint("Finished in %.1fs" % (time.time() - started), DIM))


def do_optimize(cfg, state, only=None, dry=False):
    section("Scanning (dry run)" if dry else "Optimizing")
    started = time.time()
    with Lock():
        rows = optimize(cfg, state, only=only, dry=dry)
    print_summary(rows, started)


def do_install(cfg):
    section("Autopilot")
    try:
        how = install_service(cfg)
    except OSError as exc:
        fail("Could not enable autopilot: %s" % exc)
        return False
    save_config(cfg)
    ok("Autopilot enabled via %s" % how)
    info("Checks every %d min, full re-scan every %d h, re-applies after reboot" % (cfg["interval"], cfg["full_scan_hours"]))
    info("Command installed: %s  (try: %s status)" % (paint(APP, BOLD), APP))
    return True


def edit_targets(cfg, fam):
    key = "targets%d" % fam
    raw = ask("IPv%d targets, comma separated" % fam, ", ".join(cfg[key]))
    items = [t.strip() for t in raw.split(",") if t.strip()]
    bad = [t for t in items if not valid_ip(t, fam)]
    if bad or not items:
        note("Invalid IPv%d address: %s" % (fam, ", ".join(bad) or "empty list"))
        return
    cfg[key] = items


def settings_menu(cfg):
    while True:
        banner()
        section("Settings")
        family = {"auto": "IPv4 + IPv6 (auto)", "4": "IPv4 only", "6": "IPv6 only"}[cfg["family"]]
        choice = menu([
            ("1", "IPv4 probe targets", ", ".join(cfg["targets4"])),
            ("2", "IPv6 probe targets", ", ".join(cfg["targets6"])),
            ("3", "Address family", family),
            ("4", "Search range", "%d - %d" % (cfg["floor"], cfg["ceiling"])),
            ("5", "Safety margin", "%d bytes" % cfg["margin"]),
            ("6", "Probe retries and timeout", "%d x %ds" % (cfg["tries"], cfg["timeout"])),
            ("7", "Autopilot schedule", "check every %d min, re-scan every %d h" % (cfg["interval"], cfg["full_scan_hours"])),
            ("8", "TCP MSS clamping for routed/VPN traffic", "on" if cfg["mss_clamp"] else "off"),
            ("0", "Back", ""),
        ])
        if choice == "0":
            return
        if choice == "1":
            edit_targets(cfg, 4)
        elif choice == "2":
            edit_targets(cfg, 6)
        elif choice == "3":
            pick = menu([("1", "IPv4 + IPv6 (auto)", "recommended"), ("2", "IPv4 only", ""), ("3", "IPv6 only", "")])
            cfg["family"] = {"1": "auto", "2": "4", "3": "6"}[pick]
        elif choice == "4":
            floor = ask_int("Lowest MTU to consider", cfg["floor"], 576, 8999)
            cfg["ceiling"] = ask_int("Highest MTU to consider (1500 normal, 9000 jumbo)", max(cfg["ceiling"], floor + 1), floor + 1, 9000)
            cfg["floor"] = floor
        elif choice == "5":
            cfg["margin"] = ask_int("Bytes to subtract from the discovered MTU", cfg["margin"], 0, 200)
        elif choice == "6":
            cfg["tries"] = ask_int("Attempts per probe (higher = safer on lossy links)", cfg["tries"], 1, 10)
            cfg["timeout"] = ask_int("Seconds to wait per reply", cfg["timeout"], 1, 10)
        elif choice == "7":
            cfg["interval"] = ask_int("Health check interval in minutes", cfg["interval"], 1, 59)
            cfg["full_scan_hours"] = ask_int("Full re-scan every N hours", cfg["full_scan_hours"], 1, 168)
            if service_status()[1]:
                try:
                    install_service(cfg)
                    ok("Autopilot schedule updated")
                except OSError as exc:
                    fail("Could not update schedule: %s" % exc)
        elif choice == "8":
            cfg["mss_clamp"] = not cfg["mss_clamp"]
            mss_clamp(cfg["mss_clamp"])
            ok("MSS clamping %s" % ("enabled" if cfg["mss_clamp"] else "disabled"))
        save_config(cfg)
        if choice in ("7", "8"):
            pause()


def modes_menu(cfg, state):
    dev = pick_iface("Interface to configure")
    if not dev:
        return
    section("Mode for %s" % dev["name"])
    choice = menu([
        ("1", "auto", "autopilot discovers and maintains the best MTU"),
        ("2", "pinned", "always keep a fixed MTU you choose"),
        ("3", "ignore", "never touch this interface"),
        ("0", "back", ""),
    ])
    if choice == "0":
        return
    if choice == "2":
        low, high = mtu_bounds(dev)
        set_manual(cfg, state, dev["name"], ask_int("MTU for %s" % dev["name"], dev["mtu"], low, high))
    else:
        cfg["interfaces"][dev["name"]] = {"mode": "auto" if choice == "1" else "ignore"}
        save_config(cfg)
        ok("%s is now in %s mode" % (dev["name"], cfg["interfaces"][dev["name"]]["mode"]))
    pause()


def show_log(lines=40):
    section("Recent log")
    try:
        with open(LOG_FILE, errors="replace") as handle:
            content = handle.readlines()[-lines:]
    except OSError:
        content = []
    if not content:
        print("  " + paint("Log is empty", DIM))
    for line in content:
        line = line.rstrip()
        color = RED if " ERROR " in line else YELLOW if " WARNING " in line else None
        print("  " + (paint(line, color) if color else line))


def uninstall(cfg, state, assume_yes):
    section("Uninstall")
    if state.get("original") and (assume_yes or confirm("Restore original MTUs first?", True)):
        restore_all(cfg, state, pause_auto=False)
    if remove_service():
        ok("Autopilot removed")
    if mss_clamp(False):
        ok("MSS clamping rule removed")
    if assume_yes or confirm("Delete configuration and state?", True):
        shutil.rmtree(CONF_DIR, ignore_errors=True)
        ok("Configuration deleted")
    if os.path.exists(BIN_PATH) and remove_quiet(BIN_PATH):
        ok("Removed %s" % BIN_PATH)
    ok("Uninstalled")


def first_run(cfg, state):
    banner()
    section("Welcome")
    print("  This tool finds the largest packet size each of your links can carry")
    print("  without fragmentation, applies it, and keeps it healthy in the background.")
    print("  Tunnels (WireGuard, GRE, VXLAN ...) are calculated from the uplink automatically.")
    print()
    if confirm("Optimize everything now and enable autopilot?", True):
        do_optimize(cfg, state)
        save_config(cfg)
        do_install(cfg)
        pause()
    else:
        save_config(cfg)


def interactive(cfg, state):
    if not os.path.exists(CONF_FILE):
        first_run(cfg, state)
    while True:
        banner()
        show_status(cfg, state)
        section("Actions")
        active, how = service_status()
        choice = menu([
            ("1", "Auto-optimize all interfaces", ""),
            ("2", "Optimize one interface", ""),
            ("3", "Scan only (dry run)", "shows results, keeps current MTU"),
            ("4", "Pin an MTU manually", ""),
            ("5", "Interface modes", "auto / pinned / ignore"),
            ("6", "Settings", ""),
            ("7", "Update autopilot" if how else "Enable autopilot", "systemd timer or cron"),
            ("8", "Restore original MTUs", ""),
            ("9", "View log", ""),
            ("u", "Uninstall", ""),
            ("0", "Exit", ""),
        ])
        if choice == "0":
            print()
            return
        if choice == "1":
            do_optimize(cfg, state)
            if not how and confirm("Enable autopilot to keep it optimal?", True):
                do_install(cfg)
        elif choice in ("2", "3"):
            dev = pick_iface("Interface")
            if not dev:
                continue
            do_optimize(cfg, state, only=[dev["name"]], dry=choice == "3")
        elif choice == "4":
            dev = pick_iface("Interface to pin")
            if not dev:
                continue
            low, high = mtu_bounds(dev)
            set_manual(cfg, state, dev["name"], ask_int("MTU for %s" % dev["name"], dev["mtu"], low, high))
        elif choice == "5":
            modes_menu(cfg, state)
            continue
        elif choice == "6":
            settings_menu(cfg)
            continue
        elif choice == "7":
            do_install(cfg)
        elif choice == "8":
            section("Restore")
            with Lock():
                restore_all(cfg, state)
        elif choice == "9":
            show_log()
        elif choice == "u":
            if confirm("Really uninstall netshell-mtu?", False):
                uninstall(cfg, state, False)
                return
            continue
        pause()


def watch(cfg, state):
    try:
        with Lock(wait=False):
            if not os.path.exists(CONF_FILE):
                save_config(cfg)
            optimize(cfg, state, watch=True)
    except BlockingIOError:
        log.info("another run is in progress, skipping")


def build_parser():
    parser = argparse.ArgumentParser(prog=APP, description="netshell MTU optimizer: path-MTU discovery with autopilot. Run without arguments for the interactive dashboard.", epilog="Source and issues: " + REPO)
    parser.add_argument("--no-interact", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-color", action="store_true", help="disable colors")
    parser.add_argument("-V", "--version", action="version", version="%s %s  %s" % (APP, VERSION, REPO))
    sub = parser.add_subparsers(dest="cmd", metavar="command")
    p = sub.add_parser("auto", help="discover and apply the best MTU")
    p.add_argument("ifaces", nargs="*", help="limit to these interfaces")
    p = sub.add_parser("scan", help="discover only, change nothing permanently")
    p.add_argument("ifaces", nargs="*", help="limit to these interfaces")
    p = sub.add_parser("set", help="pin a fixed MTU on an interface")
    p.add_argument("iface")
    p.add_argument("mtu", type=int)
    p = sub.add_parser("mode", help="set interface mode: auto or ignore")
    p.add_argument("iface")
    p.add_argument("mode", choices=("auto", "ignore"))
    sub.add_parser("status", help="show interfaces and autopilot state")
    sub.add_parser("install", help="enable autopilot (systemd timer or cron)")
    sub.add_parser("restore", help="restore MTUs from before the first change")
    p = sub.add_parser("uninstall", help="remove autopilot, rules and config")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask")
    sub.add_parser("log", help="show recent log")
    sub.add_parser("watch", help="single autopilot pass (used by the timer)")
    return parser


def on_signal(signum, frame):
    raise KeyboardInterrupt


def main():
    global QUIET, USE_COLOR
    if "--no-color" in sys.argv[1:]:
        sys.argv = [a for a in sys.argv if a != "--no-color"]
        USE_COLOR = False
    args = build_parser().parse_args()
    cmd = "watch" if args.no_interact else args.cmd
    if cmd is None and not sys.stdin.isatty():
        build_parser().print_help()
        sys.exit(2)
    ensure_root()
    QUIET = cmd == "watch"
    setup_logging(QUIET)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    cfg = load_config()
    state = load_json(STATE_FILE, {})
    if not ensure_tools():
        sys.exit(1)
    if not state.get("legacy_done"):
        legacy_cleanup(cfg, state)
    if cmd is None:
        interactive(cfg, state)
    elif cmd == "watch":
        watch(cfg, state)
    elif cmd in ("auto", "scan"):
        do_optimize(cfg, state, only=args.ifaces or None, dry=cmd == "scan")
        if not os.path.exists(CONF_FILE):
            save_config(cfg)
    elif cmd == "set":
        with Lock():
            sys.exit(0 if set_manual(cfg, state, args.iface, args.mtu) else 1)
    elif cmd == "mode":
        cfg["interfaces"][args.iface] = {"mode": args.mode}
        save_config(cfg)
        ok("%s is now in %s mode" % (args.iface, args.mode))
    elif cmd == "status":
        show_status(cfg, state)
        print()
    elif cmd == "install":
        sys.exit(0 if do_install(cfg) else 1)
    elif cmd == "restore":
        with Lock():
            restore_all(cfg, state)
    elif cmd == "uninstall":
        uninstall(cfg, state, args.yes)
    elif cmd == "log":
        show_log()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        clear_line()
        print("\n  " + paint("Interrupted, any temporary MTU change was rolled back.", YELLOW))
        sys.exit(130)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
