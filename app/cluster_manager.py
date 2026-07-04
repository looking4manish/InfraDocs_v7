"""Gossip + failover loop. Drives the pure logic in app.cluster against this node's
own Mongo (no shared DB). Gated by federation.cluster_enabled, so it is inert until a
real fleet turns it on.

Each round (every health_interval): pull /api/cluster/health from every known peer,
fold the replies into the local roster (self-heal), then run evaluate_cluster and apply
the decision (become/relinquish primary, follow the leader). The election grace — one
full round after primary-loss is first seen — is tracked here in `_election`.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app import cluster as CC
from app import federation as F

logger = logging.getLogger("app.cluster")

# Per-process election timing state (not persisted — it's only a debounce).
_election = {"primary_lost_since": None}


def _peer_health(address: str, timeout: int = 6) -> dict:
    return F._get_json(address.rstrip("/") + "/api/cluster/health", timeout=timeout)


def run_round(db, cfg, now=None, ping=_peer_health, auto_failover=True) -> dict:
    """One gossip+evaluate+apply cycle. Returns the decision (for logging/tests).

    `auto_failover`: when False, elections still run (roster self-heal, follow the live
    primary, and a partitioned primary still steps down — the split-brain guard is NOT
    weakened), but a `promote_self` decision is NOT applied: this node stays secondary and
    logs that a MANUAL promotion is required. Taking over primary becomes an operator action.
    """
    now = now or datetime.now(timezone.utc)
    node_id = cfg.server.id
    timeout = cfg.federation.unreachable_after_seconds
    interval = cfg.federation.health_interval_seconds

    s = db.db.cluster.find_one({"_id": "self"}) or {}
    tombstones = {t["node_id"] for t in db.db.cluster_tombstones.find({}, {"_id": 0, "node_id": 1})}
    roster = {n["node_id"]: n for n in db.db.cluster_nodes.find({}, {"_id": 0})
              if n["node_id"] not in tombstones}

    # --- gossip: pull every known peer's health, self-heal the roster ---------
    override_seen = False
    for nid, rec in list(roster.items()):
        addr = rec.get("address")
        if not addr or nid == node_id:
            continue
        try:
            msg = ping(addr)
            CC.merge_gossip(roster, msg, now, tombstones=tombstones)  # evicted ids never return
            if msg.get("is_primary") and msg.get("override"):
                override_seen = True
        except Exception:  # noqa: BLE001 — peer unreachable; leave its last_seen stale
            pass
    # persist the refreshed roster (never re-persist a tombstoned/evicted node)
    for nid, rec in roster.items():
        if nid == node_id or nid in tombstones:
            continue
        db.db.cluster_nodes.update_one({"node_id": nid}, {"$set": rec}, upsert=True)

    # --- build the node list for evaluation (self always reachable) -----------
    nodes = [{"node_id": node_id, "priority": s.get("priority"), "last_seen": now,
              "is_primary": bool(s.get("is_primary")), "self": True}]
    for nid, rec in roster.items():
        if nid == node_id:
            continue
        nodes.append({"node_id": nid, "priority": rec.get("priority"),
                      "last_seen": rec.get("last_seen"), "is_primary": bool(rec.get("is_primary")),
                      "self": False})

    override = bool(s.get("override")) or override_seen

    # --- election grace: wait one full round after primary-loss is first seen --
    reach = CC.reachable_nodes(nodes, now, timeout)
    primary_visible = any(n.get("is_primary") for n in reach)
    if primary_visible or override:
        _election["primary_lost_since"] = None
        grace_elapsed = True
    else:
        if _election["primary_lost_since"] is None:
            _election["primary_lost_since"] = now
        grace_elapsed = (now - _election["primary_lost_since"]).total_seconds() >= interval

    decision = CC.evaluate_cluster(nodes, node_id, now, override=override,
                                   election_grace_elapsed=grace_elapsed, timeout=timeout)

    # --- apply ---------------------------------------------------------------
    leader_for_settings = decision.get("current_leader")
    if decision["action"] == "promote_self":
        if auto_failover:
            db.db.cluster.update_one({"_id": "self"}, {"$set": {"is_primary": True}}, upsert=True)
            _election["primary_lost_since"] = None
        else:
            # Auto-failover disabled: DO NOT self-promote. Stay secondary, advertise no
            # leader, and log loudly so an operator knows a manual promotion is needed.
            # (The split-brain guard is untouched — a partitioned primary below still steps
            # down; we only decline to auto-ELECT a new one.)
            decision = {**decision, "action": "manual_failover_required", "self_role": "secondary",
                        "current_leader": None,
                        "reason": "auto-failover disabled — primary lost, manual promotion required"}
            leader_for_settings = None
            logger.warning("cluster[%s]: primary lost and this node is the highest-priority survivor, "
                           "but AUTO-FAILOVER IS DISABLED — staying secondary; promote manually in "
                           "Admin · Cluster to take over", cfg.server.id)
    elif decision["action"] in ("step_down", "follow", "no_leader"):
        if s.get("is_primary"):
            db.db.cluster.update_one({"_id": "self"}, {"$set": {"is_primary": False}}, upsert=True)
    db.db.cluster.update_one({"_id": "self"}, {"$set": {"override_seen": override_seen}}, upsert=True)
    db.db.settings.update_one({"_id": "app"}, {"$set": {"primary_node": leader_for_settings}}, upsert=True)
    return decision


async def loop(cfg, db, logger):
    interval = cfg.federation.health_interval_seconds
    while True:
        try:
            # Re-read the flag every round so an Admin-tab toggle takes effect live.
            af = auto_failover_enabled(cfg, db)
            d = await asyncio.to_thread(run_round, db, cfg, None, _peer_health, af)
            logger.debug("cluster round: %s (auto_failover=%s)", d.get("action"), af)
        except Exception as e:  # noqa: BLE001 — never let a blip kill the loop
            logger.warning("cluster round failed: %s", e)
        await asyncio.sleep(interval)


# --------------------- runtime enable/disable (hot toggle) ------------------
# The gossip loop can be started/stopped WITHOUT a container restart so the Admin tab's
# cluster_enabled switch takes effect live. The task handle lives on app.state.


def is_enabled(cfg, db) -> bool:
    """Effective cluster_enabled: the config default OR a persisted runtime override
    (cluster{self}.cluster_enabled), so an operator's in-UI enable survives a restart."""
    s = db.db.cluster.find_one({"_id": "self"}) or {}
    if s.get("cluster_enabled") is not None:
        return bool(s.get("cluster_enabled"))
    return bool(cfg.federation.cluster_enabled)


def auto_failover_enabled(cfg, db) -> bool:
    """Effective auto_failover: a persisted runtime override (cluster{self}.auto_failover)
    wins over the config default, so an operator's in-UI toggle survives a restart. When
    False, the gossip loop still runs but never auto-promotes a new primary — taking over
    is a manual action."""
    s = db.db.cluster.find_one({"_id": "self"}) or {}
    if s.get("auto_failover") is not None:
        return bool(s.get("auto_failover"))
    return bool(cfg.federation.auto_failover)


def start_gossip(app, cfg, db, logger) -> bool:
    """Start the gossip loop if not already running. Idempotent. Returns True if a task
    is now running."""
    task = getattr(app.state, "gossip_task", None)
    if task and not task.done():
        return True
    app.state.gossip_task = asyncio.create_task(loop(cfg, db, logger))
    logger.info("cluster gossip loop STARTED (health=%ss unreachable=%ss)",
                cfg.federation.health_interval_seconds, cfg.federation.unreachable_after_seconds)
    return True


def stop_gossip(app, logger) -> bool:
    """Cancel the gossip loop if running. Idempotent. Quiesces cleanly — the node keeps
    its last-known role/roster in Mongo; it simply stops electing/gossiping."""
    task = getattr(app.state, "gossip_task", None)
    if task and not task.done():
        task.cancel()
        app.state.gossip_task = None
        logger.info("cluster gossip loop STOPPED (quiesced)")
        return True
    app.state.gossip_task = None
    return False
