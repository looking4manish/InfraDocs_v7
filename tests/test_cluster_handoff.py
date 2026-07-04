"""Planned PRIMARY switchover (/handoff + /assume-primary), DB-backed; skips without Mongo.

The guarded promote/demote can't hand leadership between two HEALTHY nodes (each guard blocks
the other). /handoff runs on the current primary: step down, then tell the target to assume
primary over the fleet-authed channel — with rollback if the target can't take it. This is what
the Admin fleet console's per-node 'Make primary' button drives.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import dependencies as api_deps
from app.api.main import app
from app.core.config_loader import load_config
from app.core.db_manager import DBManager

TEST_DB = "infradocs_handoff_test"


def _auth():
    cfg = load_config(str(ROOT / "config.yml"))
    return (cfg.auth.username, os.environ.get(cfg.auth.password_env) or cfg.auth.dev_password)


AUTH = _auth()
NODE_ID = load_config(str(ROOT / "config.yml")).server.id


@pytest.fixture
def db():
    cfg = load_config(str(ROOT / "config.yml"))
    if not os.environ.get(cfg.mongodb.uri_env):
        pytest.skip("MongoDB URI not configured")
    d = DBManager(uri=cfg.mongodb.uri, database=TEST_DB)
    yield d
    d.client.drop_database(TEST_DB)
    d.close()


@pytest.fixture
def client(db, monkeypatch):
    cfg = load_config(str(ROOT / "config.yml"))
    monkeypatch.setattr("app.cluster_manager.start_gossip", lambda *a, **k: True)
    monkeypatch.setattr("app.cluster_manager.stop_gossip", lambda *a, **k: True)
    app.dependency_overrides[api_deps.get_db] = lambda: db
    app.dependency_overrides[api_deps.get_config] = lambda: cfg
    yield TestClient(app)
    app.dependency_overrides.clear()


def _be_primary_with_secondary(db, target="oci-p", ago=2):
    db.db.cluster.update_one({"_id": "self"},
        {"$set": {"node_id": NODE_ID, "priority": 1, "is_primary": True}}, upsert=True)
    db.db.settings.update_one({"_id": "app"}, {"$set": {"role": "primary", "primary_node": NODE_ID}}, upsert=True)
    db.db.cluster_nodes.update_one({"node_id": target},
        {"$set": {"node_id": target, "priority": 2, "is_primary": False,
                  "address": "http://target", "last_seen": datetime.now(timezone.utc) - timedelta(seconds=ago)}},
        upsert=True)


def test_handoff_switches_primary_to_reachable_peer(client, db, monkeypatch):
    _be_primary_with_secondary(db, "oci-p")
    calls = {}
    def fake_post(url, body, headers=None, timeout=25):
        calls["url"] = url; calls["auth"] = (headers or {}).get("X-Fleet-Auth")
        return {"ok": True}
    monkeypatch.setattr("app.federation._post_json", fake_post)
    r = client.post("/api/cluster/handoff", auth=AUTH, json={"target": "oci-p"})
    assert r.status_code == 200 and r.json()["leader"] == "oci-p"
    # source stepped down; new leader recorded locally
    s = db.db.cluster.find_one({"_id": "self"})
    assert s["is_primary"] is False
    assert (db.db.settings.find_one({"_id": "app"}) or {}).get("primary_node") == "oci-p"
    assert db.db.cluster_nodes.find_one({"node_id": "oci-p"})["is_primary"] is True
    # target was actually called on its assume-primary with a fleet secret
    assert calls["url"].endswith("/api/cluster/assume-primary") and calls["auth"]


def test_handoff_rolls_back_and_keeps_primary_when_target_fails(client, db, monkeypatch):
    _be_primary_with_secondary(db, "oci-p")
    def boom(*a, **k):
        raise ConnectionError("target down mid-handoff")
    monkeypatch.setattr("app.federation._post_json", boom)
    r = client.post("/api/cluster/handoff", auth=AUTH, json={"target": "oci-p"})
    assert r.status_code == 502
    # rolled back — we are STILL primary, cluster not left leaderless
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is True
    assert (db.db.settings.find_one({"_id": "app"}) or {}).get("primary_node") == NODE_ID


def test_handoff_refused_when_not_primary(client, db):
    db.db.cluster.update_one({"_id": "self"}, {"$set": {"node_id": NODE_ID, "is_primary": False}}, upsert=True)
    r = client.post("/api/cluster/handoff", auth=AUTH, json={"target": "oci-p"})
    assert r.status_code == 409


def test_handoff_refused_when_target_unreachable(client, db, monkeypatch):
    _be_primary_with_secondary(db, "oci-p", ago=300)  # stale -> unreachable
    monkeypatch.setattr("app.federation._post_json", lambda *a, **k: {"ok": True})
    r = client.post("/api/cluster/handoff", auth=AUTH, json={"target": "oci-p"})
    assert r.status_code == 409
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is True  # untouched


def test_assume_primary_requires_fleet_principal(client, db):
    db.db.cluster.update_one({"_id": "self"}, {"$set": {"node_id": NODE_ID, "is_primary": False}}, upsert=True)
    # a browser/admin session must NOT be able to mint a second primary out of band
    r = client.post("/api/cluster/assume-primary", auth=AUTH)
    assert r.status_code == 403
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is False
    # with the shared fleet secret it succeeds
    from app.fleet_auth import set_fleet_secret
    set_fleet_secret(db, "sekret")
    r2 = client.post("/api/cluster/assume-primary", headers={"X-Fleet-Auth": "sekret"})
    assert r2.status_code == 200 and r2.json()["leader"] == NODE_ID
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is True
