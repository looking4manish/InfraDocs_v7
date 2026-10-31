"""Production glue for the election engine: MongoDB persistence, HTTP transport between
members, host facts for the Servers page, and the background loop.

One `Node` per process. It starts once setup is complete (first server) or once a join has
succeeded (later servers). Until then this node is not part of any replica set.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import election as E
from app.election_manager import Node

log = logging.getLogger("app.cluster")
VERSION = "7.0.0"
SCAN_OVERDUE_AFTER = timedelta(minutes=15)

_node: Optional[Node] = None
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


# ───────────── storage ─────────────

class MongoStore:
    """Election state lives in this node's own MongoDB (no shared database)."""

    def __init__(self, db):
        self.db = db

    def load(self):
        doc = self.db.db.election.find_one({"_id": "state"})
        if doc:
            doc.pop("_id", None)
        return doc

    def save(self, doc):
        self.db.db.election.replace_one({"_id": "state"}, {"_id": "state", **doc}, upsert=True)

    def record_election(self, ev):
        self.db.db.election_history.insert_one(dict(ev))

    def elections(self, limit=20):
        return list(self.db.db.election_history.find({}, {"_id": 0}).sort("term", -1).limit(limit))


# ───────────── transport ─────────────

class HttpTransport:
    """POSTs `/api/cluster/rpc/<name>` on the member's address with the fleet secret."""

    def __init__(self, secret_fn):
        self.secret_fn = secret_fn

    def send(self, member: E.Member, rpc: str, payload: dict, timeout: float):
        if not member.address:
            return None
        url = member.address.rstrip("/") + f"/api/cluster/rpc/{rpc}"
        body = json.dumps(payload, default=str).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "X-Fleet-Auth": self.secret_fn() or ""})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — member addresses only
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None


# ───────────── host facts ─────────────

_cpu_prev = {"t": None}


def _host_root() -> str:
    return os.environ.get("INFRADOCS_HOST_ROOT", "/")


def _os_name() -> str:
    for p in (os.path.join(_host_root(), "etc/os-release"), "/etc/os-release"):
        try:
            with open(p) as f:
                kv = dict(line.rstrip().split("=", 1) for line in f if "=" in line)
            name = kv.get("NAME", "").strip('"')
            ver = kv.get("VERSION_ID", "").strip('"')
            return f"{name} {ver}".strip()
        except OSError:
            continue
    return ""


def host_facts() -> dict:
    """CPU, memory and disk for the Servers page. The API container shares the host's PID
    namespace and /proc, so psutil reads the host; the disk is the host root via /host."""
    out = {"os": _os_name()}
    try:
        import psutil
        out["cpu_count"] = psutil.cpu_count() or None
        out["cpu_pct"] = round(psutil.cpu_percent(interval=None))
        out["mem_pct"] = round(psutil.virtual_memory().percent)
        out["mem_gb"] = round(psutil.virtual_memory().total / 2**30)
    except Exception:  # noqa: BLE001
        pass
    try:
        du = shutil.disk_usage(_host_root())
        out["disk_pct"] = round(du.used * 100 / du.total)
    except OSError:
        pass
    return out


def _last_scan(db) -> Optional[datetime]:
    doc = db.db.scan_logs.find_one({"status": {"$in": ["success", "failed"]}}, sort=[("started_at", -1)])
    t = (doc or {}).get("finished_at") or (doc or {}).get("started_at")
    if t is not None and t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def make_info(db):
    cache = {"at": 0.0, "val": {}}

    def info():
        now = time.monotonic()
        if now - cache["at"] > 5:
            last = _last_scan(db)
            cache["val"] = {
                "version": VERSION, "host": host_facts(),
                "last_scan_at": last.isoformat() if last else None,
                "scan_overdue": bool(last and datetime.now(timezone.utc) - last > SCAN_OVERDUE_AFTER),
            }
            cache["at"] = now
        return cache["val"]
    return info


# ───────────── lifecycle ─────────────

def node() -> Optional[Node]:
    return _node


def in_cluster(db) -> bool:
    return db.db.election.find_one({"_id": "state"}) is not None


def bootstrap_first_server(db, node_id: str, address: str) -> None:
    """Setup on the first server: a one-member replica set where this server is primary."""
    from app.core.config_loader import get_config
    fed = get_config().federation
    cfg = E.Config(members=[E.Member(node_id=node_id, address=address, priority=1, votes=1)],
                   heartbeat_s=float(fed.heartbeat_seconds), election_timeout_s=float(fed.election_timeout_seconds))
    MongoStore(db).save({"term": 0, "voted_for": None, "role": E.SECONDARY, "leader_id": None,
                         "config": cfg.to_dict()})


def adopt_joined_state(db, term: int, config: dict, leader_id: Optional[str]) -> None:
    """A server that has just joined starts as a secondary in the primary's term."""
    MongoStore(db).save({"term": int(term), "voted_for": None, "role": E.SECONDARY,
                         "leader_id": leader_id, "config": config})


def start(db, node_id: str) -> Optional[Node]:
    """Start the election loop (idempotent). Returns None while this server is not in a set."""
    global _node, _thread
    if _node is not None:
        return _node
    if not in_cluster(db):
        return None
    from app.fleet_auth import get_fleet_secret, get_or_create_fleet_secret
    get_or_create_fleet_secret(db)
    _node = Node(node_id, MongoStore(db), HttpTransport(lambda: get_fleet_secret(db)), info=make_info(db))
    _stop.clear()

    def loop():
        log.info("cluster: election loop started for %s (term %s, %s)",
                 node_id, _node.st.term, E.failover_mode(_node.st.config)[1])
        last_leader = _node.st.leader_id
        while not _stop.is_set():
            started = time.monotonic()
            try:
                _node.round()
                leader = _node.st.leader_id
                if leader != last_leader:
                    log.info("cluster: primary is now %s (term %s)", leader, _node.st.term)
                    if leader and leader != node_id:
                        _push_soon()          # a new primary has no copy of this server's scan yet
                    last_leader = leader
            except Exception as e:  # noqa: BLE001 — one bad round must not end the loop
                log.warning("cluster: round failed: %s", e)
            hb = _node.st.config.heartbeat_s
            _stop.wait(max(0.2, hb - (time.monotonic() - started)))
        log.info("cluster: election loop stopped")

    _thread = threading.Thread(target=loop, name="election", daemon=True)
    _thread.start()
    return _node


def _push_soon() -> None:
    def run():
        try:
            from app.api.routers.scans import run_scan_now
            run_scan_now()
        except Exception as e:  # noqa: BLE001
            log.warning("cluster: scan after primary change failed: %s", e)
    threading.Thread(target=run, name="scan-after-election", daemon=True).start()


def stop() -> None:
    global _node, _thread
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
    _node, _thread = None, None


def leader_address() -> Optional[str]:
    n = _node
    if n is None:
        return None
    m = n.leader_member()
    return m.address if m else None


def is_primary_or_standalone() -> bool:
    """True when this server should serve the UI and accept scans: it is the primary, or it
    is not in a replica set at all."""
    n = _node
    return n is None or n.is_primary()


def current_term() -> int:
    n = _node
    return n.st.term if n else 0
