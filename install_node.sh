#!/usr/bin/env bash
# Node setup:  bash install_node.sh <BOT_URL> <TOKEN> [auto|kvm|lxd|docker|lxc]
#   BOT_URL = the bot's Cloudflare address, e.g. https://bot.yourdomain.com
#             (same-machine test: localhost:8765 or <ip>:8765)
#   TYPE    = what the VPS on this node are made of. Default auto = DETECT what this server supports and
#             use the best one:  kvm (real VMs) > lxd > docker > lxc.  KVM not possible -> LXD/Docker/LXC.
# The node connects OUT to the bot - no IP, no port, no firewall rule needed on this server.
# Everything needed (Docker / LXD / LXC, the bridge, all OS images) is installed automatically.
set -e
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
PANEL="${1:?usage: bash install_node.sh <BOT_URL> <TOKEN> [auto|kvm|lxd|docker|lxc]}"
TOKEN="${2:?usage: bash install_node.sh <BOT_URL> <TOKEN> [auto|kvm|lxd|docker|lxc]}"
TYPE="$(echo "${3:-auto}" | tr 'A-Z' 'a-z')"
case "$TYPE" in auto|kvm|lxd|docker|lxc) ;; *) echo "TYPE must be auto, kvm, lxd, docker or lxc"; exit 1;; esac
cd "$(cd "$(dirname "$0")" && pwd)"
DIR="$(pwd)"
export DEBIAN_FRONTEND=noninteractive
export PATH="$PATH:/snap/bin"

if [ "$TYPE" = auto ]; then
  command -v python3 >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq python3; } >/dev/null 2>&1 || true
  echo "Detecting what this server supports..."
  python3 "$DIR/detect.py" --report || true
  TYPE="$(python3 "$DIR/detect.py" 2>/dev/null | tail -1)"
  case "$TYPE" in kvm|lxd|docker|lxc) ;; *) TYPE=docker;; esac
  echo "=> node type auto-detected: $TYPE"
fi

if [ "$TYPE" = docker ]; then
  # ---- Docker node: the agent itself runs in a container and creates sibling containers
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker not found - installing it (get.docker.com)..."
    curl -fsSL https://get.docker.com | sh
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  if docker compose version >/dev/null 2>&1; then DC="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then DC="docker-compose"
  else echo "docker compose plugin missing (apt-get install docker-compose-plugin)"; exit 1; fi
  # a node that used to run natively must not keep a second link to the bot
  systemctl disable --now legacy-vps-node >/dev/null 2>&1 || true
  umask 077
  printf 'TOKEN=%s\nPANEL_URL=%s\nNODE_TYPE=docker\n' "$TOKEN" "$PANEL" > .env
  $DC up -d --build
  echo "Docker node agent started - it connects to $PANEL by itself. Logs: (cd $DIR && $DC logs -f)"
else
  # ---- KVM / LXD / LXC node: runs natively (needs the host's tools); the agent installs KVM/libvirt, LXD or LXC itself
  apt-get update -qq && apt-get install -y -qq python3-venv python3-pip iptables curl
  python3 -m venv "$DIR/venv"
  "$DIR/venv/bin/pip" install -q aiohttp
  docker rm -f lb-node >/dev/null 2>&1 || true
  umask 077
  cat > /etc/systemd/system/legacy-vps-node.service <<UNIT
[Unit]
Description=Legacy VPS node agent ($TYPE)
After=network-online.target snap.lxd.daemon.service lxc-net.service libvirtd.service
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
Environment=PYTHONUNBUFFERED=1
Environment=TOKEN=$TOKEN
Environment=PANEL_URL=$PANEL
Environment=NODE_TYPE=$TYPE
ExecStart=$DIR/venv/bin/python $DIR/agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  chmod 600 /etc/systemd/system/legacy-vps-node.service
  systemctl daemon-reload
  systemctl enable --now legacy-vps-node
  echo "$TYPE node agent started - it connects to $PANEL by itself. Logs: journalctl -u legacy-vps-node -f"
fi
echo "In Discord the node shows 🟢 online within a few seconds (check /node-list or press 🔌 Connect the node)."
