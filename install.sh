#!/usr/bin/env bash
#
# InfraDocs terminal installer — stand a node up, then onboard it your way.
#
#   curl -fsSL https://raw.githubusercontent.com/looking4manish/InfraDocs_V6/main/install.sh -o install.sh
#   bash install.sh
#
# (or just `bash install.sh` from inside an existing checkout). ONE installer with a
# shared bring-up and a fork at the very end:
#   shared  — preflight, clone/pull, render the node config, build + bring up the Docker
#             stack (API + Mongo + web), wait until it's healthy.
#   onboard — you choose HOW to finish:
#     • CLI: answer the prompts here. Pick a role — [P]rimary / [S]econdary / stand[A]lone:
#       - primary/secondary federate, so they need this node's reachable address (offered
#         as an auto-detected numbered pick-list — tailscale / LAN / localhost / manual —
#         so you don't type it from memory) and a secondary also needs the primary's
#         address + a join token. It runs the SAME priority-uniqueness + bidirectional
#         reachability checks the browser wizard does.
#       - standalone is a single box with NO cluster: no address, no peers, no checks.
#     • UI:  no terminal prompts — it prints the URL you open to finish setup in the
#       browser wizard, and exits leaving the node running-but-unconfigured. The wizard
#       collects the SAME fields and calls the SAME enroll/validation APIs — one config
#       format, both paths converge.
#
# Two failure classes are kept apart on purpose:
#   • A BRING-UP failure (the build/containers never come up healthy) tears the half-built
#     stack back down so the box is left clean.
#   • An ONBOARDING failure AFTER the stack is healthy (empty/invalid address, failed
#     reachability, duplicate priority, …) NEVER tears the good build down — it re-prompts,
#     and if you bail it leaves the stack UP and tells you how to finish later.
#
# Pick non-interactively with --onboard=cli|ui (or INFRADOCS_ONBOARD) and --role=primary|
# secondary|standalone (or INFRADOCS_ROLE) so scripted installs work. Mesh-agnostic: it
# never installs or assumes Tailscale/any VPN; the node is reachable at the ADDRESS you
# supply (CLI prompt) or fill into the wizard (UI).
set -euo pipefail

REPO_URL="${INFRADOCS_REPO_URL:-https://github.com/looking4manish/InfraDocs_V6.git}"
DEPLOY_DIR="${INFRADOCS_DIR:-$HOME/infradocs}"
BRANCH="${INFRADOCS_BRANCH:-main}"

# How to onboard once the stack is up: cli | ui | "" (ask). And the cluster role for the
# CLI path: primary | secondary | standalone | "" (ask). Flags win over env.
ONBOARD="${INFRADOCS_ONBOARD:-}"
ROLE_OVERRIDE="${INFRADOCS_ROLE:-}"
# Install mode: detached (default) = clone once + sever origin (your own copy);
# normal = stay synced to origin/$BRANCH (git reset --hard each run). "" => ask.
MODE="${INFRADOCS_MODE:-}"
# Scripted reachable address: --advertise-url / INFRADOCS_ADVERTISE_URL bypasses the picker.
ADVERTISE_OVERRIDE="${INFRADOCS_ADVERTISE_URL:-}"
# Web/API ports — config default 8081/8090, overridable via env or --web-port/--api-port.
# Preflighted for conflicts before bring-up so a busy port fails loudly / auto-suggests
# instead of crash-looping the web container.
WEB_PORT_SEL="${INFRADOCS_WEB_PORT:-8081}"; WEB_EXPLICIT=0; [[ -n "${INFRADOCS_WEB_PORT:-}" ]] && WEB_EXPLICIT=1
API_PORT_SEL="${INFRADOCS_API_PORT:-8090}"; API_EXPLICIT=0; [[ -n "${INFRADOCS_API_PORT:-}" ]] && API_EXPLICIT=1
# Auto-pick scans upward from the default and binds the first free port, capped at this ceiling.
PORT_CEIL="${INFRADOCS_PORT_CEIL:-8099}"
# Interactive unless INFRADOCS_NONINTERACTIVE=1 or there is no TTY. =0 forces interactive.
NONINTERACTIVE=0
[[ "${INFRADOCS_NONINTERACTIVE:-}" == "1" ]] && NONINTERACTIVE=1
[[ -z "${INFRADOCS_NONINTERACTIVE:-}" && ! -t 0 ]] && NONINTERACTIVE=1
for arg in "$@"; do
  case "$arg" in
    --onboard=*)       ONBOARD="${arg#*=}" ;;
    --onboard)         echo "use --onboard=cli or --onboard=ui" >&2; exit 1 ;;
    --role=*)          ROLE_OVERRIDE="${arg#*=}" ;;
    --role)            echo "use --role=primary|secondary|standalone" >&2; exit 1 ;;
    --mode=*)          MODE="${arg#*=}" ;;
    --mode)            echo "use --mode=detached or --mode=normal" >&2; exit 1 ;;
    --detached)        MODE=detached ;;
    --normal)          MODE=normal ;;
    --advertise-url=*) ADVERTISE_OVERRIDE="${arg#*=}" ;;
    --advertise-url)   echo "use --advertise-url=http://HOST-OR-IP:PORT" >&2; exit 1 ;;
    --web-port=*)      WEB_PORT_SEL="${arg#*=}"; WEB_EXPLICIT=1 ;;
    --web-port)        echo "use --web-port=N" >&2; exit 1 ;;
    --api-port=*)      API_PORT_SEL="${arg#*=}"; API_EXPLICIT=1 ;;
    --api-port)        echo "use --api-port=N" >&2; exit 1 ;;
  esac
done

step() { printf "\n\033[1;36m== %s ==\033[0m\n" "$1"; }
ok()   { printf "\033[1;32m  ✓ %s\033[0m\n" "$1"; }
warn() { printf "\033[1;33m  ! %s\033[0m\n" "$1"; }
err()  { printf "\033[1;31m  ✗ %s\033[0m\n" "$1" >&2; }
ask()  { local p="$1" d="${2:-}" v; if [[ -n "$d" ]]; then read -rp "  $p [$d]: " v; printf '%s' "${v:-$d}"; else read -rp "  $p: " v; printf '%s' "$v"; fi; }
askyn(){ local v; read -rp "  $1 [y/N]: " v; [[ "$v" =~ ^[Yy] ]]; }

PORT_RESULT=""
# resolve_port LABEL PORT EXPLICIT FLAGMSG — pick the port to bind, before bring-up.
#   free                      -> PORT_RESULT=PORT
#   taken + explicit request  -> die (they asked for THAT port; don't silently pick another)
#   taken + default (either mode, incl. non-interactive) -> AUTO-SELECT the next free port in
#                                the 80xx range and bind it, unattended
#   taken + none free <= ceil -> die with a named reason (never loop forever)
resolve_port() {
  local label="$1" port="$2" explicit="$3" flagmsg="$4" picked cnt
  if "${HELP[@]}" port-check --port "$port" >/dev/null 2>&1; then
    PORT_RESULT="$port"; return 0
  fi
  if [[ "$explicit" == "1" ]]; then
    die "$label port $port was explicitly requested but is already in use on this host (by another process/container) — free it, or choose another with $flagmsg"
  fi
  cnt=$(( PORT_CEIL - port + 1 )); (( cnt < 1 )) && cnt=20
  picked="$("${HELP[@]}" find-free-port --from "$port" --count "$cnt" 2>/dev/null || true)"
  if [[ -z "$picked" ]]; then
    die "$label port $port is in use and no free port was found in range ${port}-${PORT_CEIL} — free a port, or set $flagmsg"
  fi
  ok "$label port $port in use → auto-selected $picked (serving on :$picked)"
  PORT_RESULT="$picked"
}

# ask_required PROMPT — for onboarding fields that must not be empty. Re-prompts until a
# non-empty value is given; 'q' (or EOF / no TTY) returns 1 so the caller can bail and
# LEAVE THE HEALTHY STACK UP. The prompt is shown on the terminal (stderr); only the
# chosen value goes to stdout for $(...) capture.
ask_required() {
  local p="$1" v
  while :; do
    if ! read -rp "  $p (or 'q' to finish later): " v; then
      return 1   # EOF / no TTY — can't keep prompting; treat as "finish later"
    fi
    case "$v" in
      q|Q) return 1 ;;
      "")  err "this can't be empty — re-enter, or 'q' to finish onboarding later" ;;
      *)   printf '%s' "$v"; return 0 ;;
    esac
  done
}

# pick_advertise_url — choose THIS node's reachable address for primary/secondary onboarding.
# Auto-detects candidates (tailscale / LAN) and shows a numbered pick-list with an explicit
# localhost ("this machine only") option and a manual escape hatch; default is the first
# (best peer-reachable) candidate, never localhost. Echoes the chosen URL to stdout; the
# menu/prompts go to stderr so $(...) capture stays clean. Returns 1 on 'q' / EOF (the
# caller leaves the healthy stack up). A scripted override and the no-candidates fallback
# both route to the same field as the old free-text prompt.
# Render the SHARED detected address candidates (cli_install detect-addresses) to stdout in
# spec order: tailscale/LAN first, then localhost ("this host only"), then any public IPv4
# (caveated). Used by BOTH the CLI picker and the UI-wizard message so they show the SAME set.
print_address_candidates() {
  local line url label kind peer=() pub=() e u l any=""
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    IFS=$'\t' read -r url label kind <<<"$line"
    if [[ "$kind" == "public" ]]; then pub+=("$url|$label"); else peer+=("$url|$label"); fi
    any=1
  done < <("${HELP[@]}" detect-addresses --web-port "$WEB_PORT" 2>/dev/null || true)
  for e in ${peer[@]+"${peer[@]}"}; do u="${e%%|*}"; l="${e#*|}"; printf "     %-30s (%s)\n" "$u" "$l"; done
  printf "     %-30s (%s)\n" "http://localhost:${WEB_PORT}" "this host only"
  for e in ${pub[@]+"${pub[@]}"};  do u="${e%%|*}"; l="${e#*|}"; printf "     %-30s (%s)\n" "$u" "$l"; done
  [[ -z "$any" ]] && printf "     (no address auto-detected — open http://localhost:%s here, or type one)\n" "$WEB_PORT"
  return 0
}

pick_advertise_url() {
  if [[ -n "$ADVERTISE_OVERRIDE" ]]; then printf '%s' "$ADVERTISE_OVERRIDE"; return 0; fi

  local line url label kind
  local peer_u=() peer_l=() pub_u=() pub_l=()
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    IFS=$'\t' read -r url label kind <<<"$line"
    if [[ "$kind" == "public" ]]; then pub_u+=("$url"); pub_l+=("$label"); else peer_u+=("$url"); peer_l+=("$label"); fi
  done < <("${HELP[@]}" detect-addresses --web-port "$WEB_PORT" 2>/dev/null || true)

  # Nothing peer-usable detected → manual free-text prompt (old behaviour).
  if (( ${#peer_u[@]} + ${#pub_u[@]} == 0 )); then
    ask_required "This node's reachable address — what other nodes + browsers use (e.g. http://HOST-OR-IP:${WEB_PORT})"
    return $?
  fi

  # spec order: tailscale/LAN, localhost, public — localhost/public are never the [1] default.
  local urls=() labels=()
  urls+=(${peer_u[@]+"${peer_u[@]}"}); labels+=(${peer_l[@]+"${peer_l[@]}"})
  urls+=("http://localhost:${WEB_PORT}"); labels+=("this host only")
  urls+=(${pub_u[@]+"${pub_u[@]}"});  labels+=(${pub_l[@]+"${pub_l[@]}"})

  local n=${#urls[@]} manual_num=$(( ${#urls[@]} + 1 )) i choice
  while :; do
    {
      echo "This node's reachable address — pick how other nodes + browsers reach this box:"
      for i in "${!urls[@]}"; do
        printf "    %d) %-30s (%s)\n" "$(( i + 1 ))" "${urls[$i]}" "${labels[$i]}"
      done
      printf "    %d) enter manually\n" "$manual_num"
    } >&2
    if ! read -rp "  Choose [1] (or 'q' to finish later): " choice; then return 1; fi
    choice="${choice:-1}"
    case "$choice" in
      q|Q)        return 1 ;;
      *[!0-9]*|"") err "enter a number 1-${manual_num}, or 'q' to finish later"; continue ;;
    esac
    if (( choice >= 1 && choice <= n )); then
      printf '%s' "${urls[$(( choice - 1 ))]}"; return 0
    elif (( choice == manual_num )); then
      ask_required "Enter this node's reachable address (e.g. http://HOST-OR-IP:${WEB_PORT})"
      return $?
    else
      err "enter a number 1-${manual_num}, or 'q' to finish later"
    fi
  done
}

DEPLOYED=""        # set once the stack is up, so a bring-up failure tears it back down
STACK_HEALTHY=""   # set once the stack is confirmed healthy; from here a failed onboarding
                   # step must NEVER nuke the good build — re-prompt or leave it running.
cleanup() {
  # Only tear down a HALF-BUILT stack (bring-up failed). A healthy build is never nuked.
  if [[ -n "$DEPLOYED" && -z "$STACK_HEALTHY" && -f "$DEPLOY_DIR/deploy/docker/.env" ]]; then
    warn "tearing down the half-installed stack so the box is left clean…"
    (cd "$DEPLOY_DIR/deploy/docker" && docker compose --env-file .env down -v >/dev/null 2>&1) || true
  fi
}
die() { err "$1"; echo "" >&2; err "Installation stopped."; cleanup; exit 1; }

# onboard_quit — the operator bailed out of onboarding AFTER the stack came up healthy.
# The build stays UP (never torn down for a recoverable onboarding error); print how to
# finish onboarding later, then exit cleanly.
onboard_quit() {
  step "Stack up — finish onboarding later"
  warn "onboarding not completed — the stack is UP and running, just not yet configured."
  echo "  Your build is intact; nothing was torn down. Finish onboarding any time:"
  echo "    • CLI:  re-run the installer and onboard again —"
  echo "            (cd \"$DEPLOY_DIR\" && bash install.sh --onboard=cli)"
  echo "    • UI:   open the setup wizard in a browser —"
  echo "            on this host: $LOCAL_WEB   ·   from another machine, use an address it can reach:"
  print_address_candidates
  echo "            log in admin / $ADMIN_PW (change it) and complete the wizard."
  echo "  Manage: (cd \"$DEPLOY_DIR/deploy/docker\" && docker compose --env-file .env ps|logs -f|down)"
  exit 0
}

# Normalise the overrides early so a typo fails before we build anything.
case "$ONBOARD" in
  ""|cli|ui) : ;;
  *) die "invalid --onboard='$ONBOARD' (use cli or ui)" ;;
esac
case "$ROLE_OVERRIDE" in
  ""|primary|secondary|standalone) : ;;
  *) die "invalid --role='$ROLE_OVERRIDE' (use primary, secondary, or standalone)" ;;
esac

# --- 1. preflight --------------------------------------------------------
step "Preflight checks"
for tool in git curl docker; do
  command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool — install it and re-run"
done
docker compose version >/dev/null 2>&1 || die "the 'docker compose' plugin is missing"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running? are you in the 'docker' group?)"
ok "git, curl, docker + compose present; Docker daemon reachable"

# --- 2. fetch the repo ---------------------------------------------------
# Resolve install mode (detached is the default). Detached downloads the code ONCE then
# severs the link to origin, so this becomes YOUR local copy and re-runs never overwrite
# your edits. Normal keeps it synced to origin/$BRANCH (hard-reset on every run).
case "$MODE" in
  ""|detached|normal) : ;;
  *) die "invalid --mode='$MODE' (use detached or normal)" ;;
esac
step "Install mode"
if [[ -z "$MODE" ]]; then
  a="$(ask "Install mode — [D]etached (your own local copy, no link to origin) or [N]ormal (stay synced to origin/$BRANCH)?" "D")"
  case "$a" in
    N|n|normal|NORMAL) MODE=normal ;;
    *)                 MODE=detached ;;
  esac
fi
ok "mode = $MODE"

step "Fetch InfraDocs ($BRANCH)"
if [[ "$MODE" == "normal" ]]; then
  if [[ -d "$DEPLOY_DIR/.git" ]]; then
    git -C "$DEPLOY_DIR" fetch --depth 1 origin "$BRANCH" >/dev/null 2>&1 \
      && git -C "$DEPLOY_DIR" checkout -q "$BRANCH" \
      && git -C "$DEPLOY_DIR" reset --hard -q "origin/$BRANCH" \
      || die "failed to update the existing checkout at $DEPLOY_DIR"
    ok "updated $DEPLOY_DIR (synced to origin/$BRANCH)"
  else
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$DEPLOY_DIR" >/dev/null 2>&1 \
      || die "git clone failed ($REPO_URL)"
    ok "cloned $DEPLOY_DIR (tracking origin/$BRANCH)"
  fi
else
  # detached: one-time download, then cut the cord to origin so this copy is the operator's.
  if [[ -d "$DEPLOY_DIR/.git" ]]; then
    git -C "$DEPLOY_DIR" remote remove origin >/dev/null 2>&1 || true
    ok "using existing checkout at $DEPLOY_DIR — detached (your local changes are kept, not re-pulled)"
  else
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$DEPLOY_DIR" >/dev/null 2>&1 \
      || die "git clone failed ($REPO_URL)"
    git -C "$DEPLOY_DIR" remote remove origin >/dev/null 2>&1 || true
    ok "cloned $DEPLOY_DIR and severed origin — this copy is yours to modify (installer won't overwrite it)"
  fi
fi
cd "$DEPLOY_DIR"
PY="python3"; [[ -x "$DEPLOY_DIR/venv/bin/python" ]] && PY="$DEPLOY_DIR/venv/bin/python"
HELP=( env "PYTHONPATH=$DEPLOY_DIR" "$PY" -m app.cli_install )

# --- 3. node identity (all that bring-up needs) --------------------------
# Just the node's short id — enough to render the .env and stand the stack up. The
# onboarding details (role / priority / reachable address / join) come later, and
# only on the path you choose. Defaults to the hostname for fully-scripted installs.
step "Node identity"
SERVER_ID="${INFRADOCS_SERVER_ID:-$(ask "Short node id (e.g. oci, n150)" "$(hostname -s 2>/dev/null || echo node)")}"
[[ -n "$SERVER_ID" ]] || die "a node id is required"
ok "node id = $SERVER_ID"

# --- 3b. preflight the web + API ports BEFORE writing .env / bringing up -
step "Preflight ports"
resolve_port "web" "$WEB_PORT_SEL" "$WEB_EXPLICIT" "--web-port N or INFRADOCS_WEB_PORT=N"; WEB_PORT_SEL="$PORT_RESULT"
resolve_port "API" "$API_PORT_SEL" "$API_EXPLICIT" "--api-port N or INFRADOCS_API_PORT=N"; API_PORT_SEL="$PORT_RESULT"
ok "ports free — web $WEB_PORT_SEL, API $API_PORT_SEL"

# --- 4. write the node config -------------------------------------------
step "Write node config"
export INSTALL_SERVER_ID="$SERVER_ID" INSTALL_SERVER_NAME="$SERVER_ID"
export INSTALL_WEB_PORT="$WEB_PORT_SEL" INSTALL_API_PORT="$API_PORT_SEL"
"${HELP[@]}" render-env --out "$DEPLOY_DIR/deploy/docker/.env" || die "failed to write deploy/docker/.env"
chmod 600 "$DEPLOY_DIR/deploy/docker/.env"
ok "wrote deploy/docker/.env"

# --- 5. bring up the stack (shared by both onboarding paths) ------------
step "Deploy the Docker stack"
INFRADOCS_NONINTERACTIVE=1 bash "$DEPLOY_DIR/deploy/docker/deploy.sh" || die "docker deploy failed"
DEPLOYED=1
API_PORT="$(grep -E '^API_PORT=' deploy/docker/.env | cut -d= -f2-)"; API_PORT="${API_PORT:-8090}"
WEB_PORT="$(grep -E '^WEB_PORT=' deploy/docker/.env | cut -d= -f2-)"; WEB_PORT="${WEB_PORT:-8081}"
ADMIN_PW="$(grep -E '^ADMIN_PASSWORD=' deploy/docker/.env | cut -d= -f2-)"; ADMIN_PW="${ADMIN_PW:-Changeme001}"
LOCAL_API="http://localhost:${API_PORT}"
LOCAL_WEB="http://localhost:${WEB_PORT}"
printf "  waiting for the local API"
for _ in $(seq 1 45); do
  [[ "$(curl -s -o /dev/null -w '%{http_code}' "$LOCAL_API/api/health" 2>/dev/null)" == "200" ]] && { echo; break; }
  printf "."; sleep 2
done
[[ "$(curl -s -o /dev/null -w '%{http_code}' "$LOCAL_API/api/health" 2>/dev/null)" == "200" ]] \
  || die "the local API never became healthy on $LOCAL_API"
ok "API + Mongo healthy"
# The web container must be RUNNING (a port-bind failure => Restarting/Exited crash-loop),
# not just "created", and must actually serve InfraDocs on WEB_PORT — not some OTHER process
# that already holds the port (whose 200 would otherwise masquerade as a healthy UI).
web_ps="$(cd "$DEPLOY_DIR/deploy/docker" && docker compose --env-file .env ps web 2>/dev/null || true)"
if printf '%s' "$web_ps" | grep -qiE "restarting|exited|unhealthy"; then
  die "web container is not running (state shows restarting/exited) — web port ${WEB_PORT} is likely already in use on this host. Re-run with --web-port N or INFRADOCS_WEB_PORT=N"
fi
printf "  waiting for the web UI on :${WEB_PORT}"
for _ in $(seq 1 45); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$LOCAL_WEB/api/health" 2>/dev/null)"
  [[ "$code" == "200" ]] && { echo; break; }
  printf "."; sleep 2
done
code="$(curl -s -o /dev/null -w '%{http_code}' "$LOCAL_WEB/api/health" 2>/dev/null)"
[[ "$code" == "200" ]] \
  || die "the web UI on $LOCAL_WEB is not serving InfraDocs (HTTP $code) — is web port ${WEB_PORT} taken by another process? Re-run with --web-port N or INFRADOCS_WEB_PORT=N"
# From here the build is GOOD — onboarding hiccups must never tear it down.
STACK_HEALTHY=1
ok "stack is up and healthy (API + Mongo + web)"

# --- 6. choose the onboarding method ------------------------------------
step "Onboarding method"
if [[ -z "$ONBOARD" ]]; then
  while [[ "$ONBOARD" != "cli" && "$ONBOARD" != "ui" ]]; do
    a="$(ask "Onboard this node via [C]LI prompts here, or [U]I wizard in a browser?" "C")"
    case "$a" in
      C|c|cli|CLI) ONBOARD="cli" ;;
      U|u|ui|UI)   ONBOARD="ui" ;;
      *)           err "answer C or U" ;;
    esac
  done
fi
ok "onboarding via: $ONBOARD"

# --- 7. FORK: onboard via CLI here, or hand off to the UI wizard --------
if [[ "$ONBOARD" == "cli" ]]; then
  step "Onboard via CLI"
  # Role — primary / secondary / standalone. Override via --role= / INFRADOCS_ROLE.
  ROLE="$ROLE_OVERRIDE"
  while [[ "$ROLE" != "primary" && "$ROLE" != "secondary" && "$ROLE" != "standalone" ]]; do
    a="$(ask "Node role — [P]rimary, [S]econdary, or stand[A]lone (one box, no cluster)?" "P")"
    case "$a" in
      P|p|primary)    ROLE="primary" ;;
      S|s|secondary)  ROLE="secondary" ;;
      A|a|standalone) ROLE="standalone" ;;
      *)              err "answer P, S, or A" ;;
    esac
  done
  ok "role = $ROLE"

  PRIORITY=""; PRIMARY_URL=""; JOIN_TOKEN=""; ADVERTISE_URL=""

  if [[ "$ROLE" == "standalone" ]]; then
    # Single node: NO reachable address, NO peers/token, NO reachability check. Just configure.
    step "Configure standalone node"
    if "${HELP[@]}" complete --api "$LOCAL_API" --web-port "$WEB_PORT" --user admin --password "$ADMIN_PW" \
         --role standalone --server-name "$SERVER_ID"; then
      PRIORITY=1
      ok "standalone node configured — runs locally, no cluster, no peers"
    else
      err "could not write the standalone config (see reason above)"
      onboard_quit
    fi

    step "Installed — standalone node ready (CLI)"
    echo "  role:      standalone (single node — no federation)"
    echo "  node id:   $SERVER_ID"
    echo ""
    echo "  Open the dashboard at: $LOCAL_WEB   (login: admin / $ADMIN_PW — change it)"
    echo "  From another machine, use an address it can reach:"
    print_address_candidates
    echo "  Manage: (cd \"$DEPLOY_DIR/deploy/docker\" && docker compose --env-file .env ps|logs -f|down)"
  else
    # primary / secondary — a clustered node genuinely needs a reachable address. Every
    # field below RE-PROMPTS on bad input; 'q'/EOF leaves the healthy stack up (onboard_quit).
    PRIORITY=1   # primary; a secondary picks its own below
    ADVERTISE_URL="$(pick_advertise_url)" || onboard_quit

    if [[ "$ROLE" == "secondary" ]]; then
      PRIORITY=""
      while :; do
        PRIMARY_URL="$(ask_required "The primary's reachable address (e.g. http://PRIMARY-HOST:${WEB_PORT})")" || onboard_quit
        if "${HELP[@]}" check-primary --primary-url "$PRIMARY_URL"; then
          ok "primary reachable (secondary → primary)"; break
        fi
        err "cannot reach the primary at $PRIMARY_URL — check the address / firewall, then re-enter"
      done
      JOIN_TOKEN="$(ask_required "Join token (mint one on the primary)")" || onboard_quit
      while :; do
        PRIORITY="$(ask_required "Failover priority 1-99 (1 = highest; must be free)")" || onboard_quit
        if "${HELP[@]}" check-priority --primary-url "$PRIMARY_URL" --priority "$PRIORITY"; then
          ok "priority $PRIORITY is valid and free"; break
        fi
        err "choose a different priority"
      done
    fi

    step "Enroll this node"
    while :; do
      if [[ "$ROLE" == "secondary" ]]; then
        if "${HELP[@]}" complete --api "$LOCAL_API" --web-port "$WEB_PORT" --user admin --password "$ADMIN_PW" \
             --role secondary --server-name "$SERVER_ID" --advertise-url "$ADVERTISE_URL" \
             --priority "$PRIORITY" --primary-url "$PRIMARY_URL" --join-token "$JOIN_TOKEN"; then
          ok "enrolled — reachability confirmed both directions"; break
        fi
        err "enrollment refused (reason above). The primary must reach this node back at $ADVERTISE_URL"
      else
        if "${HELP[@]}" complete --api "$LOCAL_API" --web-port "$WEB_PORT" --user admin --password "$ADMIN_PW" \
             --role primary --server-name "$SERVER_ID" --advertise-url "$ADVERTISE_URL"; then
          ok "this node is the cluster primary"; break
        fi
        err "could not write the primary's config (see reason above)"
      fi
      # Recoverable: the stack stays up. Fix the inputs and retry, or finish later.
      askyn "Re-enter onboarding details and retry? (n = leave the stack up, finish later)" || onboard_quit
      ADVERTISE_URL="$(pick_advertise_url)" || onboard_quit
      if [[ "$ROLE" == "secondary" ]]; then
        while :; do
          PRIORITY="$(ask_required "Failover priority 1-99 (1 = highest; must be free)")" || onboard_quit
          if "${HELP[@]}" check-priority --primary-url "$PRIMARY_URL" --priority "$PRIORITY"; then
            ok "priority $PRIORITY is valid and free"; break
          fi
          err "choose a different priority"
        done
      fi
    done

    step "Installed — node configured + joined (CLI)"
    echo "  role:      $ROLE"
    echo "  priority:  $PRIORITY"
    echo "  address:   $ADVERTISE_URL"
    [[ "$ROLE" == "secondary" ]] && echo "  primary:   $PRIMARY_URL  (reachable, confirmed both directions)"
    echo ""
    echo "  Open the dashboard at: $ADVERTISE_URL   (login: admin / $ADMIN_PW — change it)"
    if [[ "$ROLE" == "primary" ]]; then
      echo "  Mint a join token for each secondary from the Servers lens (or POST /api/federation/tokens)."
    fi
    echo "  Manage: (cd \"$DEPLOY_DIR/deploy/docker\" && docker compose --env-file .env ps|logs -f|down)"
  fi
else
  # ---- UI path: no terminal prompts. Stack is already up; print the wizard URL and exit.
  # The operator fills in the SAME fields (role / priority / reachable address / join) in
  # the browser wizard, which calls the SAME /api/setup/complete enroll + validation APIs.
  step "Onboard via UI wizard"
  ok "node is running — finish setup in the browser (no terminal prompts)"
  # The address other machines use is operator-supplied; here we only know the local URL.
  WIZ_URL="${INFRADOCS_ADVERTISE_URL:-$LOCAL_WEB}"
  CONFIGURED_LINE=""
  [[ -n "${INFRADOCS_ADVERTISE_URL:-}" ]] && CONFIGURED_LINE="   Configured:      $INFRADOCS_ADVERTISE_URL"
  cat <<EOF

  ====================================================================
   OPEN THE SETUP WIZARD
  ====================================================================
   On this host:    $LOCAL_WEB
   From another machine, open the address it can reach (top pick first):
$(print_address_candidates)
${CONFIGURED_LINE:+$CONFIGURED_LINE
}
   Login: admin / $ADMIN_PW  (change it), then the wizard collects role /
   priority / this node's reachable address / (secondary: primary address +
   join token) and enrolls the node — the SAME fields and APIs the CLI path
   uses. The node stays running but UNCONFIGURED until you finish there.
  ====================================================================
EOF

  step "Stack up — open the URL above to finish setup (UI)"
  echo "  onboarding:  UI wizard (node is up but NOT yet configured)"
  echo "  open:        $WIZ_URL"
  echo "  next:        log in, complete the wizard — it enrolls the node"
  echo "  Manage: (cd \"$DEPLOY_DIR/deploy/docker\" && docker compose --env-file .env ps|logs -f|down)"
fi
