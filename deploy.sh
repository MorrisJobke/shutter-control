#!/bin/bash
# Deploy shutter-control to Home Assistant OS for testing.
#
# Usage:
#   1. Edit config.yaml with your shutter IDs and MQTT credentials
#   2. Run: ./deploy.sh <haos-ip>
#
# The config is baked into the image. To change config, edit config.yaml
# and re-run this script.
#
# To remove everything after testing:
#   ssh root@<haos-ip> "docker stop shutter-control; docker rm shutter-control; docker rmi shutter-control"

set -euo pipefail

HAOS_IP="${1:?Usage: ./deploy.sh <haos-ip>}"
HAOS_USER="${2:-root}"
SSH_TARGET="${HAOS_USER}@${HAOS_IP}"
SERIAL_DEV="${SERIAL_DEV:-/dev/ttyUSB0}"

if [ ! -f config.yaml ]; then
    echo "Error: config.yaml not found. Copy config.example.yaml and edit it."
    exit 1
fi

echo "==> Building Docker image..."
docker build --platform linux/amd64 -t shutter-control .

echo "==> Saving image..."
docker save shutter-control | gzip > /tmp/shutter-control.tar.gz

echo "==> Copying image to ${SSH_TARGET}..."
scp /tmp/shutter-control.tar.gz "${SSH_TARGET}:/tmp/"

echo "==> Loading image and starting container on HAOS..."
ssh "${SSH_TARGET}" bash <<REMOTE
set -euo pipefail

# Load image
docker load < /tmp/shutter-control.tar.gz
rm /tmp/shutter-control.tar.gz

# Stop old container if exists
docker stop shutter-control 2>/dev/null || true
docker rm shutter-control 2>/dev/null || true

# Start container
#   --network=hassio  -> can reach Mosquitto at core-mosquitto:1883
#   --device          -> pass through EnOcean USB stick
#   --restart         -> auto-restart on crash (not on reboot)
docker run -d \\
    --name shutter-control \\
    --network=hassio \\
    --device="${SERIAL_DEV}:${SERIAL_DEV}" \\
    --restart unless-stopped \\
    shutter-control

echo ""
echo "==> Container started. Check logs with:"
echo "    docker logs -f shutter-control"
REMOTE

rm /tmp/shutter-control.tar.gz

echo ""
echo "==> Done! SSH into HAOS and run 'docker logs -f shutter-control' to verify."
echo ""
echo "To remove after testing:"
echo "  ssh ${SSH_TARGET} 'docker stop shutter-control; docker rm shutter-control; docker rmi shutter-control'"
