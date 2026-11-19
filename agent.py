#!/usr/bin/env python3
"""Universal node agent - Docker, LXD or classic LXC.

Run standalone on a node. It dials OUT to the bot (WebSocket, e.g. through Cloudflare), so the node needs
no public IP, no open port and works behind NAT.

Env:
  TOKEN      node token from /node-add                       (required)
  PANEL_URL  bot address, e.g. https://bot.yourdomain.com    (required)
  NODE_TYPE  auto | kvm | lxd | docker | lxc                 (default auto)

auto = detect what this machine supports (see detect.py) and use the best one: kvm > lxd > docker > lxc.
If KVM is not possible it falls back to LXD, then Docker, then LXC.
Every backend speaks the same protocol, so the bot does not care which one a node uses.
"""
import asyncio
import importlib
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if "/snap/bin" not in os.environ.get("PATH", ""):
    os.environ["PATH"] = os.environ.get("PATH", "") + ":/snap/bin"

import detect

TYPES = detect.TYPES
MODULES = {k: f"backends.{k}_backend" for k in TYPES}


def detect_type():
    try:
        kind, rep = detect.choose()
    except ValueError as e:
        sys.exit(str(e))
    print(detect.text_report(rep), flush=True)
    return kind, rep


def load_backend(kind):
    return importlib.import_module(MODULES[kind])


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

    kind, report = detect_type()
    be = load_backend(kind)
    print(f"node type: {kind}", flush=True)

    state = {"ready": False, "setup": "starting", "storage": "checking", "error": ""}
    setup_done = asyncio.Event()  # set when the runtime install/check finished (ok or failed)

    async def prepare():
        """Install/check the runtime in the background so the node shows online at once and /node-add can
        read the progress through the `info` op."""
        try:
            note = await be.ensure_lxd()
            state["setup"] = note or "ok"
            if note:
                print("setup:", note, flush=True)
            await be.apply_protection()
            state["ready"] = True
        except Exception as e:
            state["error"] = str(e)[:300]
            state["setup"] = "failed"
            print(f"{kind} setup / protection failed:", e, flush=True)
        setup_done.set()
        asyncio.create_task(be.prewarm_images())  # download all OS images in the background
        state["storage"] = await be.ensure_quota_pool()
        print("storage:", state["storage"], flush=True)
        asyncio.create_task(be.watchdog_loop())

    async def op_info():
        """Connection self-test: what this node is and whether its runtime really works."""
        status = None
        try:
            status = await be.OPS["status"]()
        except Exception as e:
            state["error"] = state["error"] or str(e)[:200]
        return {"type": kind, "ready": bool(state["ready"] and status is not None), "setup": state["setup"],
                "storage": state["storage"], "error": state["error"] if status is None or not state["ready"] else "",
                "count": (status or {}).get("count", 0), "running": (status or {}).get("running", 0),
                "images": sorted(getattr(be, "IMAGES", {}).keys()),
                "detected": {k: v["supported"] for k, v in report["caps"].items()}, "forced": report.get("forced", False)}

    ops = dict(be.OPS)
    ops["info"] = op_info
    _create = ops["create"]

    async def create_after_setup(*a, **kw):
        # first start on a fresh server: LXD / LXC / Docker may still be installing - wait instead of failing
        try:
            await asyncio.wait_for(setup_done.wait(), 1500)
        except asyncio.TimeoutError:
            pass
        if state["error"] and not state["ready"]:
            raise RuntimeError(f"{kind} is not ready on this node: {state['error']}")
        return await _create(*a, **kw)

    ops["create"] = create_after_setup

    asyncio.create_task(prepare())

    async def run_op(ws, msg):
        rid = msg.get("id")
        try:
            op = msg.get("op")
            if op not in ops:
                raise RuntimeError("unknown op")
            out = {"id": rid, "data": await ops[op](**(msg.get("kw") or {}))}
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
                async with s.ws_connect(url, headers={"Authorization": f"Bearer {token}", "X-Node-Type": kind},
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
