"""Fleet node-switcher — view/act on ANY roster node from the leader's single URL.

Live-proxy with stale-fallback: a reachable node is proxied in real time and its actions
run live; an unreachable node falls back to the last-synced snapshot it previously pushed
to the leader, LOUDLY flagged stale, with live actions refused (409). Reachability +
address come from the live gossip roster (app.cluster state / cluster_nodes), NOT a cache.
This is a view+proxy layer only — it does not touch election/gossip/majority-guard/override.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response

from app import cluster as CC
from app.api.dependencies import get_config, get_db, verify_auth
from app.fleet_auth import get_fleet_secret
from app.core.config_loader import Config
from app.core.db_manager import DBManager

router = APIRouter()


# Injectable HTTP forwarder (tests patch this) — forwards a request to a node's own API.
def _http_forward(method: str, url: str, headers: dict, body: Optional[bytes], timeout: int = 10):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return getattr(r, "status", 200), r.headers.get("Content-Type", "application/json"), r.read()


def _fleet_nodes(db: DBManager, cfg: Config):
    """Every roster node (self + non-tombstoned cluster_nodes) with address, live
    reachability, leader flag, and last_synced_at — from the live gossip roster."""
    now = datetime.now(timezone.utc)
    timeout = cfg.federation.unreachable_after_seconds
    node_id = cfg.server.id
    s = db.db.cluster.find_one({"_id": "self"}) or {}
    tomb = {t["node_id"] for t in db.db.cluster_tombstones.find({}, {"_id": 0, "node_id": 1})}
    synced = {x["server_id"]: x for x in db.db.federation_servers.find({}, {"_id": 0})}
    rows = [{
        "node_id": node_id, "address": s.get("address"), "is_primary": bool(s.get("is_primary")),
        "self": True, "reachable": True, "last_synced_at": None,
    }]
    for n in db.db.cluster_nodes.find({}, {"_id": 0}):
        nid = n["node_id"]
        if nid == node_id or nid in tomb:
            continue
        fs = synced.get(nid, {})
        rows.append({
            "node_id": nid, "address": n.get("address"), "is_primary": bool(n.get("is_primary")),
            "self": False, "reachable": CC._age(now, n.get("last_seen")) <= timeout,
            "last_synced_at": fs.get("last_synced_at") or fs.get("last_seen"),
        })
    leader = next((r["node_id"] for r in rows if r["is_primary"] and r["reachable"]), node_id)
    for r in rows:
        r["is_leader"] = r["node_id"] == leader
        r["stale"] = not r["reachable"]
    return rows, leader


@router.get("/nodes")
def nodes(_: str = Depends(verify_auth), cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """Switcher list: every node's live reachability + last_synced_at, so the operator sees
    fleet health before selecting. Reflects current gossip state, not a stale cache."""
    rows, leader = _fleet_nodes(db, cfg)
    return {"nodes": rows, "leader": leader, "count": len(rows)}


def _stale_get(db: DBManager, sid: str, path: str, last_synced_at):
    """Serve a node's last-synced snapshot from what the leader stored (assets + apps by
    server_id; ports/storage recomputed from those assets). Always flagged stale."""
    base = {"stale": True, "node_id": sid, "last_synced_at": last_synced_at}
    p = "/" + path.strip("/")

    def _apps():
        return list(db.db.applications.find({"server_id": sid}, {"_id": 0}))

    def _assets():
        return list(db.db.assets.find({"server_id": sid}, {"_id": 0}))

    if p.startswith("/api/applications/list"):
        a = _apps()
        return {**base, "count": len(a), "applications": a}
    if p.startswith("/api/applications/"):
        name = p.split("/api/applications/", 1)[1].split("/")[0]
        doc = db.db.applications.find_one({"server_id": sid, "name": name}, {"_id": 0}) or {}
        return {**base, **doc, "name": name}
    if p.startswith("/api/assets"):
        a = _assets()
        return {**base, "count": len(a), "assets": a}
    if p.startswith("/api/ports"):
        try:
            from app.ports_registry import build_ports_registry
            vp = [x.get("name") for x in _apps()] + ["System"]
            reg = build_ports_registry(_assets(), server_id=sid, valid_projects=vp)
            return {**base, "count": len(reg), "ports": reg}
        except Exception:  # noqa: BLE001 — snapshot is best-effort; never crash the stale view
            return {**base, "count": 0, "ports": []}
    if p.startswith("/api/storage"):
        try:
            from app.storage_registry import build_storage_registry
            vp = [x.get("name") for x in _apps()] + ["System"]
            reg = build_storage_registry(_assets(), server_id=sid, projects_root="/", valid_projects=vp)
            return {**base, "count": len(reg), "storage": reg}
        except Exception:  # noqa: BLE001
            return {**base, "count": 0, "storage": []}
    if p.startswith("/api/health"):
        return {**base, "status": "stale", "server": sid}
    return {**base, "data": None}


@router.api_route("/{node_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(node_id: str, path: str, request: Request,
                _: str = Depends(verify_auth), cfg: Config = Depends(get_config),
                db: DBManager = Depends(get_db)):
    """Reachable node → forward the request live and stream the node's own response back.
    Unreachable node → GET falls back to the stale snapshot; any write returns 409 (the
    action gate — a down node's action must never appear to succeed)."""
    rows, _leader = _fleet_nodes(db, cfg)
    node = next((r for r in rows if r["node_id"] == node_id), None)
    if node is None:
        return Response(json.dumps({"error": f"unknown node '{node_id}'"}),
                        status_code=404, media_type="application/json")

    auth_failed = False
    method = request.method
    if node["reachable"] and (node.get("address") or node["self"]):
        body = await request.body()
        addr = node.get("address") or f"http://localhost:{cfg.server.api_port}"
        qs = ("?" + request.url.query) if request.url.query else ""
        url = addr.rstrip("/") + "/" + path.lstrip("/") + qs
        # Authenticate to the peer with the SHARED FLEET SECRET, not the operator's browser
        # session (valid only on the serving node). This is what breaks the cross-node
        # 401 -> login-loop: the peer accepts X-Fleet-Auth as the 'fleet' principal.
        headers = {}
        _fs = get_fleet_secret(db)
        if _fs:
            headers["X-Fleet-Auth"] = _fs
        if request.headers.get("content-type"):
            headers["Content-Type"] = request.headers["content-type"]
        try:
            status, ctype, data = _http_forward(method, url, headers, body or None)
            r = Response(content=data, status_code=status, media_type=ctype)
            r.headers["X-Fleet-Node"] = node_id
            r.headers["X-Fleet-Live"] = "1"
            return r
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # The peer REJECTED the fleet credential. This is NOT the operator's session
                # expiring — never bounce them to login. Fall through to the stale view with a
                # clear reason so the frontend shows the FleetBanner, not a redirect loop.
                auth_failed = True
            else:
                return Response(content=e.read(), status_code=e.code, media_type="application/json",
                                headers={"X-Fleet-Node": node_id, "X-Fleet-Live": "1"})
        except Exception:  # noqa: BLE001 — live forward failed → fall through to stale
            pass

    # unreachable, OR the peer rejected our fleet credential (auth_failed)
    reason = ("peer authentication failed — the target node rejected the fleet credential"
              if auth_failed else f"{node_id} is unreachable")
    hdrs = {"X-Fleet-Node": node_id, "X-Fleet-Stale": "1"}
    if auth_failed:
        hdrs["X-Fleet-Auth-Failed"] = "1"
    if method != "GET":
        return Response(
            json.dumps({"ok": False, "stale": True, "node_id": node_id,
                        "detail": f"{reason} — actions unavailable",
                        "last_synced_at": str(node.get("last_synced_at"))}),
            status_code=409, media_type="application/json", headers=hdrs)
    payload = _stale_get(db, node_id, path, node.get("last_synced_at"))
    payload["reason"] = reason
    return Response(json.dumps(payload, default=str), status_code=200, media_type="application/json", headers=hdrs)
