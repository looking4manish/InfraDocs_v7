"""Automatic-failover switch + the MANUAL take-over path (DB-backed; skips without Mongo).

Proves:
  - the effective auto_failover flag = runtime override wins over config default;
  - with auto_failover OFF, a lost-primary election that WOULD promote this node is
    suppressed (stays secondary, advertises no leader, logs) — no auto-takeover;
  - with auto_failover ON, the same situation DOES promote_self (unchanged behaviour);
  - the SPLIT-BRAIN GUARD is untouched by the switch: a partitioned primary that has lost
    majority still steps down even with auto_failover OFF;
  - the manual take-over endpoint (/api/cluster/promote) lets an operator take primary when
    auto-failover is off and the old primary is unreachable (force), and still refuses to
    stomp a live primary.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.cluster_manager as MGR
from app.api import dependencies as api_deps
from app.api.main import app
from app.core.config_loader import load_config
from app.core.db_manager import DBManager

TEST_DB = "infradocs_auto_failover_test"
NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


def _auth():
    cfg = load_config(str(ROOT / "config.yml"))
    return (cfg.auth.username, os.environ.get(cfg.auth.password_env) or cfg.auth.dev_password)


AUTH = _auth()
NODE_ID = load_config(str(ROOT / "config.yml")).server.id


@pytest.fixture
def cfg():
    return load_config(str(ROOT / "config.yml"))


@pytest.fixture
def db(cfg):
    if not os.environ.get(cfg.mongodb.uri_env):
        pytest.skip("MongoDB URI not configured")
    d = DBManager(uri=cfg.mongodb.uri, database=TEST_DB)
    yield d
    d.client.drop_database(TEST_DB)
    d.close()


@pytest.fixture(autouse=True)
def _reset_election():
    MGR._election["primary_lost_since"] = None
    yield
    MGR._election["primary_lost_since"] = None


def _ok_ping(recent_primary=False):
    """A ping stub: every peer answers, so it is reachable after this round."""
    def ping(addr, timeout=6):
        return {"node_id": addr.split("//")[-1], "priority": None, "is_primary": recent_primary}
    return ping


def _dead_ping(addr, timeout=6):
    raise ConnectionError("peer down")


# --------------------------- the effective flag -----------------------------


def test_auto_failover_enabled_respects_runtime_override_over_config(cfg, db):
    # config.yml ships auto_failover=false
    assert cfg.federation.auto_failover is False
    assert MGR.auto_failover_enabled(cfg, db) is False          # falls back to config
    db.db.cluster.update_one({"_id": "self"}, {"$set": {"auto_failover": True}}, upsert=True)
    assert MGR.auto_failover_enabled(cfg, db) is True           # runtime override wins
    db.db.cluster.update_one({"_id": "self"}, {"$set": {"auto_failover": False}}, upsert=True)
    assert MGR.auto_failover_enabled(cfg, db) is False


# ------------------- promote suppression vs. auto-promote -------------------


def _seed_lost_primary(db, self_primary=False):
    """This node is priority-1; two reachable peers, NO node currently primary → the
    election would make this node the winner."""
    db.db.cluster.update_one({"_id": "self"},
        {"$set": {"node_id": NODE_ID, "priority": 1, "is_primary": self_primary}}, upsert=True)
    for nid, prio in (("bee", 2), ("cee", 3)):
        db.db.cluster_nodes.update_one({"node_id": nid},
            {"$set": {"node_id": nid, "priority": prio, "is_primary": False,
                      "address": "http://" + nid, "last_seen": NOW - timedelta(seconds=2)}}, upsert=True)


def test_run_round_suppresses_promote_when_auto_failover_off(cfg, db):
    _seed_lost_primary(db)
    MGR._election["primary_lost_since"] = NOW - timedelta(seconds=60)   # grace already elapsed
    d = MGR.run_round(db, cfg, NOW, _ok_ping(), False)                  # auto_failover OFF
    assert d["action"] == "manual_failover_required"
    assert d["current_leader"] is None
    # this node must NOT have taken primary
    assert (db.db.cluster.find_one({"_id": "self"}) or {}).get("is_primary") is not True
    assert (db.db.settings.find_one({"_id": "app"}) or {}).get("primary_node") is None


def test_run_round_auto_promotes_when_auto_failover_on(cfg, db):
    _seed_lost_primary(db)
    MGR._election["primary_lost_since"] = NOW - timedelta(seconds=60)
    d = MGR.run_round(db, cfg, NOW, _ok_ping(), True)                   # auto_failover ON
    assert d["action"] == "promote_self" and d["current_leader"] == NODE_ID
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is True


# ---------------- split-brain guard is UNAFFECTED by the switch -------------


def test_partitioned_primary_still_steps_down_with_auto_failover_off(cfg, db):
    # self IS the primary but both peers are unreachable → minority partition.
    db.db.cluster.update_one({"_id": "self"},
        {"$set": {"node_id": NODE_ID, "priority": 1, "is_primary": True}}, upsert=True)
    for nid, prio in (("bee", 2), ("cee", 3)):
        db.db.cluster_nodes.update_one({"node_id": nid},
            {"$set": {"node_id": nid, "priority": prio, "is_primary": False,
                      "address": "http://" + nid, "last_seen": NOW - timedelta(seconds=300)}}, upsert=True)
    d = MGR.run_round(db, cfg, NOW, _dead_ping, False)                  # auto_failover OFF
    assert d["action"] == "step_down"                                  # incumbent split-brain guard
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is False


# ---------------------- manual take-over endpoint ---------------------------


@pytest.fixture
def client(db, cfg, monkeypatch):
    monkeypatch.setattr("app.cluster_manager.start_gossip", lambda *a, **k: True)
    monkeypatch.setattr("app.cluster_manager.stop_gossip", lambda *a, **k: True)
    app.dependency_overrides[api_deps.get_db] = lambda: db
    app.dependency_overrides[api_deps.get_config] = lambda: cfg
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_manual_promote_takes_over_when_auto_failover_off(client, db):
    # auto-failover off; the old primary is unreachable. The operator manually forces takeover.
    db.db.cluster.update_one({"_id": "self"}, {"$set": {"auto_failover": False}}, upsert=True)
    db.db.cluster_nodes.insert_one(
        {"node_id": "oldprimary", "priority": 2, "is_primary": False,
         "last_seen": datetime.now(timezone.utc) - timedelta(seconds=300), "address": "http://old"})
    # without force, an unreachable node blocks silent takeover
    r1 = client.post("/api/cluster/promote", auth=AUTH, json={"force": False})
    assert r1.json()["needs_force"] is True
    # with force, the operator takes primary
    r2 = client.post("/api/cluster/promote", auth=AUTH, json={"force": True})
    assert r2.json()["promoted"] is True
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is True


def test_manual_promote_still_refuses_a_live_primary(client, db):
    db.db.cluster.update_one({"_id": "self"}, {"$set": {"auto_failover": False}}, upsert=True)
    db.db.cluster_nodes.insert_one(
        {"node_id": "liveprimary", "priority": 2, "is_primary": True,
         "last_seen": datetime.now(timezone.utc) - timedelta(seconds=2), "address": "http://live"})
    r = client.post("/api/cluster/promote", auth=AUTH, json={"force": True})
    assert r.json()["promoted"] is False and r.json()["leader"] == "liveprimary"


def test_state_exposes_auto_failover_flag(client, db):
    r = client.get("/api/cluster/state", auth=AUTH)
    assert r.status_code == 200 and "auto_failover" in r.json()


def test_auto_failover_endpoint_persists_toggle(client, db):
    r = client.post("/api/cluster/auto-failover", auth=AUTH, json={"value": True})
    assert r.json() == {"ok": True, "auto_failover": True}
    assert db.db.cluster.find_one({"_id": "self"})["auto_failover"] is True
    r2 = client.post("/api/cluster/auto-failover", auth=AUTH, json={"value": False})
    assert r2.json()["auto_failover"] is False
