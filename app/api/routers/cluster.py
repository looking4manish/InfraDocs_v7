"""Cluster coordination endpoints — gossip health, cluster state, override, and the
guarded manual promote (the restore path). No shared DB: each node persists its own
view in its own Mongo (`cluster` self-doc + `cluster_nodes` roster)."""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import cluster as CC
from app import cluster_manager as MGR
from app.api.dependencies import get_config, get_db, require_operator, verify_auth
from app.api.routers.setup import enroll_secondary  # shared enroll path (no drift)
from app.core.config_loader import Config
from app.core.db_manager import DBManager

router = APIRouter()
_log = logging.getLogger("app.cluster.admin")


def _self(db: DBManager) -> dict:
    return db.db.cluster.find_one({"_id": "self"}) or {}


def _roster(db: DBManager) -> list:
    return list(db.db.cluster_nodes.find({}, {"_id": 0}))


def _tombstones(db: DBManager) -> set:
    return {t["node_id"] for t in db.db.cluster_tombstones.find({}, {"_id": 0, "node_id": 1})}


def _set_self(db: DBManager, patch: dict) -> None:
    db.db.cluster.update_one({"_id": "self"}, {"$set": patch}, upsert=True)


def _role(db: DBManager) -> str:
    """Current role: explicit settings.role if set, else inferred from the self-doc."""
    settings = db.db.settings.find_one({"_id": "app"}) or {}
    if settings.get("role"):
        return settings["role"]
    s = _self(db)
    if s.get("is_primary"):
        return "primary"
    return "secondary" if _roster(db) else "standalone"


def _set_role(db: DBManager, role: str) -> None:
    db.db.settings.update_one({"_id": "app"}, {"$set": {"role": role}}, upsert=True)


def _known_priorities(db: DBManager, node_id: str) -> dict:
    """node_id -> priority across roster + self (for duplicate-priority rejection)."""
    out = {}
    s = _self(db)
    if s.get("priority") is not None:
        out[node_id] = s["priority"]
    for n in _roster(db):
        if n.get("priority") is not None:
            out[n["node_id"]] = n["priority"]
    return out


def _audit(db: DBManager, actor: str, action: str, from_role: Optional[str],
           to_role: Optional[str], node_id: str, result: str, reason: Optional[str] = None) -> None:
    """Append a timestamped, reasoned entry to the persisted cluster audit log AND emit a
    loud timestamped log line (matching the project's logging style)."""
    entry = {
        "ts": datetime.now(timezone.utc),
        "actor": actor, "action": action,
        "from_role": from_role, "to_role": to_role,
        "node_id": node_id, "result": result, "reason": reason,
    }
    try:
        db.db.cluster_audit.insert_one(dict(entry))
    except Exception:  # noqa: BLE001 — never let audit IO break a transition's response
        pass
    _log.info("CLUSTER-AUDIT actor=%s action=%s %s->%s node=%s result=%s reason=%s",
              actor, action, from_role, to_role, node_id, result, reason or "-")


def _nodes_snapshot(db: DBManager, node_id: str, now: datetime) -> list:
    """self + non-tombstoned roster, in the shape app.cluster expects."""
    tomb = _tombstones(db)
    s = _self(db)
    nodes = [{"node_id": node_id, "priority": s.get("priority"), "last_seen": now,
              "is_primary": bool(s.get("is_primary")), "self": True}]
    for n in _roster(db):
        if n["node_id"] == node_id or n["node_id"] in tomb:
            continue
        nodes.append({"node_id": n["node_id"], "priority": n.get("priority"),
                      "last_seen": n.get("last_seen"), "is_primary": bool(n.get("is_primary")),
                      "self": False})
    return nodes


@router.get("/health")
def health(cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """The gossip message this node emits when a peer health-checks it: its own id +
    priority + last-scan time + whether it serves as primary + override, plus the peers
    it knows (so priorities propagate transitively in one round). Unauthenticated."""
    s = _self(db)
    last_scan = db.db.scan_logs.find_one({}, sort=[("created_at", -1)])
    return {
        "node_id": cfg.server.id,
        "priority": s.get("priority"),
        "address": s.get("address"),
        "is_primary": bool(s.get("is_primary")),
        "override": bool(s.get("override")),
        "last_scan_ts": (last_scan or {}).get("created_at"),
        "peers": [
            {"node_id": n["node_id"], "priority": n.get("priority"), "address": n.get("address")}
            for n in _roster(db)
        ],
    }


@router.get("/state")
def state(_: str = Depends(verify_auth), cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """Full cluster view for the Servers lens: this node, the current leader, every
    known node with priority + reachability, fleet size, and whether we hold a majority."""
    s = _self(db)
    now = datetime.now(timezone.utc)
    timeout = cfg.federation.unreachable_after_seconds
    node_id = cfg.server.id

    nodes = [{
        "node_id": node_id, "priority": s.get("priority"), "address": s.get("address"),
        "is_primary": bool(s.get("is_primary")), "reachable": True, "self": True,
        "last_seen": now, "last_scan_ts": s.get("last_scan_ts"),
    }]
    tomb = _tombstones(db)
    for n in _roster(db):
        if n["node_id"] == node_id or n["node_id"] in tomb:
            continue
        age = CC._age(now, n.get("last_seen"))
        nodes.append({
            "node_id": n["node_id"], "priority": n.get("priority"), "address": n.get("address"),
            "is_primary": bool(n.get("is_primary")), "reachable": age <= timeout, "self": False,
            "last_seen": n.get("last_seen"), "last_scan_ts": n.get("last_scan_ts"),
        })

    reachable = [n for n in nodes if n["reachable"]]
    leader = next((n["node_id"] for n in reachable if n["is_primary"]), None)
    override = bool(s.get("override"))
    role = _role(db)
    roster = [n for n in _roster(db) if n["node_id"] not in tomb and n["node_id"] != node_id]

    # Per-transition guard reasons so the UI can DISABLE a control AND say why (never a
    # silent grey-out). None => allowed.
    snap = _nodes_snapshot(db, node_id, now)
    demote_ok, demote_reason = CC.can_demote_primary(snap, node_id, now, override=override, timeout=timeout)
    stand_ok, stand_deps, stand_reason = CC.can_go_standalone(roster)
    guards = {
        "demote_blocked_reason": None if demote_ok else demote_reason,
        "to_standalone_dependents": stand_deps,
        "to_standalone_blocked_reason": None if stand_ok else stand_reason,
        "override_pinned": override,
    }
    return {
        "node_id": node_id,
        "priority": s.get("priority"),
        "role": role,
        "is_primary": bool(s.get("is_primary")),
        "override": override,
        "cluster_enabled": MGR.is_enabled(cfg, db),
        "auto_failover": MGR.auto_failover_enabled(cfg, db),
        "current_leader": leader,
        "majority": CC.has_majority(len(reachable), len(nodes)),
        "fleet_size": CC.fleet_size([n["node_id"] for n in roster], tomb),
        "nodes": nodes,
        "guards": guards,
    }


class OverrideRequest(BaseModel):
    value: bool


@router.post("/override")
def override(req: OverrideRequest, actor: str = Depends(require_operator), db: DBManager = Depends(get_db)):
    """Pin/unpin the current primary. While set, no node initiates an election (it's
    broadcast in /health and remembered by peers). The manual escape hatch."""
    _set_self(db, {"override": bool(req.value)})
    r = _role(db)
    _audit(db, actor, "override_set" if req.value else "override_clear",
           r, r, _self(db).get("node_id"), "ok", f"override={'on' if req.value else 'off'}")
    return {"ok": True, "override": bool(req.value)}


class PromoteRequest(BaseModel):
    force: bool = False


@router.post("/promote")
def promote(
    req: PromoteRequest,
    actor: str = Depends(require_operator),
    cfg: Config = Depends(get_config),
    db: DBManager = Depends(get_db),
):
    """Manually promote THIS node to primary (the failover-restore path). Guarded:
      - refuse if a LIVE primary is visible (a reachable peer serving as primary);
      - if some nodes are UNREACHABLE, do NOT silently promote — return needs_force
        with the two-primaries warning;
      - force=true (explicit confirm) promotes despite unreachable nodes, but NEVER
        against a confirmed-live primary."""
    node_id = cfg.server.id
    now = datetime.now(timezone.utc)
    timeout = cfg.federation.unreachable_after_seconds

    live_primary, unreachable = None, []
    for n in _roster(db):
        if n["node_id"] == node_id:
            continue
        reachable = CC._age(now, n.get("last_seen")) <= timeout
        if reachable and n.get("is_primary"):
            live_primary = n["node_id"]
        elif not reachable:
            unreachable.append(n["node_id"])

    if live_primary:
        _audit(db, actor, "promote", "secondary", "primary", node_id, "refused",
               f"live primary visible: {live_primary}")
        return {"promoted": False, "leader": live_primary,
                "reason": f"a live primary is visible: {live_primary} — refuse (use override/failover instead)"}

    if unreachable and not req.force:
        _audit(db, actor, "promote", "secondary", "primary", node_id, "needs_force",
               f"unreachable nodes: {unreachable}")
        return {
            "promoted": False, "needs_force": True, "unreachable": unreachable,
            "warning": ("the current primary can't be confirmed down — these nodes are "
                        f"unreachable: {unreachable}. Forcing may create two primaries. "
                        "Re-submit with force=true only if you are certain the old primary is down."),
        }

    # Promote: this node becomes primary; clear any stale primary flags in our roster.
    _set_self(db, {"is_primary": True, "node_id": node_id, "promoted_at": now,
                   "promoted_by": actor, "forced": bool(req.force)})
    db.db.cluster_nodes.update_many({}, {"$set": {"is_primary": False}})
    db.db.settings.update_one({"_id": "app"}, {"$set": {"primary_node": node_id}}, upsert=True)
    _set_role(db, "primary")
    _audit(db, actor, "promote", "secondary", "primary", node_id,
           "ok", "forced despite unreachable nodes" if (req.force and unreachable) else "promoted")
    return {"promoted": True, "forced": bool(req.force), "leader": node_id,
            "warning": ("force-promoted despite unreachable nodes — verify no second primary is running."
                        if (req.force and unreachable) else None)}


# ===========================================================================
# Admin / Cluster tab — role-transition matrix, enable toggle, evict, audit.
# Every transition is guarded per its real hazard, audited, and (where it changes
# leadership) updates the cluster self-doc so the redirect gate follows in the same
# transaction. Nothing here is reachable unauthenticated (verify_auth on all).
# ===========================================================================


def _gossip_logger(request: Request):
    return getattr(request.app.state, "cluster_logger", _log)


class EnableRequest(BaseModel):
    value: bool


@router.post("/enable")
async def set_enabled(req: EnableRequest, request: Request, actor: str = Depends(require_operator),
                cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """cluster_enabled switch. Persists a runtime override and starts/stops the gossip
    loop LIVE (no container restart). Disabling quiesces cleanly — role/roster are kept."""
    _set_self(db, {"cluster_enabled": bool(req.value)})
    logger = _gossip_logger(request)
    if req.value:
        MGR.start_gossip(request.app, cfg, db, logger)
    else:
        MGR.stop_gossip(request.app, logger)
    _audit(db, actor, "cluster_enable" if req.value else "cluster_disable",
           _role(db), _role(db), cfg.server.id, "ok", f"cluster_enabled={req.value}")
    return {"ok": True, "cluster_enabled": bool(req.value)}


class AutoFailoverRequest(BaseModel):
    value: bool


@router.post("/auto-failover")
def set_auto_failover(req: AutoFailoverRequest, actor: str = Depends(require_operator),
                      cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """Toggle AUTOMATIC failover. Persists a runtime override the live gossip loop re-reads
    every round (no restart). When OFF, a lost primary does NOT auto-promote a survivor —
    gossip still runs and the split-brain guard is unaffected, but taking over is a manual
    action (Promote / Make primary). When ON, the majority-partition survivor auto-elects."""
    _set_self(db, {"auto_failover": bool(req.value)})
    _audit(db, actor, "auto_failover_enable" if req.value else "auto_failover_disable",
           _role(db), _role(db), cfg.server.id, "ok", f"auto_failover={req.value}")
    return {"ok": True, "auto_failover": bool(req.value)}


class ToPrimaryRequest(BaseModel):
    priority: int = 1


@router.post("/to-primary")
async def to_primary(req: ToPrimaryRequest, request: Request, actor: str = Depends(require_operator),
               cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """Standalone -> Primary. This is a CONFIG FLIP (the cluster code always ships; only
    the gossip loop is gated): enable the cluster, take a free priority, serve as primary,
    start gossip."""
    node_id = cfg.server.id
    role = _role(db)
    if role != "standalone":
        raise HTTPException(status_code=409, detail=f"node is '{role}', not standalone")
    if not (1 <= req.priority <= 99):
        raise HTTPException(status_code=400, detail="priority must be between 1 and 99")
    known = _known_priorities(db, node_id)
    if CC.priority_in_use({nid: {"priority": p} for nid, p in known.items()}, req.priority, exclude=node_id):
        taken = next(nid for nid, p in known.items() if p == req.priority and nid != node_id)
        _audit(db, actor, "to_primary", "standalone", "primary", node_id, "refused",
               f"priority {req.priority} in use by {taken}")
        raise HTTPException(status_code=409, detail=f"priority {req.priority} already in use (by '{taken}')")
    _set_self(db, {"node_id": node_id, "priority": req.priority, "is_primary": True,
                   "override": False, "cluster_enabled": True})
    db.db.settings.update_one({"_id": "app"}, {"$set": {"primary_node": node_id}}, upsert=True)
    _set_role(db, "primary")
    MGR.start_gossip(request.app, cfg, db, _gossip_logger(request))
    _audit(db, actor, "to_primary", "standalone", "primary", node_id, "ok",
           f"priority={req.priority}, cluster enabled")
    return {"ok": True, "role": "primary", "priority": req.priority, "cluster_enabled": True}


class JoinRequest(BaseModel):
    primary_url: str
    join_token: str
    advertise_url: str
    priority: int
    force: bool = False                       # confirm orphaning our own secondaries
    confirm_hostname: Optional[str] = ""      # must equal this node's id when force-orphaning


@router.post("/join")
async def join(req: JoinRequest, request: Request, actor: str = Depends(require_operator),
         cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """Standalone OR Primary -> Secondary. Reuses the installer's canonical enroll path
    verbatim (setup.enroll_secondary -> federation.enroll_with_primary -> primary /enroll),
    so it can never drift from cli_install.py and it receives+stores the shared fleet secret.
    A PRIMARY joining LEAVES its own cluster: refuse while override is pinned, and orphan-guard
    (type-id-to-confirm) if it still has enrolled secondaries. This node's role changes ONLY
    after enrol succeeds, so a bad token / unreachable / priority clash leaves it in its prior role."""
    node_id = cfg.server.id
    role = _role(db)
    s = _self(db)
    if role == "secondary":
        raise HTTPException(status_code=409, detail="this node is already a secondary")
    if not (1 <= req.priority <= 99):
        raise HTTPException(status_code=400, detail="priority must be between 1 and 99")
    # override/pin guard — a pinned primary must be un-pinned first
    if bool(s.get("override")):
        _audit(db, actor, "join", role, "secondary", node_id, "refused", "override pinned")
        raise HTTPException(status_code=409, detail={"message": "blocked",
            "reason": "override/pin is set on this node — clear it before joining another primary"})
    # orphan guard — a primary with its own enrolled secondaries would orphan them (same hazard
    # as Primary->Standalone). Lone primary / standalone: no dependents, proceed after enrol.
    dependents = []
    if role == "primary":
        tomb = _tombstones(db)
        roster = [n for n in _roster(db) if n["node_id"] not in tomb and n["node_id"] != node_id]
        okdep, dependents, reason = CC.can_go_standalone(roster)
        if not okdep and not req.force:
            _audit(db, actor, "join", role, "secondary", node_id, "needs_force", reason)
            return {"ok": False, "needs_force": True, "dependents": dependents, "reason": reason,
                    "confirm": f"type this node's id ('{node_id}') to confirm orphaning "
                               f"{len(dependents)} node(s) and joining {req.primary_url}"}
        if not okdep and req.force and req.confirm_hostname != node_id:
            raise HTTPException(status_code=400,
                                detail=f"confirm_hostname must equal '{node_id}' to force-orphan {len(dependents)} node(s)")
    # SHARED enrol path: bidirectional reachability + fleet-secret delivery. Nothing about this
    # node's role changes until it returns ok, so a failure never half-transitions/wedges it.
    result = enroll_secondary(db, node_id, req.primary_url, req.join_token, req.advertise_url, req.priority)
    if not result.get("ok"):
        _audit(db, actor, "join", role, "secondary", node_id, "refused", result.get("reason"))
        raise HTTPException(status_code=400, detail={"message": "enrollment refused",
                            "directions": result.get("directions", {}), "reason": result.get("reason")})
    if role == "primary":
        db.db.cluster_nodes.delete_many({})  # drop our OWN old roster; the new primary gossips the real one
    _set_self(db, {"cluster_enabled": True})
    _set_role(db, "secondary")
    db.db.settings.update_one({"_id": "app"}, {"$set": {"primary_node": None}}, upsert=True)  # redirect gate follows the new leader
    MGR.start_gossip(request.app, cfg, db, _gossip_logger(request))
    _audit(db, actor, "join", role, "secondary", node_id, "ok",
           f"joined {req.primary_url} as secondary at priority {req.priority}"
           + (f", orphaned {len(dependents)} node(s)" if dependents else ""))
    return {"ok": True, "role": "secondary", "from_role": role, "primary_url": req.primary_url,
            "orphaned": dependents, "directions": result.get("directions")}


@router.post("/demote")
def demote(actor: str = Depends(require_operator), cfg: Config = Depends(get_config),
           db: DBManager = Depends(get_db)):
    """Primary -> Secondary. Refuse if override is pinned, or if no alternative primary is
    reachable (would leave the cluster leaderless)."""
    node_id = cfg.server.id
    now = datetime.now(timezone.utc)
    s = _self(db)
    if not s.get("is_primary"):
        raise HTTPException(status_code=409, detail="node is not the primary")
    ok, reason = CC.can_demote_primary(_nodes_snapshot(db, node_id, now), node_id, now,
                                       override=bool(s.get("override")),
                                       timeout=cfg.federation.unreachable_after_seconds)
    if not ok:
        _audit(db, actor, "demote", "primary", "secondary", node_id, "refused", reason)
        return {"ok": False, "blocked": True, "reason": reason}
    _set_self(db, {"is_primary": False})
    _set_role(db, "secondary")
    db.db.settings.update_one({"_id": "app"}, {"$set": {"primary_node": None}}, upsert=True)
    _audit(db, actor, "demote", "primary", "secondary", node_id, "ok", "stepped down")
    return {"ok": True, "role": "secondary"}


class HandoffRequest(BaseModel):
    target: str


@router.post("/handoff")
def handoff(req: HandoffRequest, actor: str = Depends(require_operator),
            cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """Planned PRIMARY switchover to a healthy peer — the thing promote/demote can't do
    between two live nodes (each guard blocks the other). Runs on the CURRENT primary: step
    down first, then instruct the target to assume primary over the shared-fleet-secret
    channel. If the target can't be reached, roll back (reclaim primary) so the cluster is
    never left leaderless. Auto-failover being off means the brief single-leader gap can't
    kick off an election. This is what the Admin fleet console's per-node 'Make primary' uses."""
    node_id = cfg.server.id
    now = datetime.now(timezone.utc)
    s = _self(db)
    if not s.get("is_primary"):
        raise HTTPException(status_code=409, detail="only the current primary can hand off the role")
    if req.target == node_id:
        return {"ok": True, "leader": node_id, "note": "already primary"}
    tomb = _tombstones(db)
    target = next((n for n in _roster(db) if n["node_id"] == req.target and n["node_id"] not in tomb), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"'{req.target}' is not a known node")
    addr = target.get("address")
    reachable = bool(addr) and CC._age(now, target.get("last_seen")) <= cfg.federation.unreachable_after_seconds
    if not reachable:
        raise HTTPException(status_code=409, detail=f"target '{req.target}' is not reachable — cannot hand off")
    # 1) step down FIRST — a momentary no-leader is safer than two primaries, and with
    #    auto-failover off no node will opportunistically self-promote in the gap.
    _set_self(db, {"is_primary": False})
    _set_role(db, "secondary")
    db.db.settings.update_one({"_id": "app"}, {"$set": {"primary_node": req.target}}, upsert=True)
    # 2) tell the target to assume primary, authenticated with the shared fleet secret.
    from app.fleet_auth import get_or_create_fleet_secret
    from app import federation as F
    try:
        secret = get_or_create_fleet_secret(db)
        F._post_json(addr.rstrip("/") + "/api/cluster/assume-primary", {"from": node_id},
                     headers={"X-Fleet-Auth": secret}, timeout=15)
    except Exception as e:  # noqa: BLE001 — roll back so the cluster is never left leaderless
        _set_self(db, {"is_primary": True})
        _set_role(db, "primary")
        db.db.settings.update_one({"_id": "app"}, {"$set": {"primary_node": node_id}}, upsert=True)
        _audit(db, actor, "handoff", "primary", "primary", node_id, "failed",
               f"target {req.target} did not assume ({type(e).__name__}); rolled back — kept primary")
        raise HTTPException(status_code=502,
                            detail=f"handoff to '{req.target}' failed ({type(e).__name__}) — kept primary")
    # 3) reflect the new leader locally; gossip confirms it fleet-wide within a round.
    db.db.cluster_nodes.update_many({}, {"$set": {"is_primary": False}})
    db.db.cluster_nodes.update_one({"node_id": req.target}, {"$set": {"is_primary": True}})
    _audit(db, actor, "handoff", "primary", "secondary", node_id, "ok", f"handed primary to {req.target}")
    return {"ok": True, "leader": req.target, "role": "secondary"}


@router.post("/assume-primary")
def assume_primary(actor: str = Depends(verify_auth), cfg: Config = Depends(get_config),
                   db: DBManager = Depends(get_db)):
    """Internal handoff target: called by the outgoing primary over the fleet-authed channel to
    take leadership. Fleet-principal ONLY (never a browser session) so it can't be used to mint
    a second primary out of band — a direct promotion has its own guarded /promote path."""
    if actor != "fleet":
        raise HTTPException(status_code=403,
                            detail="assume-primary is an internal handoff endpoint (fleet-authenticated only)")
    node_id = cfg.server.id
    now = datetime.now(timezone.utc)
    _set_self(db, {"is_primary": True, "node_id": node_id, "promoted_at": now, "promoted_by": "handoff"})
    db.db.cluster_nodes.update_many({}, {"$set": {"is_primary": False}})
    db.db.settings.update_one({"_id": "app"}, {"$set": {"primary_node": node_id}}, upsert=True)
    _set_role(db, "primary")
    _audit(db, "handoff", "assume_primary", "secondary", "primary", node_id, "ok", "assumed primary via handoff")
    return {"ok": True, "role": "primary", "leader": node_id}


class ToStandaloneRequest(BaseModel):
    force: bool = False
    confirm_hostname: Optional[str] = None


@router.post("/to-standalone")
async def to_standalone(req: ToStandaloneRequest, request: Request, actor: str = Depends(require_operator),
                  cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """Primary -> Standalone. Refuse (unless forced) while secondaries are still enrolled
    and redirecting here — otherwise they are orphaned. Forcing requires type-the-hostname
    confirmation. On confirm: notify peers by dropping the roster, disable the cluster, keep
    serving directly."""
    node_id = cfg.server.id
    tomb = _tombstones(db)
    roster = [n for n in _roster(db) if n["node_id"] not in tomb and n["node_id"] != node_id]
    ok, dependents, reason = CC.can_go_standalone(roster)
    if not ok and not req.force:
        _audit(db, actor, "to_standalone", "primary", "standalone", node_id, "needs_force", reason)
        return {"ok": False, "needs_force": True, "dependents": dependents, "reason": reason,
                "confirm": f"type this node's id ('{node_id}') to confirm orphaning {len(dependents)} node(s)"}
    if not ok and req.force and req.confirm_hostname != node_id:
        raise HTTPException(status_code=400,
                            detail=f"confirm_hostname must equal '{node_id}' to force-orphan {len(dependents)} node(s)")
    # Confirmed: quiesce cluster, drop roster (peers stop being tracked / redirected to),
    # keep serving directly as a single node.
    _set_self(db, {"is_primary": True, "override": False, "cluster_enabled": False})
    db.db.cluster_nodes.delete_many({})
    db.db.settings.update_one({"_id": "app"}, {"$set": {"primary_node": node_id}}, upsert=True)
    _set_role(db, "standalone")
    MGR.stop_gossip(request.app, _gossip_logger(request))
    _audit(db, actor, "to_standalone", "primary", "standalone", node_id, "ok",
           f"orphaned {len(dependents)} node(s), cluster disabled" if dependents else "no dependents")
    return {"ok": True, "role": "standalone", "orphaned": dependents}


class PriorityRequest(BaseModel):
    priority: int


@router.post("/priority")
def set_priority(req: PriorityRequest, actor: str = Depends(require_operator),
                 cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """Reassign this node's failover priority (1-99). Rejects a number already in use by a
    known node — same rule the installer/enroll enforce."""
    node_id = cfg.server.id
    if not (1 <= req.priority <= 99):
        raise HTTPException(status_code=400, detail="priority must be between 1 and 99")
    known = _known_priorities(db, node_id)
    if CC.priority_in_use({nid: {"priority": p} for nid, p in known.items()}, req.priority, exclude=node_id):
        taken = next(nid for nid, p in known.items() if p == req.priority and nid != node_id)
        _audit(db, actor, "priority", _role(db), _role(db), node_id, "refused",
               f"priority {req.priority} in use by {taken}")
        raise HTTPException(status_code=409, detail=f"priority {req.priority} already in use (by '{taken}')")
    _set_self(db, {"priority": req.priority})
    _audit(db, actor, "priority", _role(db), _role(db), node_id, "ok", f"priority={req.priority}")
    return {"ok": True, "priority": req.priority}


class EvictRequest(BaseModel):
    node_id: str
    confirm_hostname: Optional[str] = None


@router.post("/evict")
def evict(req: EvictRequest, actor: str = Depends(require_operator), cfg: Config = Depends(get_config),
          db: DBManager = Depends(get_db)):
    """De-enroll a node from the roster. Quorum-critical: a ghost node still counts toward
    the majority-guard denominator, so eviction deletes it AND tombstones it (so gossip
    can't re-learn it), shrinking the known-fleet size the majority math uses. Type-the-id
    confirmation required (destructive with live dependents)."""
    target = req.node_id
    if target == cfg.server.id:
        raise HTTPException(status_code=400, detail="a node cannot evict itself")
    if not db.db.cluster_nodes.find_one({"node_id": target}) and target not in _tombstones(db):
        raise HTTPException(status_code=404, detail=f"'{target}' is not in the roster")
    if req.confirm_hostname != target:
        raise HTTPException(status_code=400, detail=f"confirm_hostname must equal '{target}' to evict it")
    db.db.cluster_nodes.delete_many({"node_id": target})
    db.db.federation_servers.delete_many({"server_id": target})
    db.db.cluster_tombstones.update_one(
        {"node_id": target},
        {"$set": {"node_id": target, "evicted_at": datetime.now(timezone.utc), "evicted_by": actor}},
        upsert=True,
    )
    tomb = _tombstones(db)
    roster_ids = [n["node_id"] for n in _roster(db) if n["node_id"] not in tomb and n["node_id"] != cfg.server.id]
    new_fleet = CC.fleet_size(roster_ids, tomb)
    _audit(db, actor, "evict", _role(db), _role(db), target, "ok", f"fleet_size now {new_fleet}")
    return {"ok": True, "evicted": target, "fleet_size": new_fleet}


@router.get("/audit")
def audit_log(limit: int = 100, _: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    """The persisted cluster audit trail, newest first, for display in the Admin tab."""
    rows = list(db.db.cluster_audit.find({}, {"_id": 0}).sort("ts", -1).limit(min(limit, 500)))
    return {"entries": rows, "count": len(rows)}
