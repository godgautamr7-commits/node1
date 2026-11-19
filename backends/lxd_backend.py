#!/usr/bin/env python3
"""LXD node agent.
- Imported by bot.py for the local node (node1)
- Run standalone on remote nodes: it dials OUT to the bot (WebSocket via Cloudflare), so the node needs
  no public IP, no open port and works behind NAT / on localhost.
"""
import asyncio
import json
import os
import re
import shutil
import sys
import time
from asyncio.subprocess import DEVNULL, PIPE, STDOUT

IMAGES = {
    "ubuntu22": ("Ubuntu 22.04", "ubuntu:22.04"),
    "ubuntu24": ("Ubuntu 24.04", "ubuntu:24.04"),
    "debian11": ("Debian 11", "images:debian/11"),
    "debian12": ("Debian 12", "images:debian/12"),
}
if "/snap/bin" not in os.environ.get("PATH", ""):
    os.environ["PATH"] = os.environ.get("PATH", "") + ":/snap/bin"
BRIDGE = os.getenv("LXD_BRIDGE", "lxdbr0")
NAME_RE = re.compile(r"vps-[a-z0-9-]{3,40}")
EVENTS = []

MINER_RE = re.compile(
    r"xmrig|minerd|cpuminer|ethminer|nbminer|lolminer|nanominer|phoenixminer|"
    r"gminer|bzminer|stratum\+|cryptonight|randomx|--donate-level|kdevtmpfsi|"
    r"minergate|nicehash|xmr-stak|hellminer",
    re.I,
)

SSHX_UNIT = """[Unit]
Description=sshx web terminal
After=network-online.target

[Service]
Environment=HOME=/root
ExecStart=/bin/bash -c 'rm -f /root/.sshx_link; exec $(command -v sshx || echo /usr/local/bin/sshx) -q > /root/.sshx_link'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


async def sh(*a, inp=None, timeout=300):
    # stdin must be closed when there is no input: on Python 3.10 communicate() leaves it open, and
    # 'lxc init/launch' reads its config from stdin when it is not a terminal -> it would wait forever.
    try:
        p = await asyncio.create_subprocess_exec(*a, stdin=PIPE if inp is not None else DEVNULL, stdout=PIPE, stderr=STDOUT)
    except (FileNotFoundError, PermissionError) as e:  # missing binary must not crash a command
        return 127, f"command not found / not runnable: {a[0]} ({e})"
    try:
        out, _ = await asyncio.wait_for(p.communicate(inp.encode() if inp else None), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "timeout"
    return p.returncode, out.decode(errors="replace").strip()


def ex(name, cmd, **kw):
    return sh("lxc", "exec", name, "--", "bash", "-c", cmd, **kw)


def _chk(name):
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError("bad container name")


async def _get_sshx(name, tries=20):
    """sshx web-terminal link. The sshx service runs INSIDE the container as the container's root,
    so the link opens a root shell of that container only - never the host machine."""
    for _ in range(tries):
        rc, out = await ex(name, "cat /root/.sshx_link 2>/dev/null", timeout=15)
        if rc == 0 and out.startswith("http"):
            return out.splitlines()[0].strip()
        await asyncio.sleep(2)
    return ""


async def _install_sshx(name):
    """Install sshx inside the container + start it as a service. Returns the link, raises on failure."""
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
    await sh("lxc", "exec", name, "--", "tee", "/etc/systemd/system/sshx.service", inp=SSHX_UNIT)
    await ex(name, "systemctl daemon-reload && systemctl enable sshx && systemctl restart sshx")
    link = await _get_sshx(name)
    if not link:
        raise RuntimeError("sshx started but gave no link (check container internet / sshx.io)")
    return link


async def _drop_tmate(name):
    """Old containers may still have tmate - remove it so sshx is the only way in."""
    await ex(name, "systemctl disable --now tmate-ssh 2>/dev/null; pkill -x tmate 2>/dev/null; "
                   "rm -f /etc/systemd/system/tmate-ssh.service /tmp/tmate.sock; systemctl daemon-reload", timeout=60)


# ---------------------------------------------------------------- auto install LXD
APT = "export DEBIAN_FRONTEND=noninteractive; "


async def ensure_lxd():
    """Install + initialise LXD on this machine if it is missing. Returns a short log ('' = nothing to do)."""
    if os.geteuid() != 0:
        raise RuntimeError("must run as root")
    notes = []
    if not shutil.which("iptables"):
        await sh("bash", "-c", APT + "apt-get update -qq && apt-get install -y -qq iptables", timeout=600)
        notes.append("iptables installed")
    if not shutil.which("lxc"):
        if not shutil.which("snap"):
            rc, out = await sh("bash", "-c", APT + "apt-get update -qq && apt-get install -y -qq snapd", timeout=900)
            if rc:
                raise RuntimeError("snapd install failed: " + out[-200:])
            await sh("systemctl", "enable", "--now", "snapd.socket")
            await asyncio.sleep(8)
        rc, out = await sh("bash", "-c", "snap install lxd || (sleep 15; snap install lxd)", timeout=1500)
        if not shutil.which("lxc"):
            raise RuntimeError("LXD install failed: " + out[-300:])
        await sh("lxd", "waitready", "--timeout", "120", timeout=150)
        notes.append("LXD installed")
    rc, out = await sh("lxc", "storage", "list", "--format", "json")
    if rc:
        raise RuntimeError("lxc not working: " + out[-200:])
    if not json.loads(out or "[]"):
        free_gb = shutil.disk_usage("/").free // 10**9
        size = int(os.getenv("LXD_LOOP_GB") or max(10, int(free_gb * 0.8)))
        order = [os.getenv("LXD_STORAGE")] if os.getenv("LXD_STORAGE") else ["btrfs", "zfs", "dir"]
        last = ""
        for be in order:
            args = ["lxd", "init", "--auto", "--storage-backend", be]
            if be != "dir":
                args += ["--storage-create-loop", str(size)]
            rc, last = await sh(*args, timeout=900)
            if rc == 0:
                notes.append(f"LXD initialised ({be}" + (f", {size}GB" if be != "dir" else
                             ", WARNING: dir backend cannot enforce disk limits") + ")")
                break
            # clean leftovers of the failed attempt before trying the next backend
            await sh("lxc", "network", "delete", "lxdbr0")
            await sh("lxc", "storage", "delete", "default")
        else:
            raise RuntimeError("lxd init failed: " + last[-300:])
    return " · ".join(notes)


# ---------------------------------------------------------------- image cache + diagnostics
# primary source first, mirror second (used when the primary is slow / unreachable)
FALLBACK = {
    "ubuntu22": "images:ubuntu/22.04",
    "ubuntu24": "images:ubuntu/24.04",
    "debian11": "images:debian/11",
    "debian12": "images:debian/12",
}
IMAGE_TIMEOUT = int(os.getenv("IMAGE_TIMEOUT", "600"))  # seconds per image-download attempt


async def diagnose():
    """Collect what is going on when a create step hangs. Returns a short text (also printed to the log)."""
    parts = []
    for label, cmd in (
        ("lxc operations", "lxc operation list --format csv 2>&1 | head -5"),
        ("lxc processes", "ps -eo pid,etime,args | grep -E 'lxc (launch|init|image)' | grep -v grep | head -5"),
        ("disk free", "df -h / | tail -1"),
        ("ipv4 to images", "curl -4 -sS -m 10 -o /dev/null -w '%{http_code}' https://images.lxd.canonical.com/ 2>&1"),
        ("ipv4 to ubuntu", "curl -4 -sS -m 10 -o /dev/null -w '%{http_code}' https://cloud-images.ubuntu.com/releases/streams/v1/index.json 2>&1"),
    ):
        rc, out = await sh("bash", "-c", cmd, timeout=30)
        parts.append(f"{label}: {(out or '-')[-160:]}")
    text = " | ".join(parts)
    print("DIAG:", text, flush=True)
    return text


IMAGE_STALL = int(os.getenv("IMAGE_STALL", "120"))  # abort a download whose % has not moved for this long
_IMG_LOCKS = {}


async def _download_pct():
    """Best-effort: percentage of the running LXD image download (None if not visible)."""
    rc, out = await sh("lxc", "operation", "list", "--format", "json", timeout=15)
    if rc:
        return None
    try:
        for op in json.loads(out or "[]"):
            m = re.search(r"(\d{1,3})%", json.dumps(op.get("metadata") or {}))
            if m:
                return min(int(m.group(1)), 100)
    except Exception:
        pass
    return None


async def _copy_image(src, alias, P):
    """lxc image copy with live progress. Aborts when it stalls (no % change) or takes too long."""
    p = await asyncio.create_subprocess_exec(
        "lxc", "image", "copy", src, "local:", "--alias", alias, stdin=DEVNULL, stdout=PIPE, stderr=STDOUT,
        start_new_session=True)
    waiter = asyncio.ensure_future(p.communicate())
    t0 = last_move = time.monotonic()
    last = -1
    while not waiter.done():
        await asyncio.wait({waiter}, timeout=3)
        if waiter.done():
            break
        now = time.monotonic()
        pct = await _download_pct()
        if pct is not None and pct != last:
            last, last_move = pct, now
            await P(5 + int(pct * 0.14), f"Downloading OS image {pct}% (one time only)")
        if now - last_move > IMAGE_STALL or now - t0 > IMAGE_TIMEOUT:
            try:
                os.killpg(p.pid, 9)  # kill the whole group so no child keeps the pipe open
            except Exception:
                p.kill()
            waiter.cancel()
            try:
                await asyncio.wait_for(p.wait(), 5)
            except Exception:
                pass
            why = f"stalled at {max(last, 0)}% for {IMAGE_STALL}s" if now - last_move > IMAGE_STALL else "too slow"
            return 124, f"download {why}"
    out, _ = waiter.result()
    return p.returncode, out.decode(errors="replace").strip()


async def _ensure_image(os_key, P):
    """Make sure the OS image is cached locally BEFORE creating the container.
    Returns the local alias to launch from. Tries the primary source, then the mirror.
    One download at a time per OS (the startup pre-download and a deploy share it instead of fighting)."""
    alias = f"lb-{os_key}"
    lock = _IMG_LOCKS.setdefault(os_key, asyncio.Lock())
    await P(5, "Preparing OS image (waiting for running download)" if lock.locked() else "Preparing OS image")
    async with lock:
        rc, out = await sh("lxc", "image", "info", f"local:{alias}", timeout=30)
        if rc == 0:
            return alias
        last = ""
        for src in (IMAGES[os_key][1], FALLBACK[os_key]):
            await P(5, f"Downloading OS image from {src.split(':')[0]} (one time only)")
            rc, last = await _copy_image(src, alias, P)
            if rc == 0:
                return alias
            # a killed client leaves the daemon-side download running: cancel stale operations
            await sh("bash", "-c", "lxc operation list --format csv | cut -d, -f1 | "
                     "xargs -r -n1 lxc operation delete 2>/dev/null", timeout=30)
        d = await diagnose()
        raise RuntimeError(f"OS image download failed ({last[-120:]}) :: {d}")



# ---------------------------------------------------------------- real disk limits
QUOTA_POOL_NAME = "lbquota"
QUOTA_POOL = None  # set to the pool name once a pool that really enforces disk size is verified
QUOTA_NOTE = ""
QUOTA_TRIED = False
_QUOTA_LOCK = None


async def _pool_driver(pool):
    rc, out = await sh("lxc", "storage", "show", pool, timeout=30)
    m = re.search(r"^driver:\s*(\S+)", out, re.M) if rc == 0 else None
    return m.group(1) if m else None


async def _verify_quota(pool):
    """Launch a throw-away container with a 2GB root on this pool and check it really sees ~2GB."""
    async def _np(*a):
        pass
    t = "lb-quotatest"
    await sh("lxc", "delete", t, "--force")
    try:
        alias = await _ensure_image("ubuntu22", _np)
        rc, out = await sh("lxc", "init", alias, t, "-s", pool, "-d", "root,size=2GB", timeout=300)
        if rc == 0:
            rc, out = await sh("lxc", "start", t, timeout=120)
        if rc:
            return False
        for _ in range(20):
            rc, out = await ex(t, "df -BG --output=size / | tail -1", timeout=20)
            m = re.search(r"(\d+)", out or "")
            if rc == 0 and m:
                return 1 <= int(m.group(1)) <= 4
            await asyncio.sleep(2)
        return False
    finally:
        await sh("lxc", "delete", t, "--force")


async def _try_pool(driver, size_gb):
    await sh("lxc", "storage", "delete", QUOTA_POOL_NAME)
    rc, out = await sh("lxc", "storage", "create", QUOTA_POOL_NAME, driver, f"size={size_gb}GB", timeout=300)
    if rc:
        return False, out[-120:]
    if await _verify_quota(QUOTA_POOL_NAME):
        return True, ""
    await sh("lxc", "storage", "delete", QUOTA_POOL_NAME)
    return False, "size limit not enforced"


async def _try_ext4_prjquota(size_gb):
    """dir pool on an ext4 loop file mounted with project quotas - LXD can then enforce 'size' on dir."""
    img, mnt = "/var/lib/lbquota.img", "/mnt/lbquota"
    await sh("lxc", "storage", "delete", QUOTA_POOL_NAME)
    if not os.path.ismount(mnt):
        os.makedirs(mnt, exist_ok=True)
        if not os.path.exists(img):
            await sh("truncate", "-s", f"{size_gb}G", img)
            rc, out = await sh("mkfs.ext4", "-q", "-F", "-O", "project,quota", "-E", "quotatype=prjquota", img, timeout=300)
            if rc:
                return False, "mkfs: " + out[-100:]
        rc, out = await sh("mount", "-o", "loop,prjquota", img, mnt)
        if rc:
            return False, "mount: " + out[-100:]
        line = f"{img} {mnt} ext4 loop,prjquota 0 0"
        try:
            if line not in open("/etc/fstab").read():
                open("/etc/fstab", "a").write(line + "\n")
        except Exception:
            pass
    rc, out = await sh("lxc", "storage", "create", QUOTA_POOL_NAME, "dir", f"source={mnt}")
    if rc:
        return False, out[-120:]
    if await _verify_quota(QUOTA_POOL_NAME):
        return True, ""
    await sh("lxc", "storage", "delete", QUOTA_POOL_NAME)
    return False, "size limit not enforced"


async def ensure_quota_pool():
    global _QUOTA_LOCK, QUOTA_TRIED
    if _QUOTA_LOCK is None:
        _QUOTA_LOCK = asyncio.Lock()
    async with _QUOTA_LOCK:
        if QUOTA_TRIED:
            return QUOTA_NOTE
        r = await _ensure_quota_pool()
        QUOTA_TRIED = True
        return r


async def _ensure_quota_pool():
    """Find/build a storage pool where each VPS disk is really capped. Never raises.
    Order: reuse existing -> btrfs -> lvm -> zfs -> ext4(project quota) loop. Returns a short status text."""
    global QUOTA_POOL, QUOTA_NOTE
    try:
        rc, out = await sh("lxc", "storage", "list", "--format", "json", timeout=30)
        pools = json.loads(out or "[]") if rc == 0 else []
        for p in pools:  # existing non-dir pool: size limits work natively
            if p.get("driver") in ("btrfs", "zfs", "lvm", "ceph") and p["name"] != "lb-selftest":
                if await _verify_quota(p["name"]):
                    QUOTA_POOL, QUOTA_NOTE = p["name"], f"disk limits enforced ({p['driver']})"
                    return QUOTA_NOTE
        if any(p["name"] == QUOTA_POOL_NAME for p in pools) and await _verify_quota(QUOTA_POOL_NAME):
            QUOTA_POOL = QUOTA_POOL_NAME
            QUOTA_NOTE = f"disk limits enforced ({await _pool_driver(QUOTA_POOL_NAME)})"
            return QUOTA_NOTE
        free_gb = shutil.disk_usage("/").free // 10**9
        size_gb = int(os.getenv("LXD_LOOP_GB") or max(10, int(free_gb * 0.7)))
        errs = []
        for name, fn in (("btrfs", lambda: _try_pool("btrfs", size_gb)),
                         ("lvm", lambda: _try_pool("lvm", size_gb)),
                         ("zfs", lambda: _try_pool("zfs", size_gb)),
                         ("ext4-quota", lambda: _try_ext4_prjquota(size_gb))):
            ok, why = await fn()
            if ok:
                QUOTA_POOL = QUOTA_POOL_NAME
                QUOTA_NOTE = f"disk limits enforced ({name}, {size_gb}GB pool)"
                return QUOTA_NOTE
            errs.append(f"{name}: {why}")
        QUOTA_NOTE = "WARNING: disk limit NOT enforceable on this server - " + " | ".join(errs)
        return QUOTA_NOTE
    except Exception as e:
        QUOTA_NOTE = f"WARNING: disk limit check failed: {e}"
        return QUOTA_NOTE


async def prewarm_images():
    """Download every OS image once, in the background, so the first VPS of each OS is created fast.
    Never raises; a failed image is simply downloaded on demand when someone deploys it."""
    async def _np(*x):
        pass
    for key in IMAGES:
        try:
            await _ensure_image(key, _np)
            print(f"image {key} ready", flush=True)
        except Exception as e:
            print(f"image {key} pre-download failed (will retry on demand): {str(e)[:200]}", flush=True)


# ---------------------------------------------------------------- operations
async def op_create(name, os_key, cpu, ram, disk, progress=None):
    """progress(pct, text) is optional (local node only) - it reports the REAL stage so the panel can show it."""
    _chk(name)
    if os_key not in IMAGES:
        raise ValueError("unknown OS")

    async def P(pct, text):
        if progress:
            try:
                progress(pct, text)
            except Exception:
                pass

    alias = await _ensure_image(os_key, P)
    if not QUOTA_TRIED:
        await P(18, "Checking disk-limit storage (first time only)")
        await ensure_quota_pool()
    await P(20, "Creating container storage")
    # 'dir' storage cannot enforce a disk size (and the size option can make init hang): skip it there.
    driver = ""
    rc0, so = await sh("lxc", "storage", "list", "--format", "json", timeout=30)
    try:
        driver = (json.loads(so)[0].get("driver") or "") if rc0 == 0 else ""
    except Exception:
        pass
    args = ["lxc", "init", alias, name,
            "-c", f"limits.cpu={int(cpu)}",
            "-c", f"limits.memory={int(ram)}GB",
            "-c", "limits.memory.swap=false",
            "-c", "limits.processes=4096",
            "-c", "security.privileged=false"]  # root inside = container root only, never host root
    if not QUOTA_POOL and driver == "dir" and os.getenv("REQUIRE_DISK_LIMIT"):
        raise RuntimeError("disk limit cannot be enforced on this server: " + QUOTA_NOTE)
    if QUOTA_POOL:
        args += ["-s", QUOTA_POOL, "-d", f"root,size={int(disk)}GB"]
    elif driver != "dir" or os.getenv("FORCE_DISK_LIMIT"):
        args += ["-d", f"root,size={int(disk)}GB"]
    await P(20, f"Creating container storage ({'quota pool' if QUOTA_POOL else driver or 'unknown'})")
    rc, out = await sh(*args, timeout=300)
    if rc == 0:
        await P(25, "Starting container")
        rc, out = await sh("lxc", "start", name, timeout=180)
    if rc:
        await sh("lxc", "delete", name, "--force")
        hint = ""
        if "quota" in out.lower() or "size" in out.lower():
            hint = " (storage backend cannot enforce the disk size - use btrfs/zfs/lvm, not 'dir')"
        if out == "timeout":
            hint = " :: " + await diagnose()
        raise RuntimeError("launch failed: " + out[-300:] + hint)
    # network speed cap per container (ignored if unsupported)
    await sh("lxc", "config", "device", "override", name, "eth0",
             "limits.egress=1Gbit", "limits.ingress=1Gbit")
    try:
        await P(30, "Waiting for container network")
        for _ in range(40):
            rc, _o = await ex(name, "getent hosts github.com", timeout=15)
            if rc == 0:
                break
            await asyncio.sleep(3)
        else:
            raise RuntimeError("container has no internet/DNS (host firewall / Docker FORWARD rule / ufw?)")
        await ex(name, "sysctl -w net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1 >/dev/null 2>&1; "
                       "echo 'Acquire::ForceIPv4 \"true\";' > /etc/apt/apt.conf.d/99ipv4", timeout=30)
        await P(50, "Setting up SSHX terminal (container root access)")
        sshx = await _install_sshx(name)
        await P(95, "Finishing")
        return {"sshx": sshx}
    except BaseException:  # includes cancellation/timeouts: never leave a half-built container behind
        await sh("lxc", "delete", name, "--force")
        raise


async def op_action(name, action):
    _chk(name)
    cmds = {
        "start": ["lxc", "start", name],
        "stop": ["lxc", "stop", name, "--force"],
        "restart": ["lxc", "restart", name, "--force"],
        "delete": ["lxc", "delete", name, "--force"],
    }
    if action not in cmds:
        raise ValueError("bad action")
    rc, out = await sh(*cmds[action], timeout=180)
    if rc:
        raise RuntimeError(out[-300:])
    return {"ok": True}


async def op_ssh(name, reset=False):
    _chk(name)
    await _drop_tmate(name)
    if reset or not await _get_sshx(name, tries=1):
        return {"sshx": await _install_sshx(name)}  # (re)installs if missing, restarts, returns fresh link
    return {"sshx": await _get_sshx(name, tries=8)}


async def op_status():
    rc, out = await sh("lxc", "list", "--format", "json")
    if rc:
        raise RuntimeError(out[-200:])
    cs = [c for c in json.loads(out) if c["name"].startswith("vps-")]
    return {"count": len(cs), "running": sum(c["status"] == "Running" for c in cs)}


async def op_usage(name):
    _chk(name)
    rc, out = await sh("lxc", "list", name, "--format", "json")
    if rc:
        raise RuntimeError(out[-200:])
    c = next((x for x in json.loads(out or "[]") if x["name"] == name), None)
    if not c:
        raise RuntimeError("container not found")
    st = c.get("state") or {}
    ips = [ad["address"] for n, net in (st.get("network") or {}).items() if n != "lo"
           for ad in net.get("addresses", []) if ad.get("family") == "inet"]
    return {
        "status": c["status"],
        "mem": (st.get("memory") or {}).get("usage", 0),
        "disk": ((st.get("disk") or {}).get("root") or {}).get("usage", 0),
        "cpu_s": (st.get("cpu") or {}).get("usage", 0) / 1e9,
        "procs": st.get("processes", 0),
        "ips": ips,
    }


async def op_events():
    ev = EVENTS[:]
    EVENTS.clear()
    return {"events": ev}


OPS = {
    "create": op_create, "action": op_action, "ssh": op_ssh,
    "status": op_status, "events": op_events, "usage": op_usage,
}


# ---------------------------------------------------------------- anti-mining
async def scan_once():
    rc, out = await sh("lxc", "list", "--format", "json")
    if rc:
        return
    for c in json.loads(out):
        if c["status"] != "Running" or not c["name"].startswith("vps-"):
            continue
        rc, ps = await ex(c["name"], "ps -eo comm=,args=", timeout=30)
        m = MINER_RE.search(ps) if rc == 0 else None
        if m:
            await sh("lxc", "stop", c["name"], "--force")
            EVENTS.append({"name": c["name"], "match": m.group(0)})


async def watchdog_loop(interval=60):
    while True:
        try:
            await scan_once()
        except Exception as e:
            print("watchdog error:", e)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------- firewall
async def _chain(chain, parent_args, rules):
    await sh("iptables", "-N", chain)
    await sh("iptables", "-F", chain)
    for r in rules:
        await sh("iptables", "-A", chain, *r)
    rc, _ = await sh("iptables", "-C", *parent_args)
    if rc:
        await sh("iptables", "-I", parent_args[0], "1", *parent_args[1:])


def _hl(name, rate, burst):
    return ["-m", "hashlimit", "--hashlimit-above", rate, "--hashlimit-burst", str(burst),
            "--hashlimit-mode", "srcip", "--hashlimit-name", name, "-j", "DROP"]


async def apply_protection():
    """Outbound-abuse limits for containers + SYN flood limits + hide host from containers."""
    await sh("sysctl", "-w", "net.ipv4.tcp_syncookies=1")
    b = BRIDGE
    # local host without public IP: containers use IPv4 + NAT only (no IPv6 -> no IPv6 hangs)
    await sh("lxc", "network", "set", b, "ipv6.address", "none")
    await sh("lxc", "network", "set", b, "ipv4.nat", "true")
    # traffic leaving containers (anti-mining pools, spam, flood tools)
    await _chain("LXDBOT_FWD", ["FORWARD", "-i", b, "-j", "LXDBOT_FWD"], [
        ["-p", "tcp", "-m", "multiport", "--dports",
         "25,465,587,3333,4444,5555,6666,7777,9999,14433,14444,45560,45700", "-j", "DROP"],
        ["-p", "tcp", "--syn", *_hl("lb_syn", "200/sec", 400)],
        ["-p", "udp", *_hl("lb_udp", "5000/sec", 8000)],
        ["-p", "icmp", *_hl("lb_icmp", "50/sec", 100)],
    ])
    # containers may only use DNS/DHCP on the host - nothing else (host services hidden)
    await _chain("LXDBOT_IN", ["INPUT", "-j", "LXDBOT_IN"], [
        ["-i", b, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ["-i", b, "-p", "udp", "-m", "multiport", "--dports", "53,67", "-j", "ACCEPT"],
        ["-i", b, "-p", "tcp", "--dport", "53", "-j", "ACCEPT"],
        ["-i", b, "-j", "DROP"],
        ["-p", "tcp", "--syn", *_hl("lb_in_syn", "100/sec", 200)],
    ])


# ---------------------------------------------------------------- standalone client (node -> bot)
def ws_url(raw):
    """Accepts: bot.example.com | https://bot.example.com | wss://.../ws | localhost:8765 | 1.2.3.4:8765"""
    raw = raw.strip().rstrip("/")
    if raw.startswith("https://"):
        raw = "wss://" + raw[8:]
    elif raw.startswith("http://"):
        raw = "ws://" + raw[7:]
    elif not raw.startswith(("ws://", "wss://")):
        plain = re.match(r"^(localhost|127\.|\[|\d+\.\d+\.\d+\.\d+)", raw) or ":" in raw
        raw = ("ws://" if plain else "wss://") + raw
    return raw if raw.endswith("/ws") else raw + "/ws"


async def main():
    from aiohttp import ClientSession, ClientTimeout, WSMsgType

    token = os.environ["TOKEN"]
    panel = os.getenv("PANEL_URL", "").strip()
    if not panel:
        sys.exit("PANEL_URL missing (the bot's Cloudflare URL, e.g. https://bot.yourdomain.com)")
    url = ws_url(panel)

    try:
        note = await ensure_lxd()
        if note:
            print("setup:", note)
        await apply_protection()
    except Exception as e:
        print("lxd setup / protection failed:", e)
    asyncio.create_task(prewarm_images())  # install all OS images on this node in the background
    print("storage:", await ensure_quota_pool(), flush=True)
    asyncio.create_task(watchdog_loop())

    async def run_op(ws, msg):
        rid = msg.get("id")
        try:
            op = msg.get("op")
            if op not in OPS:
                raise RuntimeError("unknown op")
            out = {"id": rid, "data": await OPS[op](**(msg.get("kw") or {}))}
        except Exception as e:
            out = {"id": rid, "error": str(e)}
        try:
            await ws.send_json(out)
        except Exception:
            pass  # link dropped while the op ran; the bot reports the node as disconnected

    backoff = 3
    running = set()
    print(f"node will connect to {url} (outbound only - no port is opened here)", flush=True)
    while True:
        try:
            async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=30)) as s:
                async with s.ws_connect(url, headers={"Authorization": f"Bearer {token}"},
                                        heartbeat=20, max_msg_size=0) as ws:
                    print("connected to bot", flush=True)
                    backoff = 3
                    async for m in ws:
                        if m.type == WSMsgType.TEXT:
                            t = asyncio.create_task(run_op(ws, json.loads(m.data)))
                            running.add(t)
                            t.add_done_callback(running.discard)
        except Exception as e:
            print("bot link error:", str(e)[:200], flush=True)
        print(f"disconnected - retrying in {backoff}s", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    asyncio.run(main())
