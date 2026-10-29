#!/usr/bin/env bash
# One entry point for every local service, started in dependency order:
# Postgres → MCP Toolbox → Judge → Masker → Phoenix.
#
#   ./run.sh up       start everything below that exists so far
#   ./run.sh db       start Postgres and wait until it accepts connections
#   ./run.sh toolbox  start Postgres (if needed), then the MCP Toolbox on :$TOOLBOX_PORT (5001)
#   ./run.sh judge    start the Security Judge (A2A) on :10002
#   ./run.sh masker   start the Data Masker (A2A) on :10003
#   ./run.sh phoenix  start Phoenix on :6006 as its own process, traces kept in .phoenix/
#   ./run.sh web      start the web UI on http://127.0.0.1:8000 (needs everything above)
#   ./run.sh reset    drop, recreate and seed the shop tables (EVALS §2)
#   ./run.sh psql     open psql on the shop database (as the admin user)
#   ./run.sh status   show which services are up
#   ./run.sh stop     stop everything (data and traces are kept)
#   ./run.sh stop judge|masker|web   stop only that one (the fail-loud check)
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "missing .env (copy .env.example)" >&2; exit 1; }
set -a; source .env; set +a
: "${POSTGRES_DB:=shop}" "${TOOLBOX_DB_PASSWORD:?set TOOLBOX_DB_PASSWORD in .env}"

PG_CONTAINER=support-postgres
TOOLBOX_PORT=${TOOLBOX_PORT:-5001}   # not 5000: macOS AirPlay Receiver owns it
TOOLBOX_VERSION=1.15.0           # pinned: the tools.yaml format changed in v1
PHOENIX_PORT=${PHOENIX_PORT:-6006}
JUDGE_PORT=${JUDGE_PORT:-10002}
MASKER_PORT=${MASKER_PORT:-10003}
WEB_PORT=${WEB_PORT:-8000}
mkdir -p .pids logs .phoenix
pg_exec() { docker exec -i "$PG_CONTAINER" psql -U postgres -d "$POSTGRES_DB" "$@"; }

pg_ready() { docker exec "$PG_CONTAINER" pg_isready -U postgres -d "$POSTGRES_DB" >/dev/null 2>&1; }

start_db() {
  docker info >/dev/null 2>&1 || { echo "Docker daemon is not running (open Docker Desktop)" >&2; exit 1; }
  docker compose up -d postgres >/dev/null
  printf "postgres: waiting"
  for _ in $(seq 1 60); do
    if pg_ready; then echo " … ok (127.0.0.1:${POSTGRES_PORT:-5432}/$POSTGRES_DB)"; return 0; fi
    printf "."; sleep 1
  done
  echo " … FAILED: postgres not ready after 60s" >&2; exit 1
}

reset_db() {
  pg_ready || start_db
  # ON_ERROR_STOP + single transaction: the reset is all-or-nothing
  pg_exec -v ON_ERROR_STOP=1 --single-transaction -q \
          -v toolbox_pw="$TOOLBOX_DB_PASSWORD" < db/seed.sql
  pg_exec -tA -c "select 'reset: ' || (select count(*) from users) || ' users, '
                   || (select count(*) from customer_orders) || ' orders, '
                   || (select count(*) from actions_log) || ' actions'"
}

port_up() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
port_owner() { lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $1" pid "$2}'; }
# the listener on the port is the toolbox we started (npx spawns it as a child)
toolbox_ours() {
  [[ -f .pids/toolbox ]] || return 1
  local parent; parent=$(cat .pids/toolbox)
  lsof -tiTCP:$TOOLBOX_PORT -sTCP:LISTEN 2>/dev/null | while read -r pid; do
    [[ $pid == "$parent" ]] && echo yes && break
    pgrep -P "$parent" | grep -qx "$pid" && echo yes && break
    for c in $(pgrep -P "$parent"); do pgrep -P "$c" | grep -qx "$pid" && echo yes && break 2; done
  done | grep -q yes
}

start_toolbox() {
  pg_ready || start_db
  if port_up $TOOLBOX_PORT; then
    if toolbox_ours; then echo "toolbox: already running on :$TOOLBOX_PORT"; return 0; fi
    echo "toolbox: port $TOOLBOX_PORT is held by another process ($(port_owner $TOOLBOX_PORT)); set TOOLBOX_PORT in .env" >&2
    exit 1
  fi
  # .env is exported above, so tools.yaml's ${TOOLBOX_DB_PASSWORD} etc. resolve
  nohup npx -y "@toolbox-sdk/server@$TOOLBOX_VERSION" --config mcp_toolbox/tools.yaml \
        --address 127.0.0.1 --port $TOOLBOX_PORT > logs/toolbox.log 2>&1 &
  echo $! > .pids/toolbox
  printf "toolbox: waiting"
  for _ in $(seq 1 60); do
    if port_up $TOOLBOX_PORT; then echo " … ok (http://127.0.0.1:$TOOLBOX_PORT/mcp)"; return 0; fi
    kill -0 "$(cat .pids/toolbox)" 2>/dev/null || { echo " … FAILED, see logs/toolbox.log:" >&2; tail -20 logs/toolbox.log >&2; exit 1; }
    printf "."; sleep 1
  done
  echo " … FAILED: not listening after 60s (logs/toolbox.log)" >&2; exit 1
}

stop_toolbox() {
  if [[ -f .pids/toolbox ]]; then
    for c in $(pgrep -P "$(cat .pids/toolbox)"); do pkill -P "$c" 2>/dev/null || true; done
    pkill -P "$(cat .pids/toolbox)" 2>/dev/null || true
    kill "$(cat .pids/toolbox)" 2>/dev/null || true
    rm -f .pids/toolbox
  fi
  echo "toolbox: stopped"
}

start_phoenix() {
  if curl -sf -o /dev/null "http://127.0.0.1:$PHOENIX_PORT/v1/projects"; then
    echo "phoenix: already running on :$PHOENIX_PORT"; return 0
  fi
  if port_up $PHOENIX_PORT; then
    echo "phoenix: port $PHOENIX_PORT is held by another process ($(port_owner $PHOENIX_PORT))" >&2; exit 1
  fi
  # own process, own venv, SQLite under .phoenix/: traces survive agent restarts (O-1, R-10)
  PHOENIX_WORKING_DIR="$PWD/.phoenix" PHOENIX_HOST=127.0.0.1 PHOENIX_PORT=$PHOENIX_PORT \
    nohup .venv-phoenix/bin/phoenix serve > logs/phoenix.log 2>&1 &
  echo $! > .pids/phoenix
  printf "phoenix: waiting"
  for _ in $(seq 1 90); do
    if curl -sf -o /dev/null "http://127.0.0.1:$PHOENIX_PORT/v1/projects"; then
      echo " … ok (http://127.0.0.1:$PHOENIX_PORT)"; return 0
    fi
    kill -0 "$(cat .pids/phoenix)" 2>/dev/null || { echo " … FAILED, see logs/phoenix.log:" >&2; tail -20 logs/phoenix.log >&2; exit 1; }
    printf "."; sleep 1
  done
  echo " … FAILED: not answering after 90s (logs/phoenix.log)" >&2; exit 1
}

# start_a2a <name> <module> <port>: a guard service as its own process (J-1, K-1)
start_a2a() {
  local name=$1 module=$2 port=$3 card="http://127.0.0.1:$3/.well-known/agent-card.json"
  if curl -sf -o /dev/null "$card"; then echo "$name: already running on :$port"; return 0; fi
  if port_up "$port"; then echo "$name: port $port is held by another process ($(port_owner "$port"))" >&2; exit 1; fi
  nohup .venv/bin/python -m "$module" > "logs/$name.log" 2>&1 &
  echo $! > ".pids/$name"
  printf "%s: waiting" "$name"
  for _ in $(seq 1 60); do
    if curl -sf -o /dev/null "$card"; then echo " … ok ($card)"; return 0; fi
    kill -0 "$(cat ".pids/$name")" 2>/dev/null || { echo " … FAILED, see logs/$name.log:" >&2; tail -20 "logs/$name.log" >&2; exit 1; }
    printf "."; sleep 1
  done
  echo " … FAILED: no agent card after 60s (logs/$name.log)" >&2; exit 1
}

stop_a2a() {
  local name=$1 pid
  if [[ -f ".pids/$name" ]]; then
    pid=$(cat ".pids/$name")
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done   # wait until it is gone
    rm -f ".pids/$name"
  fi
  echo "$name: stopped"
}

start_web() {
  if curl -sf -o /dev/null "http://127.0.0.1:$WEB_PORT/"; then echo "web: already running on :$WEB_PORT"; return 0; fi
  if port_up "$WEB_PORT"; then echo "web: port $WEB_PORT is held by another process ($(port_owner "$WEB_PORT"))" >&2; exit 1; fi
  nohup .venv/bin/uvicorn support.web:app --host 127.0.0.1 --port "$WEB_PORT" > logs/web-server.log 2>&1 &
  echo $! > .pids/web
  printf "web: waiting"
  for _ in $(seq 1 60); do
    if curl -sf -o /dev/null "http://127.0.0.1:$WEB_PORT/"; then echo " … ok (http://127.0.0.1:$WEB_PORT)"; return 0; fi
    kill -0 "$(cat .pids/web)" 2>/dev/null || { echo " … FAILED, see logs/web-server.log:" >&2; tail -20 logs/web-server.log >&2; exit 1; }
    printf "."; sleep 1
  done
  echo " … FAILED (logs/web-server.log)" >&2; exit 1
}

stop_phoenix() {
  if [[ -f .pids/phoenix ]]; then kill "$(cat .pids/phoenix)" 2>/dev/null || true; rm -f .pids/phoenix; fi
  echo "phoenix: stopped (traces kept in .phoenix/)"
}

status() {
  if pg_ready; then echo "postgres  up    127.0.0.1:${POSTGRES_PORT:-5432}/$POSTGRES_DB"
  else              echo "postgres  DOWN"; fi
  if toolbox_ours; then         echo "toolbox   up    http://127.0.0.1:$TOOLBOX_PORT/mcp"
  elif port_up $TOOLBOX_PORT; then echo "toolbox   DOWN  (port $TOOLBOX_PORT held by $(port_owner $TOOLBOX_PORT))"
  else                           echo "toolbox   DOWN"; fi
  if curl -sf -o /dev/null "http://127.0.0.1:$JUDGE_PORT/.well-known/agent-card.json"; then
                                 echo "judge     up    http://127.0.0.1:$JUDGE_PORT  (A2A)"
  else                           echo "judge     DOWN"; fi
  if curl -sf -o /dev/null "http://127.0.0.1:$MASKER_PORT/.well-known/agent-card.json"; then
                                 echo "masker    up    http://127.0.0.1:$MASKER_PORT  (A2A)"
  else                           echo "masker    DOWN"; fi
  if curl -sf -o /dev/null "http://127.0.0.1:$WEB_PORT/"; then
                                 echo "web       up    http://127.0.0.1:$WEB_PORT"
  else                           echo "web       DOWN"; fi
  if curl -sf -o /dev/null "http://127.0.0.1:$PHOENIX_PORT/v1/projects"; then
                                 echo "phoenix   up    http://127.0.0.1:$PHOENIX_PORT"
  else                           echo "phoenix   DOWN"; fi
}

case "${1:-}" in
  db)     start_db ;;
  reset)  reset_db ;;
  psql)   shift; docker exec -it "$PG_CONTAINER" psql -U postgres -d "$POSTGRES_DB" "$@" ;;
  up)     start_db; start_toolbox; start_a2a judge guards.judge $JUDGE_PORT
          start_a2a masker guards.masker $MASKER_PORT; start_phoenix ;;
  judge)  start_a2a judge guards.judge $JUDGE_PORT ;;
  masker) start_a2a masker guards.masker $MASKER_PORT ;;
  web)    start_web ;;
  toolbox) start_toolbox ;;
  phoenix) start_phoenix ;;
  status) status ;;
  stop)
    case "${2:-all}" in
      judge|masker|web) stop_a2a "$2" ;;
      all)   stop_a2a web; stop_phoenix; stop_a2a masker; stop_a2a judge; stop_toolbox; docker compose stop postgres ;;
      *)     echo "unknown service: $2" >&2; exit 1 ;;
    esac ;;
  *)      sed -n '2,18p' "$0"; exit 1 ;;
esac
