"""Admin/Cluster tab API (DB-backed). Skips without a MongoDB (like the other DB tests).
Covers the transition endpoints, evict-denominator recompute, the guarded refusals, and
the token lifecycle. Gossip start/stop is stubbed — the live loop is proven separately;
here we assert the persisted transitions + guards.
"""

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import dependencies as api_deps
from app.api.main import app
from app.core.config_loader import load_config
from app.core.db_manager import DBManager

TEST_DB = "infradocs_admin_api_test"


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
    # Stub the live gossip task so tests don't spawn real loops.
    monkeypatch.setattr("app.cluster_manager.start_gossip", lambda *a, **k: True)
    monkeypatch.setattr("app.cluster_manager.stop_gossip", lambda *a, **k: True)
    app.dependency_overrides[api_deps.get_db] = lambda: db
    app.dependency_overrides[api_deps.get_config] = lambda: cfg
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_standalone_to_primary_is_a_flip(client, db):
    r = client.post("/api/cluster/to-primary", auth=AUTH, json={"priority": 1})
    assert r.status_code == 200 and r.json()["role"] == "primary"
    s = db.db.cluster.find_one({"_id": "self"})
    assert s["is_primary"] and s["priority"] == 1 and s["cluster_enabled"] is True


def test_evict_shrinks_fleet_and_tombstones(client, db):
    client.post("/api/cluster/to-primary", auth=AUTH, json={"priority": 1})
    db.db.cluster_nodes.insert_one({"node_id": "n2", "priority": 2, "address": "http://n2", "is_primary": False})
    assert client.get("/api/cluster/state", auth=AUTH).json()["fleet_size"] == 2
    # wrong confirmation is refused
    assert client.post("/api/cluster/evict", auth=AUTH,
                       json={"node_id": "n2", "confirm_hostname": "nope"}).status_code == 400
    ev = client.post("/api/cluster/evict", auth=AUTH, json={"node_id": "n2", "confirm_hostname": "n2"})
    assert ev.status_code == 200 and ev.json()["fleet_size"] == 1
    assert db.db.cluster_tombstones.find_one({"node_id": "n2"}) is not None
    assert client.get("/api/cluster/state", auth=AUTH).json()["fleet_size"] == 1


def test_demote_blocked_when_leaderless(client, db):
    client.post("/api/cluster/to-primary", auth=AUTH, json={"priority": 1})
    r = client.post("/api/cluster/demote", auth=AUTH)
    assert r.status_code == 200 and r.json()["ok"] is False and "leaderless" in r.json()["reason"]


def test_primary_to_standalone_orphan_refusal_then_confirm(client, db):
    client.post("/api/cluster/to-primary", auth=AUTH, json={"priority": 1})
    db.db.cluster_nodes.insert_one({"node_id": "n2", "priority": 2, "address": "http://n2"})
    soft = client.post("/api/cluster/to-standalone", auth=AUTH, json={"force": False})
    assert soft.json()["needs_force"] is True and soft.json()["dependents"] == ["n2"]
    bad = client.post("/api/cluster/to-standalone", auth=AUTH, json={"force": True, "confirm_hostname": "wrong"})
    assert bad.status_code == 400
    ok = client.post("/api/cluster/to-standalone", auth=AUTH, json={"force": True, "confirm_hostname": NODE_ID})
    assert ok.status_code == 200 and ok.json()["role"] == "standalone"
    assert db.db.cluster_nodes.count_documents({}) == 0  # roster dropped


def test_priority_rejects_duplicate(client, db):
    client.post("/api/cluster/to-primary", auth=AUTH, json={"priority": 1})
    db.db.cluster_nodes.insert_one({"node_id": "n2", "priority": 5, "address": "http://n2"})
    assert client.post("/api/cluster/priority", auth=AUTH, json={"priority": 5}).status_code == 409
    assert client.post("/api/cluster/priority", auth=AUTH, json={"priority": 7}).status_code == 200


def test_token_lifecycle_list_and_revoke(client, db):
    tok = client.post("/api/federation/tokens", auth=AUTH, json={"server_id": "n2"}).json()["token"]
    listed = client.get("/api/federation/tokens", auth=AUTH).json()
    assert listed["count"] == 1 and listed["tokens"][0]["server_id"] == "n2"
    assert "token_preview" in listed["tokens"][0]
    rv = client.delete(f"/api/federation/tokens/{tok}", auth=AUTH)
    assert rv.status_code == 200 and rv.json()["ok"] is True
    assert client.get("/api/federation/tokens", auth=AUTH).json()["count"] == 0


def test_transitions_are_audited(client, db):
    client.post("/api/cluster/to-primary", auth=AUTH, json={"priority": 1})
    client.post("/api/cluster/priority", auth=AUTH, json={"priority": 3})
    entries = client.get("/api/cluster/audit", auth=AUTH).json()["entries"]
    actions = {e["action"] for e in entries}
    assert "to_primary" in actions and "priority" in actions
    assert all("ts" in e and "result" in e for e in entries)


def test_admin_endpoints_require_auth(client):
    # No auth header → 401 (nothing on this tab is reachable unauthenticated).
    assert client.post("/api/cluster/to-primary", json={"priority": 1}).status_code == 401
    assert client.get("/api/cluster/audit").status_code == 401
    assert client.post("/api/cluster/evict", json={"node_id": "x", "confirm_hostname": "x"}).status_code == 401


# ---- Primary/Standalone -> Secondary via the shared enrol path (join an existing primary) ----

def _make_primary(client):
    assert client.post("/api/cluster/to-primary", auth=AUTH, json={"priority": 1}).status_code == 200


def test_lone_primary_joins_via_shared_enroll_and_stores_fleet_secret(client, db, monkeypatch):
    _make_primary(client)
    seen = {}

    def fake(primary_url, advertise_url, join_token, server_id, priority, timeout=10):
        seen["server_id"] = server_id  # proves it went through the SAME enrol path (cli_install)
        return {"ok": True, "fleet_secret": "SEC-1",
                "directions": {"secondary_to_primary": True, "primary_to_secondary": True}}

    monkeypatch.setattr("app.federation.enroll_with_primary", fake)
    r = client.post("/api/cluster/join", auth=AUTH, json={
        "primary_url": "http://100.107.140.36:8081", "join_token": "tok",
        "advertise_url": "http://thisnode:8081", "priority": 2})
    assert r.status_code == 200 and r.json()["role"] == "secondary" and r.json()["from_role"] == "primary"
    assert seen["server_id"] == NODE_ID
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is False
    assert db.db.fleet.find_one({"_id": "config"})["secret"] == "SEC-1"   # shared fleet secret received + stored


def test_join_from_primary_with_dependents_is_orphan_guarded(client, db, monkeypatch):
    _make_primary(client)
    db.db.cluster_nodes.insert_one({"node_id": "n2", "priority": 2, "address": "http://n2"})
    body = {"primary_url": "http://p:8081", "join_token": "t", "advertise_url": "http://me:8081", "priority": 3}
    soft = client.post("/api/cluster/join", auth=AUTH, json=body)
    assert soft.status_code == 200 and soft.json()["needs_force"] is True and soft.json()["dependents"] == ["n2"]
    bad = client.post("/api/cluster/join", auth=AUTH, json={**body, "force": True, "confirm_hostname": "wrong"})
    assert bad.status_code == 400
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is True   # NOT transitioned on a bad confirm


def test_join_refused_when_override_pinned(client, db, monkeypatch):
    _make_primary(client)
    client.post("/api/cluster/override", auth=AUTH, json={"value": True})
    r = client.post("/api/cluster/join", auth=AUTH, json={
        "primary_url": "http://p:8081", "join_token": "t", "advertise_url": "http://me:8081", "priority": 2})
    assert r.status_code == 409
    assert "override" in r.json()["detail"]["reason"].lower()
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is True


def test_join_enroll_failure_reports_reason_and_leaves_prior_role(client, db, monkeypatch):
    _make_primary(client)
    monkeypatch.setattr("app.federation.enroll_with_primary",
                        lambda *a, **k: {"ok": False, "reason": "priority 2 already in use (by 'oci')",
                                         "directions": {"secondary_to_primary": True, "primary_to_secondary": None}})
    r = client.post("/api/cluster/join", auth=AUTH, json={
        "primary_url": "http://p:8081", "join_token": "t", "advertise_url": "http://me:8081", "priority": 2})
    assert r.status_code == 400 and "already in use" in r.json()["detail"]["reason"]
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is True   # prior role intact — no half-transition
    assert db.db.fleet.find_one({"_id": "config"}) is None                 # no fleet secret stored


def test_post_join_role_is_secondary_targeting_new_leader(client, db, monkeypatch):
    _make_primary(client)
    monkeypatch.setattr("app.federation.enroll_with_primary",
                        lambda *a, **k: {"ok": True, "fleet_secret": "SEC-2",
                                         "directions": {"secondary_to_primary": True, "primary_to_secondary": True}})
    client.post("/api/cluster/join", auth=AUTH, json={
        "primary_url": "http://100.107.140.36:8081", "join_token": "t",
        "advertise_url": "http://me:8081", "priority": 4})
    st = client.get("/api/cluster/state", auth=AUTH).json()
    assert st["role"] == "secondary"
    assert db.db.cluster.find_one({"_id": "self"})["is_primary"] is False
    assert db.db.settings.find_one({"_id": "app"}).get("primary_node") is None     # redirect gate follows the leader
    assert db.db.fleet.find_one({"_id": "config"})["primary_url"] == "http://100.107.140.36:8081"  # targets new primary
