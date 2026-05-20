#!/usr/bin/env bash
# Deploy the one-on-one template to a remote host (RPi5, VPS, etc).
# Usage:
#   ./scripts/deploy-rpi5.sh user@host [target-path]
#
# Defaults:
#   target-path = /opt/iris
#
# What it does:
#   1. rsyncs source over SSH, excluding state (auth/, .env, *.db, .venv/, node_modules/).
#   2. Prints next steps for the remote host.

set -euo pipefail

REMOTE="${1:-}"
TARGET="${2:-/opt/iris}"

if [[ -z "$REMOTE" ]]; then
    echo "Usage: $0 user@host [target-path]" >&2
    exit 1
fi

echo "Syncing to $REMOTE:$TARGET ..."
rsync -av --delete \
    --exclude='.git/' \
    --exclude='auth/' \
    --exclude='.venv/' \
    --exclude='node_modules/' \
    --exclude='__pycache__/' \
    --exclude='*.egg-info/' \
    --exclude='.pytest_cache/' \
    --exclude='dist/' \
    --exclude='*.db' \
    --exclude='*.db-journal' \
    --exclude='.env' \
    --exclude='.env.bak.*' \
    --exclude='*.log' \
    --exclude='data/' \
    ./ "$REMOTE:$TARGET/"

cat <<EOF

✅ Sync complete.

Next steps on $REMOTE:

    cd $TARGET
    cp .env.example .env                 # edit secrets
    cp brain/.env.example brain/.env
    cp relay-bot/.env.example relay-bot/.env
    cp wa-listener/.env.example wa-listener/.env
    cp ui/.env.example ui/.env
    cp SOUL-default.md brain/SOUL.md     # customize placeholders

    # Postgres (if not already running):
    docker run --name iris-pg -e POSTGRES_USER=iris -e POSTGRES_PASSWORD=iris \\
        -e POSTGRES_DB=iris -p 5432:5432 -d postgres:16

    make brain-install ui-install wa-install relay-install
    make db-migrate

    sudo cp deploy/systemd/*.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now iris-brain iris-wa-listener iris-relay iris-ui

    sudo journalctl -fu iris-wa-listener   # scan QR once

EOF
