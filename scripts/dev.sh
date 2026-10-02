#!/usr/bin/env bash
# Build and launch OcheCore locally with live logs.
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export OCHECORE_PUBLISH_HOST="${OCHECORE_PUBLISH_HOST:-0.0.0.0}"
command -v docker >/dev/null || { echo "Install Docker first." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || {
    echo "Install the Docker Compose plugin first." >&2
    exit 1
}
docker info >/dev/null 2>&1 || { echo "Start Docker first." >&2; exit 1; }

compose=(docker compose -p oche-dev -f docker-compose.yml)
if [[ -f docker-compose.override.yml ]]; then
    compose+=(-f docker-compose.override.yml)
fi

echo "OcheCore development: listening on ${OCHECORE_PUBLISH_HOST}; .env can override port 9180."
echo "Open http://localhost:9180 here or http://YOUR_LAN_IP:9180 from another device."
echo "Stop other Oche containers first. Press Ctrl+C to stop; logs appear below."
exec "${compose[@]}" up --build
