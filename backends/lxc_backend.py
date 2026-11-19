#!/usr/bin/env python3
"""Classic LXC node backend (lxc-create / lxc-start / lxc-attach, bridge lxcbr0).
Not LXD: this uses the plain `lxc-*` tools. Used by node/agent.py - not started directly.
Same interface as docker_backend / lxd_backend: ensure_lxd, apply_protection, prewarm_images,
ensure_quota_pool, watchdog_loop, OPS.
"""
import asyncio
import json
import os
import re
import shutil
import time

from . import lxd_backend as L  # shared helpers: sh(), MINER_RE, SSHX_UNIT, firewall chains, EVENTS

sh = L.sh
EVENTS = L.EVENTS
NAME_RE = L.NAME_RE
MINER_RE = L.MINER_RE
SSHX_UNIT = L.SSHX_UNIT
BRIDGE = os.getenv("LXC_BRIDGE", "lxcbr0")
ARCH = os.getenv("LXC_ARCH", "amd64")
IMAGE_TIMEOUT = int(os.getenv("IMAGE_TIMEOUT", "900"))
LXC_PATH = os.getenv("LXC_PATH", "/var/lib/lxc")
APT = "export DEBIAN_FRONTEND=noninteractive; "
PATH_FIX = "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; "

# key -> (label, dist, release)   (images from the LXC "download" template)
IMAGES = {
    "ubuntu22": ("Ubuntu 22.04", "ubuntu", "jammy"),
    "ubuntu24": ("Ubuntu 24.04", "ubuntu", "noble"),
    "debian11": ("Debian 11", "debian", "bullseye"),
    "debian12": ("Debian 12", "debian", "bookworm"),
}


def ex(name, cmd, **kw):
    return sh("lxc-attach", "-n", name, "--", "bash", "-c", PATH_FIX + cmd, **kw)


def _chk(name):
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError("bad container name")


# ---------------------------------------------------------------- setup
async def ensure_lxd():
    """(name kept for the bot) Install + prepare classic LXC and the lxcbr0 bridge. Returns a short log."""
    if os.geteuid() != 0:
        raise RuntimeError("must run as root")
    notes = []
    if not shutil.which("lxc-create"):
        rc, out = await sh("bash", "-c", APT + "apt-get update -qq && apt-get install -y -qq lxc lxc-utils "
                           "uidmap bridge-utils dnsmasq-base iptables iproute2 curl gnupg rsync xz-utils", timeout=1500)
        if not shutil.which("lxc-create"):
            raise RuntimeError("LXC install failed: " + out[-300:])
        notes.append("LXC installed")
    if not shutil.which("iptables"):
        await sh("bash", "-c", APT + "apt-get install -y -qq iptables", timeout=600)
    # bridge lxcbr0 comes from the lxc-net service
    rc, _ = await sh("ip", "link", "show", BRIDGE)
    if rc:
        cfg = "/etc/default/lxc-net"
        try:
            txt = open(cfg).read() if os.path.exists(cfg) else ""
            if not re.search(r'^USE_LXC_BRIDGE="?true"?', txt, re.M):
                txt = re.sub(r'^USE_LXC_BRIDGE=.*\n?', "", txt, flags=re.M) + '\nUSE_LXC_BRIDGE="true"\n'
                open(cfg, "w").write(txt)
        except OSError as e:
            raise RuntimeError("cannot write /etc/default/lxc-net: " + str(e))
        await sh("systemctl", "enable", "--now", "lxc-net", timeout=60)
        await sh("systemctl", "restart", "lxc-net", timeout=60)
        for _ in range(10):
            rc, _ = await sh("ip", "link", "show", BRIDGE)
            if rc == 0:
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError(f"bridge {BRIDGE} did not come up (check: systemctl status lxc-net)")
        notes.append(f"bridge {BRIDGE} created")
    await sh("sysctl", "-w", "net.ipv4.ip_forward=1")
    return " · ".join(notes)


ensure_lxc = ensure_lxd


async def diagnose():
    parts = []
    for label, cmd in (
        ("lxc", "lxc-create --version 2>&1 | head -1"),
        ("containers", "lxc-ls -f 2>&1 | head -8"),
        ("bridge", f"ip -br addr show {BRIDGE} 2>&1"),
        ("disk free", "df -h /var/lib/lxc | tail -1"),
        ("download images", "curl -4 -sS -m 10 -o /dev/null -w '%{http_code}' https://images.linuxcontainers.org 2>&1"),
        ("sshx.io", "curl -4 -sS -m 10 -o /dev/null -w '%{http_code}' https://sshx.io/get 2>&1"),
    ):
        rc, out = await sh("bash", "-c", cmd, timeout=30)
        parts.append(f"{label}: {(out or '-')[-160:]}")
    text = " | ".join(parts)
    print("DIAG:", text, flush=True)
    return text


# ---------------------------------------------------------------- images (cached by the download template)
_IMG_LOCKS = {}
_IMG_READY = set()


async def _ensure_image(os_key, P):
    """Warm the rootfs cache of the LXC download template for one OS (first create is then fast)."""
    if os_key not in IMAGES:
        raise ValueError("unknown OS")
    if os_key in _IMG_READY:
        return os_key
    lock = _IMG_LOCKS.setdefault(os_key, asyncio.Lock())
    async with lock:
        if os_key in _IMG_READY:
            return os_key
        label, dist, rel = IMAGES[os_key]
        await P(8, f"Downloading {label} image (first time only)")
        tmp = f"lb-warm-{os_key}"
        await sh("lxc-destroy", "-n", tmp, "-f")
        last = ""
        for extra in ([], ["--no-validate"]):
            rc, last = await sh("lxc-create", "-n", tmp, "-t", "download", "--", "-d", dist, "-r", rel,
                                "-a", ARCH, *extra, timeout=IMAGE_TIMEOUT)
            if rc == 0:
                break
            await asyncio.sleep(3)
        await sh("lxc-destroy", "-n", tmp, "-f")
        if rc:
            raise RuntimeError(f"image download failed for {label}: " + last[-300:])
        _IMG_READY.add(os_key)
        return os_key


async def prewarm_images():
    async def _np(*a):
        pass
    for key in IMAGES:
        try:
            await _ensure_image(key, _np)
            print(f"image {key} ready", flush=True)
        except Exception as e:
            print(f"image {key} pre-download failed (will retry on demand): {str(e)[:200]}", flush=True)


# ---------------------------------------------------------------- disk limit (loop-file rootfs)
QUOTA_OK = False
QUOTA_TRIED = False
QUOTA_NOTE = "disk limit not checked yet"
_QUOTA_LOCK = None


async def ensure_quota_pool():
    """Disk limit for classic LXC = a loop-file rootfs per container (lxc-create -B loop --fssize).
    Tested once with a tiny container. Never raises. Returns a short status text."""
    global _QUOTA_LOCK, QUOTA_TRIED, QUOTA_OK, QUOTA_NOTE
    if _QUOTA_LOCK is None:
        _QUOTA_LOCK = asyncio.Lock()
    async with _QUOTA_LOCK:
        if QUOTA_TRIED:
            return QUOTA_NOTE
        try:
            if os.getenv("LXC_DISK_LIMIT", "1") == "0":
                QUOTA_NOTE = "disk limit disabled (LXC_DISK_LIMIT=0): containers share the host disk"
                return QUOTA_NOTE
            await sh("lxc-destroy", "-n", "lb-selftest", "-f")
            rc, out = await sh("lxc-create", "-n", "lb-selftest", "-B", "loop", "--fssize", "1G", "-t", "download",
                               "--", "-d", "debian", "-r", "bookworm", "-a", ARCH, timeout=IMAGE_TIMEOUT)
            await sh("lxc-destroy", "-n", "lb-selftest", "-f")
            QUOTA_OK = rc == 0
            QUOTA_NOTE = ("disk limit works (loop-file rootfs per VPS)" if QUOTA_OK else
                          "disk limit NOT available, containers share the host disk: " + out[-150:])
        except Exception as e:
            QUOTA_NOTE = "disk limit check failed: " + str(e)[:150]
        finally:
            QUOTA_TRIED = True
        return QUOTA_NOTE


# ---------------------------------------------------------------- sshx inside the container
async def _get_sshx(name, tries=20):
    for _ in range(tries):
        rc, out = await ex(name, "cat /root/.sshx_link 2>/dev/null", timeout=15)
        if rc == 0 and out.startswith("http"):
            return out.splitlines()[0].strip()
        await asyncio.sleep(2)
    return ""


async def _install_sshx(name):
    rc, out = await ex(
        name,
        "export DEBIAN_FRONTEND=noninteractive; command -v curl >/dev/null || "
        "(apt-get -o Acquire::ForceIPv4=true update -qq && apt-get -o Acquire::ForceIPv4=true install -y -qq curl ca-certificates); "
        "command -v sshx >/dev/null || test -x /usr/local/bin/sshx || "
        "for i in 1 2 3 4; do curl -4 -sSf -m 60 https://sshx.io/get | sh && break; sleep 3; done; "
        "command -v sshx >/dev/null || test -x /usr/local/bin/sshx",
        timeout=360)
    if rc:
        raise RuntimeError("sshx install failed (container cannot reach sshx.io): " + out[-150:])
    unit = SSHX_UNIT.replace("'", "'\"'\"'")
    await ex(name, f"printf '%s' '{unit}' > /etc/systemd/system/sshx.service && systemctl daemon-reload "
                   "&& systemctl enable sshx && systemctl restart sshx", timeout=60)
    link = await _get_sshx(name)
    if not link:
        raise RuntimeError("sshx started but gave no link (check container internet / sshx.io)")
    return link


# ---------------------------------------------------------------- capacity check
def _host_mem():
    kb = 0
    for line in open("/proc/meminfo"):
        if line.startswith("MemTotal:"):
            kb = int(line.split()[1])
    return kb // 1024


async def _cinfo(name):
    rc, out = await sh("lxc-info", "-n", name)
    if rc:
        return None
    d = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d.setdefault(k.strip(), v.strip())
    return d


async def _vps_names():
    rc, out = await sh("lxc-ls", "-1")
    return [n for n in out.split() if n.startswith("vps-")] if rc == 0 else []


async def check_capacity(cpu, ram, disk):
    free_disk_gb = shutil.disk_usage(LXC_PATH if os.path.isdir(LXC_PATH) else "/").free / 2**30
    min_disk = float(os.getenv("MIN_FREE_DISK_GB", "5"))
    if free_disk_gb < disk + min_disk:
        raise RuntimeError(f"not enough free disk on the node ({free_disk_gb:.0f} GB free, need {disk} GB + {min_disk:.0f} GB spare)")
    if (os.cpu_count() or 1) < cpu:
        raise RuntimeError(f"node has only {os.cpu_count()} CPU cores, plan needs {cpu}")
    if _host_mem() < ram * 1024:
        raise RuntimeError(f"node has only {_host_mem() // 1024} GB RAM, plan needs {ram} GB")


# ---------------------------------------------------------------- operations
async def op_create(name, os_key, cpu, ram, disk, progress=None):
    _chk(name)
    if os_key not in IMAGES:
        raise ValueError("unknown OS")

    async def P(pct, text):
        if progress:
            try:
                progress(pct, text)
            except Exception:
                pass

    await P(3, "Checking server resources (RAM / CPU / disk)")
    await check_capacity(cpu, ram, disk)
    await _ensure_image(os_key, P)
    await ensure_lxd()
    if not QUOTA_TRIED:
        await P(18, "Checking disk-limit support (first time only)")
        await ensure_quota_pool()
    if not QUOTA_OK and os.getenv("REQUIRE_DISK_LIMIT"):
        raise RuntimeError("disk limit cannot be enforced on this node: " + QUOTA_NOTE)
    label, dist, rel = IMAGES[os_key]
    await P(22, "Creating container")
    await sh("lxc-destroy", "-n", name, "-f")
    base = ["lxc-create", "-n", name]
    if QUOTA_OK:
        base += ["-B", "loop", "--fssize", f"{int(disk)}G"]
    rc, out = await sh(*base, "-t", "download", "--", "-d", dist, "-r", rel, "-a", ARCH, timeout=IMAGE_TIMEOUT)
    if rc:
        await sh("lxc-destroy", "-n", name, "-f")
        raise RuntimeError("launch failed: " + out[-300:])
    try:
        cfg = os.path.join(LXC_PATH, name, "config")
        txt = open(cfg).read()
        add = []
        if "lxc.net.0.link" not in txt:
            add += ["lxc.net.0.type = veth", f"lxc.net.0.link = {BRIDGE}", "lxc.net.0.flags = up"]
        add += [
            f"lxc.cgroup2.memory.max = {int(ram) * 1024 ** 3}",
            "lxc.cgroup2.memory.swap.max = 0",
            f"lxc.cgroup2.cpu.max = {int(cpu) * 100000} 100000",
            "lxc.cgroup2.pids.max = 4096",
            "lxc.start.auto = 1",
        ]
        with open(cfg, "a") as f:
            f.write("\n" + "\n".join(add) + "\n")
        await P(30, "Starting container")
        rc, out = await sh("lxc-start", "-n", name, "-d", timeout=120)
        if rc:
            raise RuntimeError("start failed: " + out[-300:])
        await sh("lxc-wait", "-n", name, "-s", "RUNNING", "-t", "60", timeout=90)
        await P(40, "Waiting for container network")
        for _ in range(40):
            rc, _o = await ex(name, "getent hosts github.com", timeout=15)
            if rc == 0:
                break
            await asyncio.sleep(3)
        else:
            raise RuntimeError("container has no internet/DNS (host firewall / forward rule / lxc-net?)")
        await P(60, "Installing SSHX terminal (container root access)")
        link = await _install_sshx(name)
        await P(95, "Finishing")
        return {"sshx": link}
    except BaseException:  # never leave a half-built container behind
        await sh("lxc-destroy", "-n", name, "-f")
        raise


async def op_action(name, action):
    _chk(name)
    cmds = {
        "start": ["lxc-start", "-n", name, "-d"],
        "stop": ["lxc-stop", "-n", name, "-k"],
        "restart": ["bash", "-c", f"lxc-stop -n {name} -k; lxc-start -n {name} -d"],
        "delete": ["lxc-destroy", "-n", name, "-f"],
    }
    if action not in cmds:
        raise ValueError("bad action")
    rc, out = await sh(*cmds[action], timeout=180)
    if rc and not (action == "stop" and "not running" in out.lower()):
        raise RuntimeError(out[-300:])
    return {"ok": True}


async def op_ssh(name, reset=False):
    _chk(name)
    if reset or not await _get_sshx(name, tries=1):
        return {"sshx": await _install_sshx(name)}
    return {"sshx": await _get_sshx(name, tries=8)}


async def op_status():
    names = await _vps_names()
    running = 0
    for n in names:
        i = await _cinfo(n)
        running += bool(i and i.get("State") == "RUNNING")
    return {"count": len(names), "running": running}


def _bytes(s):
    m = re.match(r"([\d.]+)\s*(\w*)", s or "")
    if not m:
        return 0
    mult = {"": 1, "bytes": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}
    return int(float(m.group(1)) * mult.get(m.group(2), 1))


async def op_usage(name):
    _chk(name)
    i = await _cinfo(name)
    if not i:
        raise RuntimeError("container not found")
    rc, df = await ex(name, "df -B1 / | tail -1 | awk '{print $3}'", timeout=20) if i.get("State") == "RUNNING" else (1, "0")
    return {
        "status": "Running" if i.get("State") == "RUNNING" else "Stopped",
        "mem": _bytes(i.get("Memory use")),
        "disk": int(df) if rc == 0 and df.isdigit() else 0,
        "cpu_s": float((i.get("CPU use") or "0").split()[0]) if i.get("CPU use") else 0.0,
        "procs": 0,
        "ips": [x.strip() for x in (i.get("IP") or "").split(",") if x.strip() and not x.startswith("127.")],
    }


async def op_events():
    ev = EVENTS[:]
    EVENTS.clear()
    return {"events": ev}


OPS = {
    "create": op_create, "action": op_action, "ssh": op_ssh,
    "status": op_status, "events": op_events, "usage": op_usage,
}


# ---------------------------------------------------------------- anti-mining + firewall
async def scan_once():
    for n in await _vps_names():
        i = await _cinfo(n)
        if not i or i.get("State") != "RUNNING":
            continue
        rc, ps = await ex(n, "ps -eo comm=,args=", timeout=30)
        m = MINER_RE.search(ps) if rc == 0 else None
        if m:
            await sh("lxc-stop", "-n", n, "-k")
            EVENTS.append({"name": n, "match": m.group(0)})


async def watchdog_loop(interval=60):
    while True:
        try:
            await scan_once()
        except Exception as e:
            print("watchdog error:", e)
        await asyncio.sleep(interval)


async def apply_protection():
    """Same outbound-abuse / SYN-flood rules as the LXD backend, applied to lxcbr0.
    (The LXD version also tries `lxc network set`; that command does not exist here and just fails harmlessly.)"""
    L.BRIDGE = BRIDGE
    await L.apply_protection()
