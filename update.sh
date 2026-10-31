#!/usr/bin/env bash
# InfraDocs in-place update — pull latest main, rebuild images, restart the stack IN PLACE,
# PRESERVING the Mongo volume (roster, tokens, audit, last-scan snapshots), this node's role/
# replica-set state (term, vote, membership) and the shared fleet secret. Does NOT re-run onboarding or wipe
# cluster state. Safe on the primary and on secondaries; works on single-node/standalone too.
#
#   bash update.sh            # update this node in place
#
# Replica sets: update the secondaries one at a time first, then step the primary down from
# the Servers page and update it last. A restarting member is just a short outage to the set.
set -uo pipefail

REPO_URL="${INFRADOCS_REPO_URL:-$(git -C "${INFRADOCS_DIR:-$HOME/infradocs}" remote get-url origin 2>/dev/null || echo https://github.com/looking4manish/InfraDocs_v7.git)}"
BRANCH="${INFRADOCS_BRANCH:-main}"
DEPLOY_DIR="${INFRADOCS_DIR:-$HOME/infradocs}"
DC_DIR="$DEPLOY_DIR/deploy/docker"

say()  { printf "\n\033[1;36m> %s\033[0m\n" "$1"; }
ok()   { printf "\033[1;32m✓ %s\033[0m\n" "$1"; }
warn() { printf "\033[1;33m! %s\033[0m\n" "$1"; }
die()  { printf "\033[1;31m✗ %s\033[0m\n" "$1" >&2; exit 1; }

[[ -d "$DEPLOY_DIR/.git" ]] || die "no InfraDocs checkout at $DEPLOY_DIR (set INFRADOCS_DIR)"
[[ -f "$DC_DIR/.env" ]]     || die "no deploy env at $DC_DIR/.env — is this node installed?"
command -v git    >/dev/null 2>&1 || die "git not found"
command -v docker >/dev/null 2>&1 || die "docker not found"
docker info >/dev/null 2>&1 && DC=(docker compose) || DC=(sudo docker compose)

cd "$DEPLOY_DIR" || die "cannot cd $DEPLOY_DIR"
OLD="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
say "Updating $DEPLOY_DIR (branch $BRANCH) — current commit $OLD"

# Pull latest main. Works whether origin is intact (normal mode) or severed (detached):
# fetch straight from the URL and hard-reset the tracked tree to it. Untracked files
# (deploy/docker/.env) are left in place; cluster STATE lives in the Mongo volume, not the tree.
git fetch "$REPO_URL" "$BRANCH" 2>/dev/null || die "git fetch failed (no network / bad remote)"
git reset --hard FETCH_HEAD >/dev/null 2>&1 || die "git reset to fetched $BRANCH failed"
NEW="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
[[ "$OLD" == "$NEW" ]] && ok "already at latest ($NEW) — rebuilding to apply any image drift" || ok "code $OLD -> $NEW"

# Rebuild + restart IN PLACE. No 'down -v' — named volumes (mongo_data/caddy_data) and thus
# roster, tokens, audit, snapshots, role, priority, and the fleet secret all survive. If the
# build fails, the previous containers keep running (the node is NOT left wedged).
say "Rebuilding images + restarting stack (volumes preserved)…"
( cd "$DC_DIR" && "${DC[@]}" --env-file .env up -d --build ) \
  || die "build/restart failed — previous containers left running, node not wedged. Check: ${DC[*]} --env-file .env logs"

# Preflight: API + Mongo + web must be healthy; report loudly with named reasons if not.
get() { grep -E "^$1=" "$DC_DIR/.env" | cut -d= -f2-; }
API_PORT="$(get API_PORT)"; API_PORT="${API_PORT:-8090}"
WEB_PORT="$(get WEB_PORT)"; WEB_PORT="${WEB_PORT:-8081}"
say "Preflight health check (API :$API_PORT · Mongo · web :$WEB_PORT)…"
api_ok=""; for _ in $(seq 1 30); do
  [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${API_PORT}/api/health" 2>/dev/null || true)" == "200" ]] && { api_ok=1; break; }
  sleep 2
done
mongo_ok=""; ( cd "$DC_DIR" && "${DC[@]}" --env-file .env ps mongo 2>/dev/null | grep -qiE "up|running|healthy" ) && mongo_ok=1
web_code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${WEB_PORT}/" 2>/dev/null || true)"
web_ok=""; [[ "$web_code" =~ ^(200|301|302|401|404)$ ]] && web_ok=1

( cd "$DC_DIR" && "${DC[@]}" --env-file .env ps ) || true
[[ -n "$api_ok"   ]] || die "preflight: API did not return 200 on /api/health — check: ${DC[*]} --env-file .env logs api"
[[ -n "$mongo_ok" ]] || die "preflight: Mongo container is not up — check: ${DC[*]} --env-file .env logs mongo"
[[ -n "$web_ok"   ]] || warn "preflight: web did not answer on :$WEB_PORT (got '$web_code'); API is up — check Caddy logs"

echo
ok "Update complete: $OLD -> $NEW. Stack healthy (API + Mongo up). Replica-set state preserved."
echo "  Manage: (cd \"$DC_DIR\" && ${DC[*]} --env-file .env ps | logs -f)"
