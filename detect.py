#!/usr/bin/env python3
"""Runtime auto-detection for nodes AND for the bot's own machine (node1). Standard library only.

Which VPS runtimes can this machine run?   kvm (real virtual machines) | lxd | docker | lxc (classic)

Rules
  * A runtime is "supported" if it already works here, or if it can be installed automatically here.
  * auto picks the best supported one, best first:   kvm  >  lxd  >  docker  >  lxc
        (kvm = full VM with its own kernel; lxd = system containers with disk limits;
         docker = app containers; lxc = classic containers)
  * KVM not supported            -> falls back to LXD / Docker / LXC (best of what is left)
  * LXD and KVM not supported    -> Docker, and so on. If nothing at all is detected: Docker.
  * NODE_TYPE=kvm|lxd|docker|lxc forces one (auto = detect).

CLI:   python3 detect.py            -> prints the chosen type
       python3 detect.py --report   -> prints a readable report
       python3 detect.py --json     -> machine readable
"""
import json
import os
import platform
import shutil
import subprocess
import sys

PRIORITY = ("kvm", "lxd", "docker", "lxc")  # best first
TYPES = PRIORITY


def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:  # missing binary, timeout ...
        return 127, str(e)


def in_container():
    """True when this process itself runs inside a container (e.g. the bot in Docker)."""
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    rc, out = _run("systemd-detect-virt -c")
    if rc == 0 and out and out != "none":
        return True
    try:
        txt = open("/proc/1/cgroup").read()
        return any(k in txt for k in ("docker", "kubepods", "containerd", "/lxc/"))
    except OSError:
        return False


def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _installable_base():
    """Can we install packages automatically on this machine?"""
    if in_container() or not _is_root():
        return False
    return bool(shutil.which("apt-get") or shutil.which("dnf") or shutil.which("yum"))


def _has_systemd():
    return os.path.isdir("/run/systemd/system")


def _virsh_ok():
    if not shutil.which("virsh"):
        return False
    return _run("virsh -c qemu:///system version", timeout=20)[0] == 0


def check_kvm():
    if platform.machine().lower() not in ("x86_64", "amd64"):
        return {"supported": False, "ready": False, "reason": f"cpu is {platform.machine()} (KVM images are x86_64)"}
    if in_container():
        # inside a container (the bot in Docker): only possible through the host's libvirt socket
        if _virsh_ok():
            return {"supported": True, "ready": True, "reason": "host libvirt reachable through its socket"}
        return {"supported": False, "ready": False,
                "reason": "in a container and no host libvirt socket (install libvirt on the host, then restart)"}
    if not os.path.exists("/dev/kvm"):
        flags = ""
        try:
            flags = open("/proc/cpuinfo").read()
        except OSError:
            pass
        if _is_root() and (" vmx" in flags or " svm" in flags):
            for mod in ("kvm_intel", "kvm_amd"):
                _run(["modprobe", mod])
                if os.path.exists("/dev/kvm"):
                    break
    if not os.path.exists("/dev/kvm"):
        return {"supported": False, "ready": False,
                "reason": "/dev/kvm missing (no hardware virtualization / nested virtualization is off)"}
    if _virsh_ok():
        return {"supported": True, "ready": True, "reason": "/dev/kvm + libvirt working"}
    if _installable_base() and _has_systemd():
        return {"supported": True, "ready": False, "reason": "/dev/kvm present, libvirt will be installed"}
    return {"supported": False, "ready": False, "reason": "/dev/kvm present but libvirt cannot be installed here"}


def check_lxd():
    if shutil.which("lxc") and shutil.which("lxd"):
        if _run("lxc info", timeout=30)[0] == 0:
            return {"supported": True, "ready": True, "reason": "LXD installed and working"}
    if in_container():
        return {"supported": False, "ready": False, "reason": "inside a container - LXD needs the host"}
    if not _is_root():
        return {"supported": False, "ready": False, "reason": "not root"}
    if _has_systemd() and (shutil.which("snap") or shutil.which("apt-get")):
        return {"supported": True, "ready": False, "reason": "LXD will be installed (snap)"}
    return {"supported": False, "ready": False, "reason": "no systemd / snap / apt - cannot install LXD"}


def check_docker():
    if shutil.which("docker"):
        if _run("docker info --format '{{.ServerVersion}}'", timeout=30)[0] == 0:
            return {"supported": True, "ready": True, "reason": "Docker installed and working"}
        if in_container():
            return {"supported": False, "ready": False, "reason": "docker CLI there, but no reachable docker.sock"}
    elif in_container():
        return {"supported": False, "ready": False, "reason": "inside a container without docker.sock"}
    if _is_root() and (shutil.which("curl") or shutil.which("wget") or shutil.which("apt-get")):
        return {"supported": True, "ready": False, "reason": "Docker will be installed (get.docker.com)"}
    return {"supported": False, "ready": False, "reason": "cannot install Docker here (need root + internet tools)"}


def check_lxc():
    if shutil.which("lxc-create") and shutil.which("lxc-ls"):
        return {"supported": True, "ready": True, "reason": "classic LXC installed"}
    if _installable_base() and shutil.which("apt-get"):
        return {"supported": True, "ready": False, "reason": "LXC will be installed (apt)"}
    return {"supported": False, "ready": False, "reason": "cannot install classic LXC here"}


CHECKS = {"kvm": check_kvm, "lxd": check_lxd, "docker": check_docker, "lxc": check_lxc}


def capabilities():
    out = {}
    for k in PRIORITY:
        try:
            out[k] = CHECKS[k]()
        except Exception as e:
            out[k] = {"supported": False, "ready": False, "reason": f"check failed: {str(e)[:80]}"}
    return out


def choose(want=None, caps=None):
    """Returns (type, report_dict). want = 'auto' | one of TYPES (default: env NODE_TYPE)."""
    want = (want or os.getenv("NODE_TYPE", "auto") or "auto").strip().lower()
    if want != "auto" and want not in TYPES:
        raise ValueError(f"NODE_TYPE must be auto, {', '.join(TYPES)} (got '{want}')")
    caps = caps or capabilities()
    if want != "auto":
        return want, {"requested": want, "chosen": want, "forced": True, "caps": caps}
    for k in PRIORITY:
        if caps[k]["supported"]:
            return k, {"requested": "auto", "chosen": k, "forced": False, "caps": caps}
    return "docker", {"requested": "auto", "chosen": "docker", "forced": False, "caps": caps,
                      "note": "nothing detected as supported - trying Docker"}


def text_report(rep):
    lines = []
    for k in PRIORITY:
        c = rep["caps"][k]
        mark = "✔ ready" if c["ready"] else ("✔ installable" if c["supported"] else "✘ no")
        lines.append(f"  {k:<6} {mark:<14} {c['reason']}")
    head = f"runtime: {rep['chosen']}" + (" (forced by NODE_TYPE)" if rep.get("forced") else " (auto-detected)")
    return head + "\n" + "\n".join(lines) + (("\n  " + rep["note"]) if rep.get("note") else "")


if __name__ == "__main__":
    try:
        kind, rep = choose()
    except ValueError as e:
        sys.exit(str(e))
    if "--json" in sys.argv:
        print(json.dumps(rep))
    elif "--report" in sys.argv:
        print(text_report(rep))
    else:
        print(kind)
