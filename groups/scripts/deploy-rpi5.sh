#!/usr/bin/env bash
# Deploy Phoenix v2 al RPi5 <ECOSYSTEM> vía rsync.
# Uso: ./scripts/deploy-rpi5.sh [host]
# Default host: <user>@<HOST_IP>
set -euo pipefail

HOST="${1:-<user>@<HOST_IP>}"
REMOTE_DIR="phoenix"

cd "$(dirname "$0")/.."

echo "==> rsync a ${HOST}:${REMOTE_DIR}/"
rsync -avz --delete \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude '.venv/' \
  --exclude 'node_modules/' \
  --exclude 'auth/' \
  --exclude '*.db' \
  --exclude '*.db-journal' \
  --exclude 'logs/' \
  --exclude 'dist/' \
  --exclude '*.egg-info' \
  --exclude '.env' \
  --exclude '/.env' \
  --exclude '**/.env' \
  ./ "${HOST}:${REMOTE_DIR}/"

echo "==> deploy listo. Pasos remotos:"
cat <<'EOF'

  ssh ${HOST}
  mkdir -p ~/phoenix/logs
  # Si es la primera vez:
  cd ~/phoenix
  cp .env.example .env  # completa ANTHROPIC_API_KEY y PHOENIX_OWNER_JID después del pair
  cp brain/.env.example brain/.env
  cp wa-listener/.env.example wa-listener/.env
  cd brain && uv pip install -e .
  cd ../ui && uv pip install -e .
  cd ../wa-listener && npm install
  cd ..
  make db-init

  # systemd:
  mkdir -p ~/.config/systemd/user
  cp deploy/systemd/*.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now phoenix-brain phoenix-ui phoenix-listener
  loginctl enable-linger jmfraga

  # Pareo de WhatsApp desde la UI:
  #   ssh -L 8101:127.0.0.1:8101 <user>@<HOST_IP>
  #   navegador: http://localhost:8101/setup

EOF
