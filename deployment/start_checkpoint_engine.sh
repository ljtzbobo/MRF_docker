#!/bin/sh
# Dedicated native daemon; does not select/change a Docker Desktop context.
set -eu
test "$(id -u)" = 0 || { echo 'Run in Linux as root'; exit 1; }
CRIU_BIN_DIR=${MRF_CRIU_BIN_DIR:-/opt/mrf-checkpoint/criu-4.2.1/criu}
DOCKER_BIN_DIR=${MRF_DOCKER_BIN_DIR:-/opt/mrf-checkpoint/docker-27.5.1/docker}
test -x "$CRIU_BIN_DIR/criu"
test -x "$DOCKER_BIN_DIR/dockerd"
mkdir -p /run/mrf-checkpoint27 /var/lib/mrf-checkpoint-docker27
if test -S /run/mrf-checkpoint27/docker.sock && "$DOCKER_BIN_DIR/docker" -H unix:///run/mrf-checkpoint27/docker.sock info --format '{{.ID}}'; then
  exit 0
fi
printf '%s\n' '{"experimental":true,"storage-driver":"vfs"}' > /run/mrf-checkpoint27/daemon.json
ip link show mrfbr27 >/dev/null 2>&1 || ip link add name mrfbr27 type bridge
ip -4 addr show dev mrfbr27 | grep -q '172.29.251.1/24' || ip addr add 172.29.251.1/24 dev mrfbr27
ip link set mrfbr27 up
systemd-run --unit=mrf-checkpoint-containerd27 --property=Restart=on-failure \
  /usr/bin/env PATH="$CRIU_BIN_DIR:$DOCKER_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  "$DOCKER_BIN_DIR/containerd" --address /run/mrf-checkpoint27/containerd.sock \
  --root /var/lib/mrf-checkpoint-containerd27 --state /run/mrf-checkpoint27/containerd
systemd-run --unit=mrf-checkpoint-engine27 --property=Restart=on-failure \
  /usr/bin/env PATH="$CRIU_BIN_DIR:$DOCKER_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" "$DOCKER_BIN_DIR/dockerd" \
  --config-file=/run/mrf-checkpoint27/daemon.json \
  --host=unix:///run/mrf-checkpoint27/docker.sock \
  --containerd=/run/mrf-checkpoint27/containerd.sock \
  --containerd-namespace=mrf-checkpoint --containerd-plugins-namespace=mrf-checkpoint-plugins \
  --data-root=/var/lib/mrf-checkpoint-docker27 \
  --exec-root=/run/mrf-checkpoint27/exec \
  --pidfile=/run/mrf-checkpoint27/docker.pid \
  --bridge=mrfbr27
echo "Dedicated daemon launch requested; check journalctl -u mrf-checkpoint-engine27"
