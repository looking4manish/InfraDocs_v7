#!/usr/bin/env bash
# InfraDocs one-shot deploy. Run it on the server (SSH or Cockpit terminal):
#   bash deploy/docker/deploy.sh
# Installs Docker if needed, writes a minimal deploy/docker/.env, builds + starts
# the stack, AND sets up a browser-reachable URL for the first-run wizard (where
# you fill in everything else). Re-runnable. Defaults are fine for a first test.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# Non-interactive mode (set by install.sh): reuse a pre-written .env, never prompt,
# and skip the browser-exposure menu entirely (the operator already supplied the
# reachable address — transport-agnostic, no Tailscale).
NONINT="${INFRADOCS_NONINTERACTIVE:-}"

say()  { printf "\n\033[1;36m> %s\033[0m\n" "$1"; }
warn() { printf "\033[1;33m! %s\033[0m\n" "$1"; }
ask()  { local p="$1" d="$2" v; read -rp "$p [$d]: " v; printf '%s' "${v:-$d}"; }

# --- 1. Docker -----------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  if [[ -n "$NONINT" ]]; then
    echo "Docker is required but not installed (non-interactive). Install Docker, then retry."; exit 1
  fi
  warn "Docker is not installed."
  read -rp "Install Docker now (needs sudo)? [Y/n]: " a
  if [[ "${a:-Y}" =~ ^[Yy] ]]; then
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER" || true
    warn "Docker installed — if the build asks for sudo, the group isn't active"
    warn "yet; log out/in afterwards to run docker without sudo."
  else
    echo "Docker is required. Aborting."; exit 1
  fi
fi
if docker info >/dev/null 2>&1; then DC=(docker compose); else DC=(sudo docker compose); fi
"${DC[@]}" version >/dev/null 2>&1 || { echo "docker compose plugin missing"; exit 1; }

# Random URL-safe secret (hex -> no characters that need escaping in a Mongo URI).
gen_secret() { openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# --- 2. Minimal .env (the rest is configured in the web wizard) ----------
if [[ -f .env ]]; then
  say ".env exists - reusing it. (Delete deploy/docker/.env to reconfigure.)"
  # Backfill Mongo credentials on an .env from before auth was enabled, so the
  # container can start. NOTE: an EXISTING mongo_data volume has no users, so
  # enabling --auth against it will lock the API out — the volume must be recreated
  # (docker compose down -v; the scan catalog re-populates on next scan). Fresh
  # deploys are unaffected.
  if ! grep -q '^MONGO_PASSWORD=' .env; then
    grep -q '^MONGO_USER=' .env || printf 'MONGO_USER=infradocs\n' >> .env
    printf 'MONGO_PASSWORD=%s\n' "$(gen_secret)" >> .env
    warn "Added MONGO_PASSWORD to .env (auth is now enforced). If Mongo already had"
    warn "data, recreate its volume:  ${DC[*]} --env-file .env down -v   (scan re-populates)"
  fi
elif [[ -n "$NONINT" ]]; then
  echo "No deploy/docker/.env present (non-interactive). install.sh should write it first."; exit 1
else
  say "Minimal config - everything else is set in the web wizard after login."
  SERVER_ID=$(ask "Server id (short, e.g. oci-p)" "$(hostname -s 2>/dev/null || echo infradocs)")
  PROJECTS_ROOT=$(ask "Where your apps live (Projects root)" "$HOME/projects")
  {
    printf 'SERVER_ID=%s\n'      "$SERVER_ID"
    printf 'SERVER_NAME=%s\n'    "$SERVER_ID"
    printf 'ADMIN_USER=admin\n'
    printf 'ADMIN_PASSWORD=Changeme001\n'
    printf 'MONGO_USER=infradocs\n'
    printf 'MONGO_PASSWORD=%s\n' "$(gen_secret)"
    printf 'PROJECTS_ROOT=%s\n'  "$PROJECTS_ROOT"
    printf 'DOMAIN=:8081\n'      # Caddy (host net) listens here; must match WEB_PORT
    printf 'COMPOSE_PROFILES=\n'
    printf 'CF_TUNNEL_TOKEN=\n'
    printf 'TS_AUTHKEY=\n'
    printf 'WEB_PORT=8081\n'
    printf 'WEB_TLS_PORT=8443\n'
    printf 'API_PORT=8090\n'
    printf 'MONGO_PORT=27018\n'
  } > .env
  chmod 600 .env
  say ".env written (mode 600)."
fi

# --- 3. Build + start ----------------------------------------------------
say "Building + starting (first run pulls images + builds the UI - a few minutes)..."
"${DC[@]}" --env-file .env up -d --build

# --- 4. Wait for health --------------------------------------------------
get() { grep -E "^$1=" .env | cut -d= -f2-; }
API_PORT="$(get API_PORT)"; API_PORT="${API_PORT:-8090}"
WEB_PORT="$(get WEB_PORT)"; WEB_PORT="${WEB_PORT:-8081}"
say "Waiting for the API..."
ok=""
for _ in $(seq 1 40); do
  [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${API_PORT}/api/health" || true)" == "200" ]] && { ok=1; break; }
  sleep 2
done
"${DC[@]}" --env-file .env ps
[[ -n "$ok" ]] && say "InfraDocs is up." || warn "API not ready - check: ${DC[*]} --env-file .env logs api"

# --- 5. Make the wizard reachable from a browser ------------------------
WIZ_URL=""
if [[ -n "$NONINT" ]]; then
  # install.sh drives onboarding over the terminal + the operator-supplied address;
  # no browser-exposure step, no Tailscale. Report the local URL the installer probes.
  WIZ_URL="${INFRADOCS_ADVERTISE_URL:-http://localhost:${WEB_PORT}}"
  say "Non-interactive: stack is up. Onboarding handled by install.sh."
  echo "Local URL: http://localhost:${WEB_PORT}  ·  reachable at: ${WIZ_URL}"
  exit 0
fi
say "How should you open the wizard in your browser?"
echo "  1) Tailscale  (private URL on your tailnet - recommended, no domain/firewall)"
echo "  2) Public IP  (open the port in your cloud firewall, browse the IP)"
echo "  3) Skip       (I'll expose it myself)"
HOW=$(ask "Choose 1-3" "1")

case "$HOW" in
  1)
    if ! command -v tailscale >/dev/null 2>&1; then
      warn "Tailscale isn't installed."
      read -rp "Install it now? [Y/n]: " ti
      [[ "${ti:-Y}" =~ ^[Yy] ]] && curl -fsSL https://tailscale.com/install.sh | sudo sh
    fi
    if command -v tailscale >/dev/null 2>&1; then
      tailscale status >/dev/null 2>&1 || { warn "Logging this box into Tailscale (click the printed link):"; sudo tailscale up || true; }
      if sudo tailscale serve --bg "$WEB_PORT" 2>/dev/null; then
        WIZ_URL="$(tailscale serve status 2>/dev/null | grep -oE 'https://[^ ]+' | head -1)"
      else
        warn "tailscale serve failed - try: sudo tailscale up   then re-run this script."
      fi
    fi
    ;;
  2)
    PUBIP="$(curl -s --max-time 4 https://api.ipify.org || hostname -I 2>/dev/null | awk '{print $1}')"
    sudo ufw allow "${WEB_PORT}"/tcp 2>/dev/null || true
    WIZ_URL="http://${PUBIP}:${WEB_PORT}"
    warn "Also open TCP ${WEB_PORT} in your cloud firewall/security-list (OCI: a VCN ingress rule)."
    ;;
esac

cat <<EOF

====================================================================
 OPEN THE SETUP WIZARD
====================================================================
 In any browser, go to:
     ${WIZ_URL:-<run on the server: sudo tailscale serve --bg ${WEB_PORT} ; then: tailscale serve status>}

 First login:  admin / Changeme001   -> set a new password -> fill in the wizard.
 Manage:       ${DC[*]} --env-file .env   ps | logs -f | down
====================================================================
EOF
