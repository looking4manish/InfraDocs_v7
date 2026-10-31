"""Scan ingest: secondaries push their scans to the primary, which keeps one slice per server."""

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app import cluster_runtime as RT
from app.api.dependencies import get_db, verify_auth
from app.core.db_manager import DBManager
from app.fleet_auth import fleet_auth_ok

router = APIRouter()


class IngestRequest(BaseModel):
    server_id: str
    assets: List[dict] = []
    applications: List[dict] = []
    term: int = 0


@router.post("/ingest")
def ingest(req: IngestRequest, x_fleet_auth: Optional[str] = Header(None), db: DBManager = Depends(get_db)):
    if not fleet_auth_ok(db, x_fleet_auth):
        raise HTTPException(status_code=403, detail="fleet credential refused")
    n = RT.node()
    if n is None or not n.is_primary():
        raise HTTPException(status_code=409, detail="not the primary")
    if req.term > n.st.term:
        raise HTTPException(status_code=409, detail="stale primary")
    if n.st.config.member(req.server_id) is None:
        raise HTTPException(status_code=403, detail="not a member of this replica set")
    sid = req.server_id

    def _clean(docs):
        for d in docs:
            d.pop("_id", None)
            d["server_id"] = sid          # a member can only write its own slice
        return docs

    db.db.assets.delete_many({"server_id": sid})
    assets = _clean(req.assets)
    if assets:
        db.db.assets.insert_many(assets)
    db.db.applications.delete_many({"server_id": sid})
    apps = _clean(req.applications)
    if apps:
        db.db.applications.insert_many(apps)
    now = datetime.now(timezone.utc)
    db.db.federation_servers.update_one(
        {"server_id": sid},
        {"$set": {"server_id": sid, "last_seen": now, "last_synced_at": now,
                  "asset_count": len(assets), "app_count": len(apps)}},
        upsert=True,
    )
    return {"ok": True, "server_id": sid, "assets": len(assets), "applications": len(apps)}


@router.get("/servers")
def list_servers(_: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    return {"servers": list(db.db.federation_servers.find({}, {"_id": 0}))}
