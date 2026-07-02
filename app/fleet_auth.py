"""Shared fleet secret — the fleet-wide SERVICE credential the node-switcher proxy presents
to a peer when it forwards a request on the operator's behalf. This app has no JWT/signing
secret (auth is opaque DB sessions + a per-node bootstrap credential), so cross-node auth is
solved with one shared secret rather than a shared signing key.

Stored per node in Mongo (db.fleet, _id=config), delivered to secondaries over the trusted,
join-token-gated enroll channel. It is a distinct principal ("fleet"), never a user session,
and is never logged in plaintext. Rotating it on the primary + re-syncing revokes old copies.
"""

import secrets as _secrets
from typing import Optional

_DOC = {"_id": "config"}


def get_fleet_secret(db) -> Optional[str]:
    doc = db.db.fleet.find_one(_DOC) or {}
    return doc.get("secret")


def set_fleet_secret(db, secret: str) -> None:
    db.db.fleet.update_one(_DOC, {"$set": {"secret": secret}}, upsert=True)


def get_or_create_fleet_secret(db) -> str:
    """Primary is the source of truth: mint + persist a random secret if absent."""
    doc = db.db.fleet.find_one(_DOC) or {}
    s = doc.get("secret")
    if not s:
        s = _secrets.token_urlsafe(32)
        db.db.fleet.update_one(_DOC, {"$set": {"secret": s}}, upsert=True)
    return s


def fleet_auth_ok(db, presented: Optional[str]) -> bool:
    if not presented:
        return False
    s = get_fleet_secret(db)
    return bool(s) and _secrets.compare_digest(presented, s)


def store_fleet_config(db, primary_url: str, join_token: str, secret: str) -> None:
    """On a secondary: persist the shared secret + the creds needed to re-sync it later."""
    db.db.fleet.update_one(_DOC, {"$set": {
        "secret": secret, "primary_url": primary_url, "join_token": join_token,
    }}, upsert=True)


def get_fleet_config(db) -> dict:
    return db.db.fleet.find_one(_DOC) or {}
