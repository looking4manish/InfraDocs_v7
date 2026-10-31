"""Replica-set endpoints: member-to-member RPCs (heartbeat, vote, transfer, join) and the
operator actions behind the Servers page. Contract: docs/V7_API_CONTRACT.md."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app import cluster_runtime as RT
from app import election as E
from app.api.dependencies import get_config, get_db, verify_auth
from app.core.config_loader import Config
from app.core.db_manager import DBManager
from app.fleet_auth import fleet_auth_ok, get_fleet_secret

router = APIRouter()
log = logging.getLogger("app.cluster.api")
JOIN_CODE_TTL = timedelta(minutes=30)


def _node():
    n = RT.node()
    if n is None:
        raise HTTPException(status_code=409, detail="this server is not in a replica set yet")
    return n


def _fleet(db: DBManager, x_fleet_auth: Optional[str]) -> None:
    if not fleet_auth_ok(db, x_fleet_auth):
        raise HTTPException(status_code=403, detail="fleet credential refused")


def _iso(t):
    if t is None:
        return None
    if isinstance(t, datetime):
        return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()
    return t


def _guard(fn, *a, **kw):
    """Map engine refusals to HTTP errors with a few-word reason."""
    try:
        return fn(*a, **kw)
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _audit(db: DBManager, actor: str, action: str, detail: dict) -> None:
    db.db.actions_log.insert_one({"timestamp": datetime.now(timezone.utc), "actor": actor,
                                  "category": "cluster", "action": action, "asset_name": detail.get("target"),
                                  "status": "success", "detail": detail})


# ───────────── member-to-member RPCs ─────────────

@router.get("/rpc/ping")
def ping(cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    """Public identity probe used by setup and join to check an address really is this server."""
    n = RT.node()
    return {"node_id": cfg.server.id, "version": RT.VERSION, "in_cluster": n is not None,
            "role": n.st.role if n else None}


@router.post("/rpc/heartbeat")
def rpc_heartbeat(msg: dict, x_fleet_auth: Optional[str] = Header(None), db: DBManager = Depends(get_db)):
    _fleet(db, x_fleet_auth)
    return _node().on_heartbeat(msg)


@router.post("/rpc/vote")
def rpc_vote(req: dict, x_fleet_auth: Optional[str] = Header(None), db: DBManager = Depends(get_db)):
    _fleet(db, x_fleet_auth)
    return _node().on_vote(req)


@router.post("/rpc/timeout_now")
def rpc_timeout_now(req: dict, x_fleet_auth: Optional[str] = Header(None), db: DBManager = Depends(get_db)):
    _fleet(db, x_fleet_auth)
    return _node().on_timeout_now(req)


@router.post("/rpc/removed")
def rpc_removed(req: dict, x_fleet_auth: Optional[str] = Header(None), db: DBManager = Depends(get_db)):
    """The primary removed this server from the set. Stop the loop and forget the set."""
    _fleet(db, x_fleet_auth)
    n = _node()
    if int(req.get("term", 0)) < n.st.term:
        raise HTTPException(status_code=409, detail="stale term")
    RT.stop()
    db.db.election.delete_one({"_id": "state"})
    db.db.settings.update_one({"_id": "app"}, {"$set": {"cluster_removed_at": datetime.now(timezone.utc)}}, upsert=True)
    log.warning("cluster: removed from the replica set by %s", req.get("by"))
    return {"ok": True}


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class JoinRPC(BaseModel):
    code: str
    node_id: str
    address: str
    version: Optional[str] = None


@router.post("/rpc/join")
def rpc_join(req: JoinRPC, db: DBManager = Depends(get_db)):
    """A new server presents a join code. The primary checks the code, connects BACK to the
    new server's address (both directions must work), then adds it to the config."""
    from app.federation import probe_failure_reason, _get_json
    n = _node()
    rec = db.db.join_codes.find_one({"hash": _hash(req.code)})
    now = datetime.now(timezone.utc)
    if not rec or rec.get("state") not in ("waiting", "failed"):
        raise HTTPException(status_code=403, detail="join code not valid")
    exp = rec["expires_at"] if rec["expires_at"].tzinfo else rec["expires_at"].replace(tzinfo=timezone.utc)
    if exp < now:
        db.db.join_codes.update_one({"_id": rec["_id"]}, {"$set": {"state": "expired"}})
        raise HTTPException(status_code=403, detail="join code expired")
    if not n.is_primary():
        raise HTTPException(status_code=409, detail="this server is no longer primary")

    def fail(reason, back=None):
        db.db.join_codes.update_one({"_id": rec["_id"]}, {"$set": {
            "state": "failed", "node_id": req.node_id, "reason": reason,
            "directions": {"new_to_primary": True, "primary_to_new": back}}})
        raise HTTPException(status_code=400, detail=reason)

    if n.st.config.member(req.node_id):
        fail(f"a server named {req.node_id} is already a member")
    try:
        who = _get_json(req.address.rstrip("/") + "/api/cluster/rpc/ping", timeout=8)
    except Exception as e:  # noqa: BLE001
        fail("primary cannot reach the new server: " + probe_failure_reason(req.address, e), back=False)
    if who.get("node_id") != req.node_id:
        fail(f"{req.address} answers as {who.get('node_id')}, not {req.node_id}", back=False)

    member = E.Member(node_id=req.node_id, address=req.address.rstrip("/"),
                      votes=int(rec["votes"]), priority=int(rec["priority"]) if rec["votes"] else 0)
    try:
        cfg = n.change_config(lambda c: E.add_member(c, member))
    except (ValueError, PermissionError) as e:
        fail(str(e), back=True)
    db.db.join_codes.update_one({"_id": rec["_id"]}, {"$set": {
        "state": "joined", "node_id": req.node_id, "reason": None, "joined_at": now,
        "directions": {"new_to_primary": True, "primary_to_new": True}}})
    _audit(db, rec.get("created_by", "operator"), "member_added",
           {"target": req.node_id, "votes": member.votes, "priority": member.priority,
            "mode_after": E.failover_mode(cfg)[0]})
    users = [{k: u.get(k) for k in ("username", "password_hash", "must_change_password")}
             for u in db.db.users.find({}, {"_id": 0})]
    return {"ok": True, "fleet_secret": get_fleet_secret(db), "config": cfg.to_dict(),
            "term": n.st.term, "leader_id": n.node_id, "users": users}


# ───────────── read side ─────────────

@router.get("/state")
def state(_: str = Depends(verify_auth), cfg: Config = Depends(get_config), db: DBManager = Depends(get_db)):
    n = RT.node()
    if n is None:
        return {"node_id": cfg.server.id, "role": "standalone", "term": 0, "leader_id": None,
                "leader_address": None, "mode": E.STANDALONE, "mode_reason": "not in a replica set",
                "voting_members": 1, "quorum": 1, "reachable_voters": 1, "has_quorum": True,
                "primary_since": None, "settings": {}, "members": [], "elections": []}
    out = n.status()
    elections = RT.MongoStore(db).elections()
    out["elections"] = [{**e, "at": _iso(e.get("at"))} for e in elections]
    current = next((e for e in elections if e.get("term") == out["term"] and e.get("winner") == out["leader_id"]), None)
    out["primary_since"] = _iso(current.get("at")) if current else None
    return out


# ───────────── join codes ─────────────

class JoinCodeRequest(BaseModel):
    votes: int = 0
    priority: Optional[int] = None


def _public_base(request: Request, n) -> str:
    me = n.st.config.member(n.node_id)
    return (me.address if me and me.address else str(request.base_url)).rstrip("/")


@router.post("/join-code")
def join_code(req: JoinCodeRequest, request: Request, actor: str = Depends(verify_auth),
              db: DBManager = Depends(get_db)):
    n = _node()
    if not n.is_primary():
        raise HTTPException(status_code=409, detail="only the primary adds servers")
    if req.votes not in (0, 1):
        raise HTTPException(status_code=400, detail="votes must be 0 or 1")
    cfg = n.st.config
    priority = (req.priority or E.next_priority(cfg)) if req.votes else 0
    if req.votes and len(cfg.voters) >= E.MAX_VOTING_MEMBERS:
        raise HTTPException(status_code=409, detail=f"already {E.MAX_VOTING_MEMBERS} voting members")
    code = "ifd7_" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16].upper()
    now = datetime.now(timezone.utc)
    db.db.join_codes.insert_one({"hash": _hash(code), "votes": req.votes, "priority": priority,
                                 "created_at": now, "expires_at": now + JOIN_CODE_TTL,
                                 "created_by": actor, "state": "waiting"})
    voting_after = len(cfg.voters) + req.votes
    probe = E.Config(members=cfg.members + [E.Member("new", votes=req.votes, priority=priority)])
    mode_after, why_after = E.failover_mode(probe)
    base = _public_base(request, n)
    return {"code": code, "expires_at": (now + JOIN_CODE_TTL).isoformat(),
            "command": f"curl -fsSL {base}/join.sh | bash -s -- {code}",
            "voting_after": voting_after, "mode_after": mode_after, "mode_after_reason": why_after}


@router.get("/join-code/{code}/status")
def join_code_status(code: str, _: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    rec = db.db.join_codes.find_one({"hash": _hash(code)})
    if not rec:
        raise HTTPException(status_code=404, detail="unknown join code")
    exp = rec["expires_at"] if rec["expires_at"].tzinfo else rec["expires_at"].replace(tzinfo=timezone.utc)
    st = rec.get("state")
    if st == "waiting" and exp < datetime.now(timezone.utc):
        st = "expired"
    return {"state": st, "node_id": rec.get("node_id"), "reason": rec.get("reason"),
            "directions": rec.get("directions") or {"new_to_primary": None, "primary_to_new": None}}


# ───────────── operator actions ─────────────

@router.post("/step-down")
def step_down(actor: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    r = _guard(_node().step_down)
    _audit(db, actor, "step_down", {"target": r.get("to"), "ok": r.get("ok")})
    return r


class TransferRequest(BaseModel):
    to: str


@router.post("/transfer")
def transfer(req: TransferRequest, actor: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    r = _guard(_node().transfer, req.to, reason=f"handed over by {actor}")
    _audit(db, actor, "transfer", {"target": req.to, "ok": r.get("ok")})
    return r


class TakeoverRequest(BaseModel):
    confirm: str


@router.post("/takeover")
def takeover(req: TakeoverRequest, actor: str = Depends(verify_auth), cfg: Config = Depends(get_config),
             db: DBManager = Depends(get_db)):
    if req.confirm != cfg.server.id:
        raise HTTPException(status_code=400, detail=f"type {cfg.server.id} to confirm")
    r = _guard(_node().takeover, actor)
    _audit(db, actor, "takeover", {"target": cfg.server.id, "term": r.get("term")})
    return r


class MemberPatch(BaseModel):
    priority: Optional[int] = None
    votes: Optional[int] = None


@router.post("/members/{node_id}")
def update_member(node_id: str, req: MemberPatch, actor: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    n = _node()
    cfg = _guard(n.change_config, lambda c: E.update_member(c, node_id, priority=req.priority, votes=req.votes))
    _audit(db, actor, "member_updated", {"target": node_id, "priority": req.priority, "votes": req.votes})
    return {"ok": True, "config_version": cfg.version, "mode": E.failover_mode(cfg)[0]}


@router.delete("/members/{node_id}")
def remove_member(node_id: str, confirm: str = "", actor: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    if confirm != node_id:
        raise HTTPException(status_code=400, detail=f"type {node_id} to confirm")
    n = _node()
    gone = n.st.config.member(node_id)
    cfg = _guard(n.change_config, lambda c: E.remove_member(c, node_id))
    if gone:
        try:
            n.transport.send(gone, "removed", {"term": n.st.term, "by": n.node_id}, timeout=4)
        except Exception:  # noqa: BLE001 — an unreachable server is removed all the same
            pass
    db.db.assets.delete_many({"server_id": node_id})
    db.db.applications.delete_many({"server_id": node_id})
    _audit(db, actor, "member_removed", {"target": node_id, "mode_after": E.failover_mode(cfg)[0]})
    return {"ok": True, "config_version": cfg.version, "mode": E.failover_mode(cfg)[0]}


# ───────────── the join script ─────────────

JOIN_SH = r"""#!/usr/bin/env bash
# InfraDocs — join this server to an existing replica set.
#   curl -fsSL {base}/join.sh | bash -s -- <join-code> [--name <server-name>]
set -euo pipefail
CODE="${1:?join code missing — copy the full command from the Servers page}"; shift || true
PRIMARY="{base}"
REPO="${INFRADOCS_REPO:-{repo}}"
DIR="${INFRADOCS_DIR:-$HOME/infradocs}"
command -v git >/dev/null || { echo "git is required (sudo apt install -y git)"; exit 1; }
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only --quiet || true
else git clone --depth 1 "$REPO" "$DIR"; fi
exec bash "$DIR/deploy/docker/deploy.sh" --join "$CODE" --primary "$PRIMARY" "$@"
"""


def join_script(base: str, repo: str) -> str:
    return JOIN_SH.replace("{base}", base).replace("{repo}", repo)


def join_sh_response(request: Request) -> PlainTextResponse:
    import os
    n = RT.node()
    base = _public_base(request, n) if n else str(request.base_url).rstrip("/")
    repo = os.environ.get("INFRADOCS_REPO_URL", "https://github.com/looking4manish/InfraDocs_v7.git")
    return PlainTextResponse(join_script(base, repo), media_type="text/x-shellscript")
