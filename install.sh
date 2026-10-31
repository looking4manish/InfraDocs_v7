#!/usr/bin/env bash
# InfraDocs — install on the FIRST server of a replica set.
#
#   curl -fsSL https://raw.githubusercontent.com/looking4manish/InfraDocs_v7/main/install.sh | bash
#   # or, from a checkout:  bash install.sh [--name oci] [--projects ~/projects]
#
# Clones (or updates) the repository into ~/infradocs and runs deploy/docker/deploy.sh, which
# checks the server, starts the stack and prints the address and setup code for the browser.
# Later servers join from the Servers page ("Add a server"), not with this script.
set -euo pipefail
REPO="${INFRADOCS_REPO:-https://github.com/looking4manish/InfraDocs_v7.git}"
BRANCH="${INFRADOCS_BRANCH:-main}"
DIR="${INFRADOCS_DIR:-$HOME/infradocs}"

here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$here" && -f "$here/deploy/docker/deploy.sh" ]]; then
  exec bash "$here/deploy/docker/deploy.sh" "$@"
fi
command -v git >/dev/null || { echo "git is required: sudo apt install -y git"; exit 1; }
if [[ -d "$DIR/.git" ]]; then
  git -C "$DIR" pull --ff-only --quiet
else
  git clone --branch "$BRANCH" --depth 1 "$REPO" "$DIR" || {
    echo "Clone failed. For a private repository use SSH: INFRADOCS_REPO=git@github.com:looking4manish/InfraDocs_v7.git"; exit 1; }
fi
exec bash "$DIR/deploy/docker/deploy.sh" "$@"
