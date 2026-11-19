# Node agent image for NODE_TYPE=docker (VPS = sibling Docker containers, created through the host's docker.sock).
# LXD / LXC nodes run natively (systemd) - see install_node.sh - because they need the host's lxc tools.
FROM docker:27-cli AS dockercli

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 NODE_TYPE=docker
RUN apt-get update \
 && apt-get install -y --no-install-recommends iptables procps curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY requirements.txt .
RUN pip install -r requirements.txt
WORKDIR /app
COPY agent.py detect.py ./
COPY backends ./backends
CMD ["python", "/app/agent.py"]
