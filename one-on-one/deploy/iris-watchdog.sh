#!/usr/bin/env bash
# Iris v2 watchdog — corre cada 5 min vía cron en RPi5.
# Verifica los 5 componentes. Si alguno cae, alerta a OWNER en Telegram.
# Stateful en /tmp/iris-watchdog/.
set -u

STATE_DIR=/tmp/iris-watchdog
mkdir -p "$STATE_DIR"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(grep ^TELEGRAM_BOT_TOKEN /opt/iris/relay-bot/.env | cut -d= -f2)}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-$(grep ^TELEGRAM_CHAT_ID /opt/iris/relay-bot/.env | cut -d= -f2)}"

notify() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" \
        --data-urlencode "parse_mode=HTML" >/dev/null
}

# Generic check with stateful transitions
check() {
    local name="$1" check_cmd="$2"
    local state_file="$STATE_DIR/$name.state"
    local prev_state="ok"
    [[ -f "$state_file" ]] && prev_state=$(cat "$state_file")
    local cur_state="fail"
    if eval "$check_cmd" >/dev/null 2>&1; then cur_state="ok"; fi
    echo "$cur_state" > "$state_file"
    if [[ "$prev_state" == "ok" && "$cur_state" == "fail" ]]; then
        notify "🚨 <b>Iris v2 — $name caído</b>
$(date '+%H:%M:%S')"
    elif [[ "$prev_state" == "fail" && "$cur_state" == "ok" ]]; then
        notify "✅ <b>Iris v2 — $name recuperado</b>
$(date '+%H:%M:%S')"
    fi
}

# HTTP /health checks (verifica que el daemon esté vivo)
check_http() {
    local name="$1" url="$2" field="$3"
    check "$name" "curl -s --max-time 5 '$url' | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get(\"$field\") is True else 1)'"
}
check_http brain        "http://localhost:8096/health" ok
check ui "curl -sf --max-time 5 http://localhost:8097/health"
check_http relay        "http://localhost:8098/health" ok
check_http wa-listener  "http://localhost:8099/health" connected

# Postgres deep check — pg_isready dentro del container
check postgres "docker exec iris-pg pg_isready -U iris -q"
