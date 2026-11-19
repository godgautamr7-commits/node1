#!/usr/bin/env python3
"""KVM node backend: every VPS is a real virtual machine (own kernel) managed by libvirt.
Used by node/agent.py and by the bot's node1 - not started directly.

- Images: official cloud images (Ubuntu 22.04/24.04, Debian 11/12), downloaded once, VPS disks are qcow2
  overlays whose virtual size = the plan's disk (hard disk limit). RAM / vCPU are fixed by the VM definition.
- Access: cloud-init puts the node's own SSH key into root; the node then installs sshx inside the VM and
  hands the web-terminal link to the bot (root of the VM only, never the host).
- Works on a normal host (libvirt installed automatically) and also from inside the bot's Docker container
  when the host's libvirt socket + /var/lib/lb-kvm are mounted (see docker-compose.yml).
Same interface as the other backends: ensure_lxd, apply_protection, prewarm_images, ensure_quota_pool,
watchdog_loop, OPS, IMAGES, _ensure_image, diagnose.
"""
import asyncio
import os
import re
import shlex
import shutil

from . import lxd_backend as L  # shared helpers: sh(), MINER_RE, SSHX_UNIT, firewall chains, EVENTS

sh = L.sh
EVENTS = L.EVENTS
NAME_RE = L.NAME_RE
MINER_RE = L.MINER_RE
SSHX_UNIT = L.SSHX_UNIT

BASE = os.getenv("KVM_DIR", "/var/lib/lb-kvm")
IMG_DIR = os.path.join(BASE, "images")
VM_DIR = os.path.join(BASE, "vms")
KEY = os.path.join(BASE, "keys", "id_ed25519")
NETWORK = os.getenv("KVM_NETWORK", "default")
BRIDGE = os.getenv("KVM_BRIDGE", "virbr0")
IMAGE_TIMEOUT = int(os.getenv("IMAGE_TIMEOUT", "1800"))
BOOT_TIMEOUT = int(os.getenv("KVM_BOOT_TIMEOUT", "300"))
APT = "export DEBIAN_FRONTEND=noninteractive; "
CONN = ["virsh", "-c", "qemu:///system"]

# key -> (label, cloud image url)
IMAGES = {
    "ubuntu22": ("Ubuntu 22.04", "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"),
    "ubuntu24": ("Ubuntu 24.04", "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"),
    "debian11": ("Debian 11", "https://cloud.debian.org/images/cloud/bullseye/latest/debian-11-generic-amd64.qcow2"),
    "debian12": ("Debian 12", "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2"),
}


def virsh(*a, **kw):
    return sh(*CONN, *a, **kw)


def _chk(name):
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError("bad vm name")


def _in_container():
    try:
        from detect import in_container
        return in_container()
    except Exception:
        return os.path.exists("/.dockerenv")


# ---------------------------------------------------------------- setup
async def _virsh_ok():
    if not shutil.which("virsh"):
        return False
    rc, _ = await virsh("version", timeout=30)
    return rc == 0


async def ensure_lxd():
    """(name kept for the bot) Install/prepare libvirt + the default NAT network + the node's SSH key.
    Returns a short log ('' = nothing to do)."""
    notes = []
    if not await _virsh_ok():
        if _in_container():
            raise RuntimeError("cannot reach libvirt from inside this container - install libvirt on the host "
                               "(apt install qemu-system-x86 libvirt-daemon-system) and mount /var/run/libvirt")
        if os.geteuid() != 0:
            raise RuntimeError("must run as root")
        if not os.path.exists("/dev/kvm"):
            raise RuntimeError("/dev/kvm is missing - this server has no hardware virtualization "
                               "(enable VT-x/AMD-V, or nested virtualization at your provider)")
        rc, out = await sh("bash", "-c", APT + "apt-get update -qq && apt-get install -y -qq qemu-system-x86 qemu-utils "
                           "libvirt-daemon-system libvirt-clients genisoimage openssh-client iptables dnsmasq-base curl",
                           timeout=1800)
        if rc:
            raise RuntimeError("KVM / libvirt install failed: " + out[-300:])
        await sh("systemctl", "enable", "--now", "libvirtd", timeout=90)
        await sh("systemctl", "enable", "--now", "libvirtd.socket", timeout=90)
        for _ in range(15):
            if await _virsh_ok():
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError("libvirt did not start (check: systemctl status libvirtd)")
        notes.append("KVM + libvirt installed")
    for tool in ("qemu-img", "genisoimage", "ssh", "ssh-keygen", "curl"):
        if not shutil.which(tool):
            if _in_container():
                raise RuntimeError(f"{tool} missing in this image (rebuild the bot image)")
            await sh("bash", "-c", APT + "apt-get install -y -qq qemu-utils genisoimage openssh-client curl", timeout=900)
            break
    # default NAT network (virbr0, 192.168.122.0/24 with DHCP)
    rc, out = await virsh("net-info", NETWORK, timeout=30)
    if rc:
        xml = ("<network><name>default</name><forward mode='nat'/><bridge name='virbr0' stp='on' delay='0'/>"
               "<ip address='192.168.122.1' netmask='255.255.255.0'><dhcp><range start='192.168.122.2' "
               "end='192.168.122.254'/></dhcp></ip></network>")
        tmp = os.path.join(BASE, "default-net.xml")
        os.makedirs(BASE, exist_ok=True)
        open(tmp, "w").write(xml)
        rc, out = await virsh("net-define", tmp, timeout=30)
        if rc:
            raise RuntimeError("cannot define libvirt network: " + out[-200:])
        rc, out = await virsh("net-info", NETWORK, timeout=30)
    if "Active:" in out and re.search(r"Active:\s+no", out):
        rc, o2 = await virsh("net-start", NETWORK, timeout=60)
        if rc:
            raise RuntimeError("cannot start libvirt network: " + o2[-200:])
        notes.append("network started")
    await virsh("net-autostart", NETWORK, timeout=30)
    for d in (BASE, IMG_DIR, VM_DIR, os.path.dirname(KEY)):
        os.makedirs(d, exist_ok=True)
    os.chmod(BASE, 0o755)
    os.chmod(IMG_DIR, 0o755)
    os.chmod(VM_DIR, 0o755)
    os.chmod(os.path.dirname(KEY), 0o700)
    if not os.path.exists(KEY):
        rc, out = await sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", KEY, "-C", "lb-kvm-node")
        if rc:
            raise RuntimeError("ssh-keygen failed: " + out[-150:])
        notes.append("ssh key created")
    if not _in_container():
        await sh("sysctl", "-w", "net.ipv4.ip_forward=1")
    return " · ".join(notes)


ensure_kvm = ensure_lxd


async def diagnose():
    parts = []
    for label, cmd in (
        ("kvm device", "ls -l /dev/kvm 2>&1"),
        ("libvirt", "virsh -c qemu:///system version 2>&1 | head -3 | tr '\\n' ' '"),
        ("vms", "virsh -c qemu:///system list --all 2>&1 | head -8"),
        ("network", f"virsh -c qemu:///system net-info {NETWORK} 2>&1 | head -4 | tr '\\n' ' '"),
        ("disk free", f"df -h {BASE} 2>/dev/null | tail -1"),
        ("ubuntu cloud images", "curl -4 -sS -m 10 -o /dev/null -w '%{http_code}' https://cloud-images.ubuntu.com 2>&1"),
        ("sshx.io", "curl -4 -sS -m 10 -o /dev/null -w '%{http_code}' https://sshx.io/get 2>&1"),
    ):
        rc, out = await sh("bash", "-c", cmd, timeout=30)
        parts.append(f"{label}: {(out or '-')[-160:]}")
    text = " | ".join(parts)
    print("DIAG:", text, flush=True)
    return text


# ---------------------------------------------------------------- images (downloaded once)
_IMG_LOCKS = {}


def _img_path(os_key):
    return os.path.join(IMG_DIR, f"{os_key}.qcow2")


async def _ensure_image(os_key, P):
    if os_key not in IMAGES:
        raise ValueError("unknown OS")
    path = _img_path(os_key)
    lock = _IMG_LOCKS.setdefault(os_key, asyncio.Lock())
    async with lock:
        if os.path.exists(path) and os.path.getsize(path) > 50 * 1024 * 1024:
            return path
        label, url = IMAGES[os_key]
        await P(8, f"Downloading {label} cloud image (first time only)")
        os.makedirs(IMG_DIR, exist_ok=True)
        tmp = path + ".part"
        last = ""
        for _ in range(2):
            rc, last = await sh("curl", "-4", "-fL", "--retry", "3", "--connect-timeout", "30", "-o", tmp, url,
                                timeout=IMAGE_TIMEOUT)
            if rc == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 50 * 1024 * 1024:
                os.replace(tmp, path)
                os.chmod(path, 0o644)
                return path
            await asyncio.sleep(5)
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(f"image download failed for {label}: " + last[-250:])


async def prewarm_images():
    async def _np(*a):
        pass
    want = [k.strip() for k in os.getenv("KVM_PREWARM", ",".join(IMAGES)).split(",") if k.strip() in IMAGES]
    for key in want:
        try:
            await _ensure_image(key, _np)
            print(f"image {key} ready", flush=True)
        except Exception as e:
            print(f"image {key} pre-download failed (will retry on demand): {str(e)[:200]}", flush=True)


# ---------------------------------------------------------------- disk limit = qcow2 virtual size
QUOTA_OK = True
QUOTA_TRIED = True
QUOTA_NOTE = "disk limit enforced: each VPS has a fixed-size virtual disk (qcow2)"


async def ensure_quota_pool():
    return QUOTA_NOTE


# ---------------------------------------------------------------- talking to the VM (ssh)
_IPS = {}


async def _ip(name, tries=1):
    for _ in range(tries):
        rc, out = await virsh("domifaddr", name, "--source", "lease", timeout=20)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", out) if rc == 0 else None
        if m:
            _IPS[name] = m.group(1)
            return m.group(1)
        await asyncio.sleep(3)
    return ""


async def ex(name, cmd, timeout=120, **kw):
    ip = _IPS.get(name) or await _ip(name)
    if not ip:
        return 255, "vm has no ip yet (not running / still booting)"
    rc, out = await sh("ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                       "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
                       f"root@{ip}", "bash -c " + shlex.quote("export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; " + cmd),
                       timeout=timeout)
    if rc == 255:
        _IPS.pop(name, None)  # ip may have changed after a restart
    return rc, out


async def _get_sshx(name, tries=20):
    for _ in range(tries):
        rc, out = await ex(name, "cat /root/.sshx_link 2>/dev/null", timeout=25)
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
        timeout=420)
    if rc:
        raise RuntimeError("sshx install failed (VM cannot reach sshx.io): " + out[-150:])
    unit = SSHX_UNIT.replace("'", "'\"'\"'")
    await ex(name, f"printf '%s' '{unit}' > /etc/systemd/system/sshx.service && systemctl daemon-reload "
                   "&& systemctl enable sshx && systemctl restart sshx", timeout=60)
    link = await _get_sshx(name)
    if not link:
        raise RuntimeError("sshx started but gave no link (check VM internet / sshx.io)")
    return link


# ---------------------------------------------------------------- capacity
def _mem_available_mb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


async def check_capacity(cpu, ram, disk):
    os.makedirs(BASE, exist_ok=True)
    free_gb = shutil.disk_usage(BASE).free / 2**30
    min_disk = float(os.getenv("MIN_FREE_DISK_GB", "5"))
    if free_gb < min_disk + 2:  # qcow2 is sparse; keep a safety margin instead of reserving the full plan
        raise RuntimeError(f"not enough free disk on the node ({free_gb:.0f} GB free, need {min_disk + 2:.0f} GB spare)")
    if (os.cpu_count() or 1) < cpu:
        raise RuntimeError(f"node has only {os.cpu_count()} CPU cores, plan needs {cpu}")
    need = int(ram) * 1024 + int(os.getenv("MIN_FREE_RAM_MB", "512"))
    if _mem_available_mb() < need:
        raise RuntimeError(f"not enough free RAM on the node ({_mem_available_mb()} MB free, VM needs {need} MB)")


# ---------------------------------------------------------------- operations
def _domain_xml(name, cpu, ram, disk_path, seed_path):
    return f"""<domain type='kvm'>
  <name>{name}</name>
  <memory unit='MiB'>{int(ram) * 1024}</memory>
  <currentMemory unit='MiB'>{int(ram) * 1024}</currentMemory>
  <vcpu>{int(cpu)}</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type><boot dev='hd'/></os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff><on_reboot>restart</on_reboot><on_crash>restart</on_crash>
  <devices>
    <disk type='file' device='disk'><driver name='qemu' type='qcow2' cache='none'/><source file='{disk_path}'/><target dev='vda' bus='virtio'/></disk>
    <disk type='file' device='cdrom'><driver name='qemu' type='raw'/><source file='{seed_path}'/><target dev='sda' bus='sata'/><readonly/></disk>
    <interface type='network'><source network='{NETWORK}'/><model type='virtio'/></interface>
    <serial type='pty'/><console type='pty'/>
    <rng model='virtio'><backend model='random'>/dev/urandom</backend></rng>
  </devices>
</domain>
"""


async def _destroy(name):
    await virsh("destroy", name)
    await virsh("undefine", name, "--nvram", "--managed-save", "--snapshots-metadata")
    shutil.rmtree(os.path.join(VM_DIR, name), ignore_errors=True)
    _IPS.pop(name, None)


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
    await ensure_lxd()
    await check_capacity(cpu, ram, disk)
    base_img = await _ensure_image(os_key, P)
    await P(20, "Creating virtual machine disk")
    await _destroy(name)
    vdir = os.path.join(VM_DIR, name)
    os.makedirs(vdir, exist_ok=True)
    os.chmod(vdir, 0o755)
    disk_path, seed_path = os.path.join(vdir, "disk.qcow2"), os.path.join(vdir, "seed.iso")
    try:
        rc, out = await sh("qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", base_img, disk_path, f"{int(disk)}G", timeout=120)
        if rc:
            raise RuntimeError("disk create failed: " + out[-250:])
        pub = open(KEY + ".pub").read().strip()
        open(os.path.join(vdir, "user-data"), "w").write(
            "#cloud-config\n"
            f"hostname: {name}\n"
            "manage_etc_hosts: true\n"
            "disable_root: false\n"
            "ssh_pwauth: false\n"
            "users:\n"
            "  - name: root\n"
            "    ssh_authorized_keys:\n"
            f"      - {pub}\n")
        open(os.path.join(vdir, "meta-data"), "w").write(f"instance-id: {name}\nlocal-hostname: {name}\n")
        rc, out = await sh("genisoimage", "-quiet", "-output", seed_path, "-volid", "cidata", "-joliet", "-rock",
                           os.path.join(vdir, "user-data"), os.path.join(vdir, "meta-data"), timeout=60)
        if rc:
            raise RuntimeError("cloud-init seed failed: " + out[-250:])
        for f in (disk_path, seed_path):
            os.chmod(f, 0o644)
        xml_path = os.path.join(vdir, "domain.xml")
        open(xml_path, "w").write(_domain_xml(name, cpu, ram, disk_path, seed_path))
        await P(30, "Defining virtual machine")
        rc, out = await virsh("define", xml_path, timeout=60)
        if rc:
            raise RuntimeError("launch failed: " + out[-300:])
        await virsh("autostart", name)
        await P(35, "Booting virtual machine")
        rc, out = await virsh("start", name, timeout=120)
        if rc:
            raise RuntimeError("start failed: " + out[-300:])
        await P(45, "Waiting for the VM to boot (cloud-init)")
        ip, ok, last = "", False, ""
        for _ in range(BOOT_TIMEOUT // 5):
            ip = ip or await _ip(name)
            if ip:
                rc, last = await ex(name, "cloud-init status --wait >/dev/null 2>&1; true", timeout=120)
                if rc == 0:
                    ok = True
                    break
            await asyncio.sleep(5)
        if not ok:
            raise RuntimeError("VM did not become reachable by SSH in time" + (f" ({last[-120:]})" if last else "")
                               + " :: " + await diagnose())
        await P(65, "Checking VM internet")
        for _ in range(30):
            rc, _o = await ex(name, "getent hosts github.com", timeout=20)
            if rc == 0:
                break
            await asyncio.sleep(3)
        else:
            raise RuntimeError("VM has no internet/DNS (host firewall / forward rule / libvirt network?)")
        await P(75, "Installing SSHX terminal (VM root access)")
        link = await _install_sshx(name)
        await P(95, "Finishing")
        return {"sshx": link}
    except BaseException:  # never leave a half-built VM behind
        await _destroy(name)
        raise


async def op_action(name, action):
    _chk(name)
    if action not in ("start", "stop", "restart", "delete"):
        raise ValueError("bad action")
    if action == "delete":
        await _destroy(name)
        return {"ok": True}

    async def stop():
        await virsh("shutdown", name, timeout=30)
        for _ in range(8):
            rc, st = await virsh("domstate", name, timeout=15)
            if "running" not in st:
                break
            await asyncio.sleep(2)
        else:
            await virsh("destroy", name, timeout=30)
        _IPS.pop(name, None)

    if action in ("stop", "restart"):
        await stop()
    if action in ("start", "restart"):
        rc, out = await virsh("start", name, timeout=120)
        if rc and "already active" not in out:
            raise RuntimeError(out[-300:])
    return {"ok": True}


async def op_ssh(name, reset=False):
    _chk(name)
    if reset or not await _get_sshx(name, tries=1):
        return {"sshx": await _install_sshx(name)}
    return {"sshx": await _get_sshx(name, tries=8)}


async def _names(running_only=False):
    args = ["list", "--name"] + ([] if running_only else ["--all"])
    rc, out = await virsh(*args, timeout=30)
    if rc:
        raise RuntimeError(out[-200:])
    return [n.strip() for n in out.splitlines() if n.strip().startswith("vps-")]


async def op_status():
    return {"count": len(await _names()), "running": len(await _names(True))}


async def op_usage(name):
    _chk(name)
    rc, st = await virsh("domstate", name, timeout=20)
    if rc:
        raise RuntimeError("vm not found")
    running = "running" in st
    mem = cpu_s = 0
    ips = []
    if running:
        rc, out = await virsh("domstats", "--cpu-total", "--balloon", name, timeout=20)
        d = dict(re.findall(r"^\s*([\w.]+)=(\S+)", out, re.M)) if rc == 0 else {}
        mem = int(d.get("balloon.rss", d.get("balloon.current", 0)) or 0) * 1024
        cpu_s = int(d.get("cpu.time", 0) or 0) / 1e9
        ip = await _ip(name)
        ips = [ip] if ip else []
    rc, info = await sh("qemu-img", "info", "--force-share", os.path.join(VM_DIR, name, "disk.qcow2"), timeout=20)
    m = re.search(r"disk size:\s*([\d.]+)\s*(\w+)", info) if rc == 0 else None
    mult = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}
    disk = int(float(m.group(1)) * mult.get(m.group(2), 1)) if m else 0
    return {"status": "Running" if running else "Stopped", "mem": mem, "disk": disk, "cpu_s": cpu_s, "procs": 0, "ips": ips}


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
    for n in await _names(True):
        rc, ps = await ex(n, "ps -eo comm=,args=", timeout=40)
        m = MINER_RE.search(ps) if rc == 0 else None
        if m:
            await virsh("destroy", n)
            EVENTS.append({"name": n, "match": m.group(0)})


async def watchdog_loop(interval=60):
    while True:
        try:
            await scan_once()
        except Exception as e:
            print("watchdog error:", e)
        await asyncio.sleep(interval)


async def apply_protection():
    """Same outbound-abuse / SYN-flood rules as the other backends, applied to the libvirt bridge virbr0."""
    L.BRIDGE = BRIDGE
    await L.apply_protection()
