#!/usr/bin/env python3
"""Docker node agent.
- Imported by bot.py for the local node (node1)
- Run standalone on remote nodes: it dials OUT to the bot (WebSocket via Cloudflare), so the node needs
  no public IP, no open port and works behind NAT / on localhost.
Every VPS is a Docker container (label lb.vps=1) created through the host's Docker socket.
"""
import asyncio
import base64
import json
import os
import re
import shutil
import sys
import time
from asyncio.subprocess import DEVNULL, PIPE, STDOUT

# key -> (label, base image)
IMAGES = {
    "ubuntu22": ("Ubuntu 22.04", "ubuntu:22.04"),
    "ubuntu24": ("Ubuntu 24.04", "ubuntu:24.04"),
    "debian11": ("Debian 11", "debian:11"),
    "debian12": ("Debian 12", "debian:12"),
}
NETWORK = os.getenv("VPS_NETWORK", "lbnet")
BRIDGE = os.getenv("VPS_BRIDGE", "lbbr0")
SUBNET = os.getenv("VPS_SUBNET", "172.29.0.0/16")
NAME_RE = re.compile(r"vps-[a-z0-9-]{3,40}")
IMAGE_TIMEOUT = int(os.getenv("IMAGE_TIMEOUT", "900"))  # seconds for building one OS image
EVENTS = []

MINER_RE = re.compile(
    r"xmrig|minerd|cpuminer|ethminer|nbminer|lolminer|nanominer|phoenixminer|"
    r"gminer|bzminer|stratum\+|cryptonight|randomx|--donate-level|kdevtmpfsi|"
    r"minergate|nicehash|xmr-stak|hellminer",
    re.I,
)

# Runs inside every VPS container: keeps an sshx web-terminal alive and writes its link to /root/.sshx_link.
ENTRY_SH = """#!/bin/bash
( while true; do
    rm -f /root/.sshx_link
    sshx -q > /root/.sshx_link 2>/dev/null
    sleep 5
  done ) &
exec sleep infinity
"""


def dockerfile(base):
    entry = base64.b64encode(ENTRY_SH.encode()).decode()
    return f"""FROM {base}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get -o Acquire::ForceIPv4=true update -qq \\
 && apt-get -o Acquire::ForceIPv4=true install -y -qq --no-install-recommends \\
      curl ca-certificates procps iproute2 iputils-ping net-tools nano vim-tiny wget git sudo less \\
 && rm -rf /var/lib/apt/lists/*
RUN for i in 1 2 3 4; do curl -4 -sSf -m 60 https://sshx.io/get | sh && break; sleep 3; done; \\
    command -v sshx >/dev/null || test -x /usr/local/bin/sshx
RUN echo {entry} | base64 -d > /usr/local/bin/lb-entry && chmod +x /usr/local/bin/lb-entry
ENV HOME=/root
WORKDIR /root
ENTRYPOINT ["/usr/local/bin/lb-entry"]
"""


async def sh(*a, inp=None, timeout=300):
    # stdin must be closed when there is no input, otherwise some CLIs wait for it forever.
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
    return sh("docker", "exec", name, "bash", "-c", cmd, **kw)


def _chk(name):
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError("bad container name")


async def _get_sshx(name, tries=20):
    """sshx web-terminal link. sshx runs INSIDE the container as the container's root,
    so the link opens a root shell of that container only - never the host machine."""
    for _ in range(tries):
        rc, out = await ex(name, "cat /root/.sshx_link 2>/dev/null", timeout=15)
        if rc == 0 and out.startswith("http"):
            return out.splitlines()[0].strip()
        await asyncio.sleep(2)
    return ""


# ---------------------------------------------------------------- docker setup
async def ensure_lxd():
    """(name kept for bot.py) Check the Docker daemon is reachable and create the VPS network.
    Returns a short log ('' = nothing to do)."""
    if not shutil.which("docker"):
        raise RuntimeError("docker CLI not found - this must run in the bot image or on a host with Docker")
    rc, out = await sh("docker", "info", "--format", "{{.ServerVersion}}", timeout=30)
    if rc:
        raise RuntimeError("cannot talk to Docker (is /var/run/docker.sock mounted?): " + out[-200:])
    notes = []
    rc, _ = await sh("docker", "network", "inspect", NETWORK, timeout=30)
    if rc:
        rc, out = await sh("docker", "network", "create", "--driver", "bridge", "--subnet", SUBNET,
                           "-o", f"com.docker.network.bridge.name={BRIDGE}", NETWORK, timeout=60)
        if rc:  # subnet may clash with something: let Docker choose
            rc, out = await sh("docker", "network", "create", "--driver", "bridge",
                               "-o", f"com.docker.network.bridge.name={BRIDGE}", NETWORK, timeout=60)
        if rc:
            raise RuntimeError("cannot create docker network: " + out[-200:])
        notes.append(f"network {NETWORK} created")
    return " · ".join(notes)


ensure_docker = ensure_lxd


async def diagnose():
    """Collect what is going on when a create step hangs. Returns a short text (also printed to the log)."""
    parts = []
    for label, cmd in (
        ("docker", "docker version --format '{{.Server.Version}}' 2>&1 | head -2"),
        ("storage driver", "docker info --format '{{.Driver}} / {{.DockerRootDir}}' 2>&1"),
        ("running vps", "docker ps --filter label=lb.vps=1 --format '{{.Names}}' 2>&1 | head -10 | tr '\\n' ' '"),
        ("builds", "ps -eo pid,etime,args | grep -E 'docker (build|run|pull)' | grep -v grep | head -5"),
        ("disk free", "df -h / | tail -1"),
        ("ipv4 to docker hub", "curl -4 -sS -m 10 -o /dev/null -w '%{http_code}' https://registry-1.docker.io/v2/ 2>&1"),
        ("ipv4 to sshx", "curl -4 -sS -m 10 -o /dev/null -w '%{http_code}' https://sshx.io/get 2>&1"),
    ):
        rc, out = await sh("bash", "-c", cmd, timeout=30)
        parts.append(f"{label}: {(out or '-')[-160:]}")
    text = " | ".join(parts)
    print("DIAG:", text, flush=True)
    return text


# ---------------------------------------------------------------- OS images (built once, cached by Docker)
_IMG_LOCKS = {}


def _tag(os_key):
    return f"lb-vps:{os_key}"


async def _ensure_image(os_key, P):
    """Build (once) the VPS image for an OS: base image + tools + sshx + entrypoint. Returns the image tag."""
    if os_key not in IMAGES:
        raise ValueError("unknown OS")
    tag = _tag(os_key)
    lock = _IMG_LOCKS.setdefault(os_key, asyncio.Lock())
    async with lock:
        rc, _ = await sh("docker", "image", "inspect", tag, timeout=30)
        if rc == 0:
            return tag
        label, base = IMAGES[os_key]
        await P(8, f"Downloading + building {label} image (first time only)")
        last = ""
        for attempt in (1, 2):
            rc, last = await sh("docker", "build", "-t", tag, "-", inp=dockerfile(base), timeout=IMAGE_TIMEOUT)
            if rc == 0:
                return tag
            await asyncio.sleep(5)
        raise RuntimeError(f"image build failed for {label}: " + last[-300:])


# ---------------------------------------------------------------- disk limit (--storage-opt size)
QUOTA_OK = False
QUOTA_TRIED = False
QUOTA_NOTE = "disk limit not checked yet"
_QUOTA_LOCK = None


async def ensure_quota_pool():
    """Docker can cap a container's disk (--storage-opt size=) only on overlay2 over xfs with pquota (or zfs/btrfs).
    We test it once with a real container. Never raises. Returns a short status text."""
    global _QUOTA_LOCK, QUOTA_TRIED, QUOTA_OK, QUOTA_NOTE
    if _QUOTA_LOCK is None:
        _QUOTA_LOCK = asyncio.Lock()
    async with _QUOTA_LOCK:
        if QUOTA_TRIED:
            return QUOTA_NOTE
        try:
            async def _np(*a):
                pass
            tag = await _ensure_image("ubuntu22", _np)
            await sh("docker", "rm", "-f", "lb-selftest")
            rc, out = await sh("docker", "run", "--rm", "--name", "lb-selftest", "--storage-opt", "size=2G",
                               "--entrypoint", "true", tag, timeout=120)
            _, drv = await sh("docker", "info", "--format", "{{.Driver}}")
            if rc == 0:
                QUOTA_OK, QUOTA_NOTE = True, f"disk limits enforced ({drv})"
            else:
                QUOTA_OK = False
                QUOTA_NOTE = (f"WARNING: disk limit NOT enforceable on this server (storage driver {drv}). "
                              f"Use overlay2 on xfs with 'pquota' for per-VPS disk caps. ({out[-120:]})")
        except Exception as e:
            QUOTA_OK, QUOTA_NOTE = False, f"WARNING: disk limit check failed: {e}"
        QUOTA_TRIED = True
        return QUOTA_NOTE


async def prewarm_images():
    """Build every OS image once, in the background, so the first VPS of each OS is created fast.
    Never raises; a failed image is simply built on demand when someone deploys it."""
    async def _np(*x):
        pass
    for key in IMAGES:
        try:
            await _ensure_image(key, _np)
            print(f"image {key} ready", flush=True)
        except Exception as e:
            print(f"image {key} pre-build failed (will retry on demand): {str(e)[:200]}", flush=True)


# ---------------------------------------------------------------- resource check before creating a VPS
def _host_mem():
    try:
        with open("/proc/meminfo") as f:
            kv = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in f if ":" in l}
        return kv["MemTotal"], kv.get("MemAvailable", kv.get("MemFree", 0))
    except Exception:
        return 0, 0


async def _allocated():
    """(RAM bytes, CPUs) already promised to running VPS containers on this host."""
    rc, ids = await sh("docker", "ps", "--filter", "label=lb.vps=1", "-q", timeout=30)
    if rc or not ids.split():
        return 0, 0.0
    rc, out = await sh("docker", "inspect", "--format", "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}", *ids.split(), timeout=60)
    mem = cpu = 0.0
    for line in (out.splitlines() if rc == 0 else []):
        try:
            m, c = line.split()
            mem += int(m)
            cpu += int(c) / 1e9
        except Exception:
            pass
    return int(mem), cpu


async def check_capacity(cpu, ram, disk):
    """Refuse to create a VPS the host cannot really run. Raises RuntimeError with a clear reason.
    Env: MIN_FREE_RAM_MB (512) · MIN_FREE_DISK_GB (5) · RAM_OVERCOMMIT (0 = off; 1.0 = never promise more RAM
    than the server has; 1.5 = allow 50% overcommit) · CPU_OVERCOMMIT (0 = off)."""
    total, avail = _host_mem()
    cores = os.cpu_count() or 1
    free_disk = shutil.disk_usage("/").free
    ram_b = int(ram) * 1024**3
    if total and ram_b > total:
        raise RuntimeError(f"this server only has {total / 1024**3:.1f}GB RAM - cannot create a {int(ram)}GB VPS here")
    if int(cpu) > cores:
        raise RuntimeError(f"this server only has {cores} CPU cores - cannot create a {int(cpu)} CPU VPS here")
    min_ram = int(os.getenv("MIN_FREE_RAM_MB", "512")) * 1024**2
    if total and avail < min_ram:
        raise RuntimeError(f"server is low on RAM ({avail / 1024**2:.0f}MB free) - try again later or use another node")
    min_disk = int(os.getenv("MIN_FREE_DISK_GB", "5")) * 1024**3
    if free_disk < min_disk:
        raise RuntimeError(f"server is low on disk ({free_disk / 1024**3:.1f}GB free) - try again later or use another node")
    if QUOTA_TRIED and not QUOTA_OK and int(disk) * 1024**3 > free_disk:
        raise RuntimeError(f"not enough free disk for a {int(disk)}GB VPS ({free_disk / 1024**3:.1f}GB free)")
    ro = float(os.getenv("RAM_OVERCOMMIT", "0") or 0)
    co = float(os.getenv("CPU_OVERCOMMIT", "0") or 0)
    if ro or co:
        used_ram, used_cpu = await _allocated()
        if ro and total and used_ram + ram_b > total * ro:
            raise RuntimeError(f"RAM fully allocated: {used_ram / 1024**3:.0f}GB already promised to running VPS "
                               f"(+{int(ram)}GB would pass the {total * ro / 1024**3:.0f}GB limit)")
        if co and used_cpu + int(cpu) > cores * co:
            raise RuntimeError(f"CPU fully allocated: {used_cpu:.0f} cores already promised to running VPS")


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

    await P(3, "Checking server resources (RAM / CPU / disk)")
    await check_capacity(cpu, ram, disk)
    tag = await _ensure_image(os_key, P)
    await ensure_lxd()  # makes sure the network exists
    if not QUOTA_TRIED:
        await P(18, "Checking disk-limit support (first time only)")
        await ensure_quota_pool()
    if not QUOTA_OK and os.getenv("REQUIRE_DISK_LIMIT"):
        raise RuntimeError("disk limit cannot be enforced on this server: " + QUOTA_NOTE)
    await P(22, "Creating container")
    await sh("docker", "rm", "-f", name)
    args = ["docker", "run", "-d", "--name", name, "--hostname", name, "--init",
            "--label", "lb.vps=1", "--restart", "unless-stopped",
            "--network", NETWORK, "--dns", "1.1.1.1", "--dns", "8.8.8.8",
            "--cpus", str(int(cpu)), "--memory", f"{int(ram)}g", "--memory-swap", f"{int(ram)}g",
            "--pids-limit", "4096", "--security-opt", "no-new-privileges",
            "--sysctl", "net.ipv6.conf.all.disable_ipv6=1"]
    if QUOTA_OK:
        args += ["--storage-opt", f"size={int(disk)}G"]
    args.append(tag)
    rc, out = await sh(*args, timeout=180)
    if rc:
        await sh("docker", "rm", "-f", name)
        hint = " :: " + await diagnose() if out == "timeout" else ""
        raise RuntimeError("launch failed: " + out[-300:] + hint)
    try:
        await P(40, "Waiting for container network")
        for _ in range(40):
            rc, _o = await ex(name, "getent hosts github.com", timeout=15)
            if rc == 0:
                break
            await asyncio.sleep(3)
        else:
            raise RuntimeError("container has no internet/DNS (host firewall / ufw / FORWARD rule?)")
        await P(60, "Starting SSHX terminal (container root access)")
        link = await _get_sshx(name, tries=30)
        if not link:
            raise RuntimeError("sshx started but gave no link (check container internet / sshx.io)")
        await P(95, "Finishing")
        return {"sshx": link}
    except BaseException:  # includes cancellation/timeouts: never leave a half-built container behind
        await sh("docker", "rm", "-f", name)
        raise


async def op_action(name, action):
    _chk(name)
    cmds = {
        "start": ["docker", "start", name],
        "stop": ["docker", "stop", "-t", "5", name],
        "restart": ["docker", "restart", "-t", "5", name],
        "delete": ["docker", "rm", "-f", "-v", name],
    }
    if action not in cmds:
        raise ValueError("bad action")
    rc, out = await sh(*cmds[action], timeout=180)
    if rc:
        raise RuntimeError(out[-300:])
    return {"ok": True}


async def op_ssh(name, reset=False):
    _chk(name)
    if reset:
        # the supervisor loop inside the container restarts sshx a few seconds later with a fresh link
        await ex(name, "rm -f /root/.sshx_link; pkill -x sshx", timeout=30)
        await asyncio.sleep(3)
    link = await _get_sshx(name, tries=15)
    if not link:
        raise RuntimeError("no sshx link (is the VPS running and online?)")
    return {"sshx": link}


async def _ps():
    rc, out = await sh("docker", "ps", "-a", "--filter", "label=lb.vps=1", "--format", "{{json .}}")
    if rc:
        raise RuntimeError(out[-200:])
    rows = []
    for line in out.splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return [r for r in rows if str(r.get("Names", "")).startswith("vps-")]


async def op_status():
    cs = await _ps()
    return {"count": len(cs), "running": sum(c.get("State") == "running" for c in cs)}


async def op_usage(name):
    _chk(name)
    rc, out = await sh("docker", "inspect", "--size", name)
    if rc:
        raise RuntimeError("container not found")
    c = json.loads(out)[0]
    running = c["State"]["Status"] == "running"
    ips = [n["IPAddress"] for n in (c["NetworkSettings"].get("Networks") or {}).values() if n.get("IPAddress")]
    mem = cpu_s = procs = 0
    if running:
        rc, o = await ex(name,
                         "m=$(cat /sys/fs/cgroup/memory.current 2>/dev/null || cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null); "
                         "u=$(awk '/usage_usec/{print $2/1e6}' /sys/fs/cgroup/cpu.stat 2>/dev/null || "
                         "(awk '{print $1/1e9}' /sys/fs/cgroup/cpuacct/cpuacct.usage 2>/dev/null)); "
                         "p=$(ls /proc | grep -c '^[0-9]'); echo \"${m:-0} ${u:-0} ${p:-0}\"", timeout=20)
        try:
            m, u, p = o.split()[-3:]
            mem, cpu_s, procs = int(float(m)), float(u), int(p)
        except Exception:
            pass
    return {
        "status": "Running" if running else "Stopped",
        "mem": mem,
        "disk": c.get("SizeRw") or 0,
        "cpu_s": cpu_s,
        "procs": procs,
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
    for c in await _ps():
        if c.get("State") != "running":
            continue
        name = c["Names"]
        rc, ps = await ex(name, "ps -eo comm=,args=", timeout=30)
        m = MINER_RE.search(ps) if rc == 0 else None
        if m:
            await sh("docker", "stop", "-t", "2", name)
            EVENTS.append({"name": name, "match": m.group(0)})


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
    """Outbound-abuse limits for VPS containers + SYN flood limits + hide the host from containers.
    Needs network_mode: host + NET_ADMIN (the compose file has both). Best effort: never fatal."""
    await sh("sysctl", "-w", "net.ipv4.tcp_syncookies=1")
    b = BRIDGE
    await sh("iptables", "-N", "DOCKER-USER")  # normally exists already
    # traffic leaving containers (anti-mining pools, spam, flood tools); DOCKER-USER is the chain Docker keeps for admins
    await _chain("LBBOT_FWD", ["DOCKER-USER", "-i", b, "-j", "LBBOT_FWD"], [
        ["-p", "tcp", "-m", "multiport", "--dports",
         "25,465,587,3333,4444,5555,6666,7777,9999,14433,14444,45560,45700", "-j", "DROP"],
        ["-p", "tcp", "--syn", *_hl("lb_syn", "200/sec", 400)],
        ["-p", "udp", *_hl("lb_udp", "5000/sec", 8000)],
        ["-p", "icmp", *_hl("lb_icmp", "50/sec", 100)],
    ])
    # containers may not talk to services on the host itself (only replies to what the host started)
    await _chain("LBBOT_IN", ["INPUT", "-j", "LBBOT_IN"], [
        ["-i", b, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
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
        print("docker setup / protection failed:", e)
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
