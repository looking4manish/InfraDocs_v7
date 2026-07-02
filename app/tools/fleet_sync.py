"""Converge an already-enrolled secondary onto the shared fleet secret WITHOUT a re-enroll
or wipe. Reads this node's stored primary_url + join_token (db.fleet, written at enroll),
fetches the current secret from the primary's gated /api/federation/fleet-secret, and stores
it locally. No federation config (a primary or single-node install) → clean no-op. Invoked by
update.sh; safe to re-run.
"""

import json
import sys
import urllib.request

from app.core.config_loader import load_config
from app.core.db_manager import DBManager
from app.fleet_auth import get_fleet_config, set_fleet_secret


def main() -> int:
    cfg = load_config("config.yml")
    db = DBManager(uri=cfg.mongodb.uri, database=cfg.mongodb.database)
    fc = get_fleet_config(db)
    primary_url, token = fc.get("primary_url"), fc.get("join_token")
    if not primary_url or not token:
        print("fleet-sync: no federation config on this node — skipping (primary/single-node).")
        return 0
    from app.cli_install import normalize_url
    url = normalize_url(primary_url).rstrip("/") + "/api/federation/fleet-secret"
    body = json.dumps({"server_id": cfg.server.id}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Join-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
            sec = json.loads(r.read().decode()).get("fleet_secret")
    except Exception as e:  # noqa: BLE001
        print(f"fleet-sync: could not reach primary to sync the secret: {e}", file=sys.stderr)
        return 1
    if sec:
        set_fleet_secret(db, sec)
        print("fleet-sync: shared fleet secret synced from primary.")
        return 0
    print("fleet-sync: primary returned no secret.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
