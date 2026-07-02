"""Federation — a primary aggregates secondaries' scans over the Tailscale mesh.

Every node can reach every other node directly (tailnet), so the old outbound
command-queue + poll + reap machinery has been removed. What remains here is the
data plane (a primary ingesting a secondary's scan) + token minting + the server
list. Enrollment reachability lives below (added with the direct model); leader
election lives in app/cluster_lease.py.
"""

import secrets
from datetime import datetime, timezone

from app.fleet_auth import get_or_create_fleet_secret
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app import cluster as CC
from app import federation as F
from app.cli_install import normalize_url
from app.api.dependencies import get_config, get_db, verify_auth
from app.core.config_loader import Config
from app.core.db_manager import DBManager

router = APIRouter()


class MintRequest(BaseModel):
    server_id: str


@router.post("/tokens")
def mint_token(req: MintRequest, actor: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    """Primary mints a join token for a new secondary (shown in 'Add a server')."""
    token = secrets.token_urlsafe(24)
    db.db.join_tokens.insert_one({
        "token": token,
        "server_id": req.server_id,
        "created_at": datetime.now(timezone.utc),
        "created_by": actor,
    })
    return {"token": token, "server_id": req.server_id}


@router.get("/tokens")
def list_tokens(_: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    """List outstanding join tokens (for the Admin tab lifecycle UI). The token is shown
    truncated — enough to identify + revoke, not to leak the full secret in a list view."""
    out = []
    for t in db.db.join_tokens.find({}, {"_id": 0}).sort("created_at", -1):
        tok = t.get("token", "")
        out.append({
            "token": tok,
            "token_preview": (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else tok,
            "server_id": t.get("server_id"),
            "created_at": t.get("created_at"),
            "created_by": t.get("created_by"),
        })
    return {"tokens": out, "count": len(out)}


@router.delete("/tokens/{token}")
def revoke_token(token: str, _: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    """Revoke a join token so it can no longer enroll a secondary (same token store the
    installer's enroll validates against — revoking here closes that path everywhere)."""
    res = db.db.join_tokens.delete_one({"token": token})
    return {"ok": res.deleted_count > 0, "revoked": res.deleted_count}


@router.get("/servers")
def list_servers(_: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    servers = list(db.db.federation_servers.find({}, {"_id": 0}))
    return {"servers": servers, "count": len(servers)}


def _valid_token(db: DBManager, server_id: str, token: Optional[str]) -> bool:
    if not token:
        return False
    rec = db.db.join_tokens.find_one({"token": token})
    return bool(rec and rec.get("server_id") == server_id)


class IngestRequest(BaseModel):
    server_id: str
    assets: List[dict] = []
    applications: List[dict] = []
    scan_meta: Optional[dict] = None


@router.post("/ingest")
def ingest(
    req: IngestRequest,
    x_join_token: Optional[str] = Header(None),
    db: DBManager = Depends(get_db),
):
    if not _valid_token(db, req.server_id, x_join_token):
        raise HTTPException(status_code=401, detail="invalid or missing join token")
    sid = req.server_id

    def _clean(docs):
        for d in docs:
            d.pop("_id", None)        # let the primary assign its own ids
            d["server_id"] = sid       # enforce scope — a token can't write another server
        return docs

    # Scoped replace: swap out just this server's slice, leave others intact.
    db.db.assets.delete_many({"server_id": sid})
    assets = _clean(req.assets)
    if assets:
        db.db.assets.insert_many(assets)
    db.db.applications.delete_many({"server_id": sid})
    apps = _clean(req.applications)
    if apps:
        db.db.applications.insert_many(apps)

    db.db.federation_servers.update_one(
        {"server_id": sid},
        {"$set": {
            "server_id": sid,
            "last_seen": datetime.now(timezone.utc),
            "last_synced_at": datetime.now(timezone.utc),
            "asset_count": len(assets),
            "app_count": len(apps),
        }},
        upsert=True,
    )
    return {"ok": True, "server_id": sid, "assets": len(assets), "applications": len(apps)}


# ===========================================================================
# Direct (mesh) model: reachability handshake + leader election.
# ===========================================================================


@router.get("/ping")
def ping(cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """Unauthenticated identity — used by the primary's back-connection during the
    enroll handshake. Returns only non-sensitive identity info."""
    self_doc = db.db.cluster.find_one({"_id": "self"}) or {}
    return {"ok": True, "server_id": cfg.server.id,
            "role": _settings(db).get("role"), "is_primary": bool(self_doc.get("is_primary"))}


def _settings(db: DBManager) -> dict:
    return db.db.settings.find_one({"_id": "app"}) or {}


def _known_priorities(db: DBManager) -> dict:
    """node_id -> priority across this node's roster + its own self record."""
    out = {}
    self_doc = db.db.cluster.find_one({"_id": "self"}) or {}
    if self_doc.get("node_id") and self_doc.get("priority") is not None:
        out[self_doc["node_id"]] = self_doc["priority"]
    for n in db.db.cluster_nodes.find({}, {"_id": 0, "node_id": 1, "priority": 1}):
        if n.get("priority") is not None:
            out[n["node_id"]] = n["priority"]
    return out


class EnrollRequest(BaseModel):
    server_id: str
    secondary_url: str   # the secondary's own reachable address, for the back-connection
    join_token: str
    priority: int        # 1-99; the primary rejects a priority already taken


@router.post("/enroll")
def enroll(req: EnrollRequest, db: DBManager = Depends(get_db)):
    """Primary-side of enrollment: validate the token, REJECT a duplicate priority,
    then prove BIDIRECTIONAL reachability (the request proves secondary->primary; we
    connect BACK to prove primary->secondary). Recorded only if all three pass."""
    if not _valid_token(db, req.server_id, req.join_token):
        raise HTTPException(status_code=401, detail="invalid or missing join token")
    if not (1 <= req.priority <= 99):
        raise HTTPException(status_code=400, detail="priority must be between 1 and 99")
    # Priority uniqueness — the primary holds the authoritative roster.
    known = _known_priorities(db)
    if CC.priority_in_use(
        {nid: {"priority": p} for nid, p in known.items()}, req.priority, exclude=req.server_id
    ):
        taken_by = next(nid for nid, p in known.items() if p == req.priority and nid != req.server_id)
        raise HTTPException(status_code=409,
                            detail=f"priority {req.priority} already in use (by '{taken_by}') — pick a free one")

    secondary_url = normalize_url(req.secondary_url)  # portless -> :PORT before probe + store
    secondary_to_primary = True  # this request reached us
    primary_to_secondary = False
    reason = None
    try:
        pong = F.ping_node(secondary_url)
        if pong.get("server_id") and pong.get("server_id") != req.server_id:
            reason = (f"primary→secondary: reached {secondary_url} but it identifies as "
                      f"'{pong.get('server_id')}', not '{req.server_id}'")
        else:
            primary_to_secondary = True
    except Exception as e:  # noqa: BLE001
        reason = "primary→secondary: " + F.probe_failure_reason(secondary_url, e)

    ok = secondary_to_primary and primary_to_secondary
    if ok:
        now = datetime.now(timezone.utc)
        db.db.federation_servers.update_one(
            {"server_id": req.server_id},
            {"$set": {"server_id": req.server_id, "url": secondary_url, "enrolled_at": now}},
            upsert=True,
        )
        # Add to the authoritative roster with its priority + address.
        db.db.cluster_nodes.update_one(
            {"node_id": req.server_id},
            {"$set": {"node_id": req.server_id, "priority": req.priority,
                      "address": secondary_url, "is_primary": False, "last_seen": now}},
            upsert=True,
        )
    return {
        "ok": ok,
        "server_id": req.server_id,
        "directions": {
            "secondary_to_primary": secondary_to_primary,
            "primary_to_secondary": primary_to_secondary,
        },
        "fleet_secret": get_or_create_fleet_secret(db) if ok else None,
        "reason": reason if not ok else None,
    }


class FleetSecretRequest(BaseModel):
    server_id: str


@router.post("/fleet-secret")
def fleet_secret_endpoint(req: FleetSecretRequest, x_join_token: Optional[str] = Header(None),
                          db: DBManager = Depends(get_db)):
    """Deliver the shared fleet secret to an ENROLLED secondary that presents its durable
    join token (same trust anchor as ingest). Convergence path for already-enrolled nodes —
    no re-enroll. Returned only over this gated channel; never logged."""
    if not _valid_token(db, req.server_id, x_join_token):
        raise HTTPException(status_code=401, detail="invalid or missing join token")
    return {"fleet_secret": get_or_create_fleet_secret(db)}
