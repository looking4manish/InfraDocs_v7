#!/usr/bin/env bash
# InfraDocs installer. Run on the server, from the repository checkout.
#
#   First server:   bash deploy/docker/deploy.sh [--name oci] [--projects ~/projects]
#                   Setup finishes in this terminal (administrator, address); no browser needed.
#                   Unattended: --admin-user U --admin-password-file F --address URL
#                   To use the browser wizard instead: --browser
#   Later servers:  curl -fsSL http://<primary>:8081/join.sh | bash -s -- <join-code> [--name n150]
#                   (join.sh clones the repository and runs this script with --join/--primary)
#
# Every step prints a tick or a cross with a named reason, and is logged with a timestamp to
# deploy/docker/install.log. Re-running is safe: an existing .env is kept, including the
# setup code, and the stack is rebuilt in place (the database volume is preserved).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
LOG="$HERE/install.log"

ts()   { date -u +%Y-%m-%dT%H:%M:%SZ; }
log()  { printf '%s %s\n' "$(ts)" "$*" >>"$LOG"; }
step() { printf '\n\033[1;36m▸ %s\033[0m\n' "$1"; log "STEP $1"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$1"; log "OK   $1"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$1"; log "WARN $1"; }
die()  { printf '  \033[1;31m✗ %s\033[0m\n' "$1" >&2; log "FAIL $1"; exit 1; }

NAME=""; PROJECTS=""; PORT=""; JOIN=""; PRIMARY=""; ADDRESS=""; ADDRESS_FLAG=""
ADMIN_U=""; ADMIN_PW_FILE=""; BROWSER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)     NAME="${2:-}"; shift 2 ;;
    --projects) PROJECTS="${2:-}"; shift 2 ;;
    --port)     PORT="${2:-}"; shift 2 ;;
    --join)     JOIN="${2:-}"; shift 2 ;;
    --primary)  PRIMARY="${2:-}"; shift 2 ;;
    --address)  ADDRESS="${2:-}"; ADDRESS_FLAG=1; shift 2 ;;
    --admin-user) ADMIN_U="${2:-}"; shift 2 ;;
    --admin-password-file) ADMIN_PW_FILE="${2:-}"; shift 2 ;;
    --browser)  BROWSER=1; shift ;;
    -h|--help)  sed -n '2,15p' "$0"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done
[[ -n "$JOIN" && -z "$PRIMARY" ]] && die "--join needs --primary <url>"
log "---- installer started (join=${JOIN:+yes}) ----"

# ───────────── 1. preflight ─────────────
step "Checking this server"
[[ "$(uname -s)" == "Linux" ]] || die "Linux is required (found $(uname -s))"
command -v curl >/dev/null || die "curl is required: sudo apt install -y curl"
if ! command -v docker >/dev/null; then
  if [[ -t 0 ]]; then
    read -rp "  Docker is not installed. Install it now with sudo? [Y/n] " a
    [[ "${a:-Y}" =~ ^[Yy] ]] || die "Docker is required"
    curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker "$USER" || die "Docker install failed"
  else
    die "Docker is required: curl -fsSL https://get.docker.com | sudo sh"
  fi
fi
if docker info >/dev/null 2>&1; then DC=(docker compose); else DC=(sudo docker compose); fi
"${DC[@]}" version >/dev/null 2>&1 || die "the docker compose plugin is missing: sudo apt install -y docker-compose-plugin"
ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || sudo docker version --format '{{.Server.Version}}')"
free_gb=$(df -Pk "$HERE" | awk 'NR==2{printf "%d", $4/1048576}')
[[ "$free_gb" -ge 3 ]] || die "at least 3 GB free disk is needed (found ${free_gb} GB)"
ok "${free_gb} GB free disk"

port_free() { ! (ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"); }
pick_port() { local p="$1"; for _ in $(seq 0 40); do port_free "$p" && { echo "$p"; return; }; p=$((p+1)); done; echo ""; }

# ───────────── 2. identity and addresses ─────────────
step "Identifying this server"
if [[ -f .env ]]; then
  ok ".env exists — keeping it (delete deploy/docker/.env to start over)"
  get() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }
  NAME="$(get SERVER_ID)"; WEB_PORT="$(get WEB_PORT)"; API_PORT="$(get API_PORT)"
  SETUP_CODE="$(get SETUP_CODE)"; TS_DNS="$(get TAILSCALE_DNS)"; TS_IP="$(get TAILSCALE_IP)"; PUB_IP="$(get PUBLIC_IP)"
else
  NAME="${NAME:-$(hostname -s | tr '[:upper:]' '[:lower:]')}"
  [[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || die "server name '$NAME': use lower-case letters, digits and hyphens (--name)"
  PROJECTS="${PROJECTS:-$HOME/projects}"
  [[ -d "$PROJECTS" ]] || warn "$PROJECTS does not exist yet — create it or pass --projects"
  WEB_PORT="$(pick_port "${PORT:-8081}")"; [[ -n "$WEB_PORT" ]] || die "no free web port from ${PORT:-8081}"
  API_PORT="$(pick_port 8090)";  [[ -n "$API_PORT" ]] || die "no free API port from 8090"
  MONGO_PORT="$(pick_port 27018)"; [[ -n "$MONGO_PORT" ]] || die "no free MongoDB port from 27018"
  TS_IP=""; TS_DNS=""
  if command -v tailscale >/dev/null 2>&1; then
    TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
    TS_DNS="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
  fi
  PUB_IP="$(curl -fsS --max-time 4 https://api.ipify.org 2>/dev/null || true)"
  SETUP_CODE="$(head -c 64 /dev/urandom | base32 | tr -dc 'A-Z2-7' | head -c 8 | sed 's/^\(....\)/\1-/')"
  SVC_PASS="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
  umask 077
  cat > .env <<EOF
SERVER_ID=$NAME
SERVER_NAME=$NAME
PROJECTS_ROOT=$PROJECTS
WEB_PORT=$WEB_PORT
API_PORT=$API_PORT
MONGO_PORT=$MONGO_PORT
DOMAIN=:$WEB_PORT
SETUP_CODE=$SETUP_CODE
ADMIN_USER=service
ADMIN_PASSWORD=$SVC_PASS
TAILSCALE_IP=$TS_IP
TAILSCALE_DNS=$TS_DNS
PUBLIC_IP=$PUB_IP
COMPOSE_PROFILES=
CF_TUNNEL_TOKEN=
TS_AUTHKEY=
EOF
  ok ".env written (mode 600)"
fi
ok "server name: $NAME"
ok "web port $WEB_PORT · API port $API_PORT"
[[ -n "$TS_DNS$TS_IP" ]] && ok "Tailscale: ${TS_DNS:-$TS_IP}" || warn "Tailscale not found — other servers must reach this one another way"
[[ -n "$PUB_IP" ]] && ok "public address: $PUB_IP"

# ───────────── 3. build and start ─────────────
step "Building and starting (the first build takes a few minutes)"
if ! "${DC[@]}" --env-file .env up -d --build >>"$LOG" 2>&1; then
  tail -20 "$LOG"
  die "the build or start failed — the full output is in $LOG"
fi
ok "containers started"
for i in $(seq 1 90); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${API_PORT}/api/health" || true)"
  [[ "$code" == "200" ]] && break
  [[ "$i" == "90" ]] && { "${DC[@]}" --env-file .env logs --tail 30 api; die "the API did not become healthy in 3 minutes"; }
  sleep 2
done
ok "API healthy"
web="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${WEB_PORT}/" || true)"
[[ "$web" == "200" ]] && ok "web server answering on :$WEB_PORT" || die "web server on :$WEB_PORT answered $web"

HOST="${TS_DNS:-${TS_IP:-${PUB_IP:-localhost}}}"
ADDRESS="${ADDRESS:-http://$HOST:$WEB_PORT}"

# ───────────── 4a. later servers: join ─────────────
if [[ -n "$JOIN" ]]; then
  step "Joining the replica set at $PRIMARY"
  body=$(printf '{"setup_code":"%s","primary_url":"%s","code":"%s","address":"%s","server_name":"%s"}' \
         "$SETUP_CODE" "$PRIMARY" "$JOIN" "$ADDRESS" "$NAME")
  resp="$(curl -s -w '\n%{http_code}' -X POST "http://localhost:${API_PORT}/api/setup/join" \
          -H 'Content-Type: application/json' -d "$body" --max-time 60 || true)"
  code="$(printf '%s' "$resp" | tail -1)"; out="$(printf '%s' "$resp" | sed '$d')"
  log "join response $code $out"
  if [[ "$code" != "200" ]]; then
    reason="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("detail"))' 2>/dev/null || echo "$out")"
    die "join refused: $reason"
  fi
  mode="$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["mode"]+" — "+d["mode_reason"])' 2>/dev/null || true)"
  ok "joined as $NAME at $ADDRESS"
  ok "replica set mode: $mode"
  printf '\n  Open the primary to see it: %s/servers\n\n' "$PRIMARY"
  exit 0
fi

# ───────────── 4b. first server: finish setup ─────────────
setup_done="$(curl -s "http://localhost:${API_PORT}/api/setup/status" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("setup_complete"))' 2>/dev/null || echo False)"
if [[ "$setup_done" == "True" ]]; then
  ok "already set up — open $ADDRESS"
  exit 0
fi

browser_box() {
  cat <<EOF

  ┌──────────────────────────────────────────────────────────┐
    Finish setup in your browser

    Open        $1
    Setup code  $SETUP_CODE
  └──────────────────────────────────────────────────────────┘

EOF
  log "waiting for browser setup at $1"
}

api() {  # api METHOD PATH [JSON] -> prints the body; succeeds on 2xx
  local out code
  out="$(curl -s -w '\n%{http_code}' -X "$1" "http://localhost:${API_PORT}$2" -H 'Content-Type: application/json' ${3:+-d "$3"} --max-time 60 || true)"
  code="$(printf '%s' "$out" | tail -1)"; printf '%s' "$out" | sed '$d'
  [[ "$code" =~ ^2 ]]
}
detail() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("detail") or d.get("reason") or d)' 2>/dev/null; }
reachable() { python3 -c 'import json,sys; print(json.load(sys.stdin).get("reachable"))' 2>/dev/null; }
check_addr() {
  local body; body="$(python3 -c 'import json,sys; print(json.dumps({"address": sys.argv[1]}))' "$1")"
  api POST /api/setup/check-address "$body"
}

UNATTENDED=""; [[ -n "$ADMIN_U" && -n "$ADMIN_PW_FILE" ]] && UNATTENDED=1
if [[ -n "$BROWSER" || ( -z "$UNATTENDED" && ! -t 0 ) ]]; then
  browser_box "$ADDRESS"
  [[ -t 0 ]] || warn "no terminal attached: rerun in a terminal to finish setup here, or pass --admin-user, --admin-password-file and --address"
  exit 0
fi

step "Finishing setup on this server"
# 1. administrator
if [[ -n "$UNATTENDED" ]]; then
  [[ -r "$ADMIN_PW_FILE" ]] || die "cannot read $ADMIN_PW_FILE"
  ADMIN_PW="$(head -1 "$ADMIN_PW_FILE")"
else
  read -rp "  Administrator username [${USER:-admin}]: " ADMIN_U; ADMIN_U="${ADMIN_U:-${USER:-admin}}"
  while true; do
    read -rsp "  Password (12+ characters): " ADMIN_PW; echo
    read -rsp "  Repeat password: " PW2; echo
    [[ "$ADMIN_PW" == "$PW2" ]] || { warn "the passwords differ"; continue; }
    [[ ${#ADMIN_PW} -ge 12 ]] || { warn "use at least 12 characters"; continue; }
    break
  done
fi
[[ "$ADMIN_U" =~ ^[a-z][a-z0-9._-]{1,31}$ ]] || die "username '$ADMIN_U': lower-case letters, digits, . _ -"
[[ ${#ADMIN_PW} -ge 12 ]] || die "the administrator password needs at least 12 characters"
ok "administrator: $ADMIN_U"

# 2. the address other servers and your browser use to reach this one
MODE="domain"
if [[ -z "$UNATTENDED" && -z "$ADDRESS_FLAG" ]]; then
  cands=()
  [[ -n "$TS_DNS" ]] && cands+=("http://$TS_DNS:$WEB_PORT|Tailscale name, private to your tailnet")
  [[ -n "$TS_IP"  ]] && cands+=("http://$TS_IP:$WEB_PORT|Tailscale address, private to your tailnet")
  [[ -n "$PUB_IP" ]] && cands+=("http://$PUB_IP:$WEB_PORT|Public IP, open port $WEB_PORT in the cloud firewall")
  echo "  How will other servers and your browser reach this one?"
  i=1; for c in "${cands[@]}"; do printf '    %d) %-44s %s\n' "$i" "${c%%|*}" "${c#*|}"; i=$((i+1)); done
  printf '    %d) Another address (a domain or a Cloudflare Tunnel hostname)\n' "$i"
  while true; do
    read -rp "  Choose [1]: " pick; pick="${pick:-1}"
    if [[ "$pick" =~ ^[0-9]+$ && "$pick" -ge 1 && "$pick" -lt "$i" ]]; then
      ADDRESS="${cands[$((pick-1))]%%|*}"; MODE="domain"
      [[ "${cands[$((pick-1))]}" == *Tailscale* ]] && MODE="tailscale"
    elif [[ "$pick" == "$i" ]]; then
      read -rp "  Address (http://host:port or https://host): " ADDRESS
      read -rp "  Is this a Cloudflare Tunnel hostname? [y/N]: " cf; MODE="domain"; [[ "$cf" =~ ^[Yy] ]] && MODE="cloudflare"
    else
      warn "pick a number from the list"; continue
    fi
    ADDRESS="${ADDRESS%/}"
    res="$(check_addr "$ADDRESS" || true)"
    if [[ "$(printf '%s' "$res" | reachable)" == "True" ]]; then ok "$ADDRESS reaches this server"; break; fi
    warn "$ADDRESS does not reach this server: $(printf '%s' "$res" | detail)"
    read -rp "  Use it anyway? [y/N]: " anyway; [[ "$anyway" =~ ^[Yy] ]] && break
  done
else
  ADDRESS="${ADDRESS%/}"
  if [[ ( -n "$TS_DNS" && "$ADDRESS" == *"$TS_DNS"* ) || ( -n "$TS_IP" && "$ADDRESS" == *"$TS_IP"* ) ]]; then MODE="tailscale"; fi
  res="$(check_addr "$ADDRESS" || true)"
  if [[ "$(printf '%s' "$res" | reachable)" == "True" ]]; then ok "$ADDRESS reaches this server"
  else warn "$ADDRESS did not answer as this server: $(printf '%s' "$res" | detail)"; fi
fi

# 3. administrator + one-member replica set + first scan
body="$(SC="$SETUP_CODE" AU="$ADMIN_U" AP="$ADMIN_PW" AN="$NAME" AD="$ADDRESS" AM="$MODE" PR="${PROJECTS:-}" python3 -c '
import json, os
print(json.dumps({"setup_code": os.environ["SC"], "admin": {"username": os.environ["AU"], "password": os.environ["AP"]},
                  "server_name": os.environ["AN"], "projects_folder": os.environ["PR"] or None,
                  "access": {"mode": os.environ["AM"], "address": os.environ["AD"]}}))')"
unset ADMIN_PW PW2
if out="$(api POST /api/setup/complete "$body")"; then
  ok "setup complete: $NAME is the primary of a one-member replica set"
  ok "first scan started"
  cat <<EOF

  ┌──────────────────────────────────────────────────────────┐
    InfraDocs is ready

    Address       $ADDRESS
    Sign in as    $ADMIN_U
    Add servers   Servers → Add a server
  └──────────────────────────────────────────────────────────┘

EOF
  log "setup completed in the terminal (admin $ADMIN_U, address $ADDRESS, mode $MODE)"
else
  die "setup refused: $(printf '%s' "$out" | detail)"
fi
