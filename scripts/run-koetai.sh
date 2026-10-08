#!/usr/bin/env bash
# Start koetai and triple store containers

set -euo pipefail

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ask()  { local prompt="$1" default="$2" ans; read -r -p "$prompt [$default]: " ans; echo "${ans:-$default}"; }
yesno(){ local prompt="$1" default="${2:-y}" ans; read -r -p "$prompt [y/n] (default $default): " ans; ans="${ans:-$default}"; [[ "$ans" =~ ^[Yy] ]]; }
port_in_use() {
  if command -v lsof >/dev/null; then lsof -i ":$1" >/dev/null 2>&1
  elif command -v ss   >/dev/null; then ss -ltn | awk '{print $4}' | grep -q ":$1\$"
  else (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&-; return 0; } || return 1
  fi
}

STORES="fuseki oxigraph"
COMPOSE_PROFILE="--profile oxigraph"
PORT=3002
DIR="/home/gordonadmin/koetai-platform"

say "5/6 Starting containers ($STORES) — first run also builds the image, this can take a few minutes"
COMPOSE="docker compose"
docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"   # older standalone install
# shellcheck disable=SC2086
$COMPOSE $COMPOSE_PROFILE up -d --build koetai $STORES

echo -n "Waiting for Koetai to answer on port $PORT"
for _ in $(seq 1 60); do
  curl -sf "http://localhost:$PORT/" >/dev/null 2>&1 && { echo " up."; break; }
  echo -n "."; sleep 2
done
if ! curl -sf "http://localhost:$PORT/" >/dev/null 2>&1; then
  echo
  echo "Didn't come up in time. Recent logs:"
  $COMPOSE logs koetai --tail 50
  exit 1
fi

# ── Done ─────────────────────────────────────────────────────────────────────
say "6/6 Done"
echo "Koetai is running at: http://localhost:$PORT"
if [[ "$STORES" == *oxigraph* ]]; then
  echo "When you create your first dataset, set its backend to 'oxigraph' in the New Dataset form."
fi
echo
echo "Useful commands (run from $DIR):"
echo "  $COMPOSE logs -f koetai        # follow the app log"
echo "  $COMPOSE down                  # stop (keeps your data)"
echo "  $COMPOSE $COMPOSE_PROFILE up -d koetai $STORES   # start again"
echo "  $COMPOSE down -v               # stop AND delete all data"
echo

if command -v xdg-open >/dev/null && yesno "Open it in your browser now?" "y"; then
  xdg-open "http://localhost:$PORT" >/dev/null 2>&1 &
fi
