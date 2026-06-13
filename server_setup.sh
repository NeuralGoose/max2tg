#!/usr/bin/env bash
# One-shot setup for an Ubuntu Always-Free VM (Oracle Cloud).
# Run from the project directory (where Dockerfile and .env live):
#   chmod +x server_setup.sh && ./server_setup.sh
set -euo pipefail

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Run: cp .env.example .env && nano .env" >&2
  exit 1
fi

# Install Docker + compose plugin if missing
if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi

echo "Building and starting the bridge..."
sudo docker compose up -d --build
echo
echo "Done. Follow logs with:  sudo docker compose logs -f"
echo "Look for: 'Bridge online (own id: ...)'"
