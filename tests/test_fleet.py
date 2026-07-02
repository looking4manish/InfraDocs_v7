"""Fleet node-switcher (live-proxy + stale-fallback). DB-backed; skips without Mongo.
The HTTP forwarder is injected so no real network is used."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.api.routers.fleet as FL
from app.api import dependencies as api_deps
from app.api.main import app
from app.core.config_loader import load_config
from app.core.db_manager import DBManager

TEST_DB = "infradocs_fleet_test"


def _auth():
    c = load_config(str(ROOT / "config.yml"))
    return (c.auth.username, os.environ.get(c.auth.password_env) or c.auth.dev_password)


AUTH = _auth()
NODE = load_config(str(ROOT / "config.yml")).server.id


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
def client(db):
    cfg = load_config(str(ROOT / "config.yml"))
    app.dependency_overrides[api_deps.get_db] = lambda: db
    app.dependency_overrides[api_deps.get_config] = lambda: cfg
    yield TestClient(app)
    app.dependency_overrides.clear()


def _now():
    return datetime.now(timezone.utc)


def _self_primary(db):
    db.db.cluster.update_one({"_id": "self"},
        {"$set": {"node_id": NODE, "priority": 1, "is_primary": True, "address": "http://localhost:8090"}},
        upsert=True)


def _secondary(db, nid, seen, address="http://n2:8081"):
    db.db.cluster_nodes.update_one({"node_id": nid},
        {"$set": {"node_id": nid, "priority": 2, "address": address, "is_primary": False, "last_seen": seen}},
        upsert=True)


def test_nodes_single_install_one_entry_no_stale(client, db):
    _self_primary(db)
    j = client.get("/api/fleet/nodes", auth=AUTH).json()
    assert j["count"] == 1 and len(j["nodes"]) == 1
    n = j["nodes"][0]
    assert n["node_id"] == NODE and n["self"] and n["reachable"] and n["stale"] is False and n["is_leader"]


def test_proxy_returns_live_data_for_reachable_node(client, db, monkeypatch):
    _self_primary(db)
    _secondary(db, "n2", _now())  # fresh last_seen → reachable
    captured = {}

    def fake(method, url, headers, body, timeout=10):
        captured["url"] = url
        return 200, "application/json", json.dumps({"count": 1, "applications": [{"name": "liveapp"}]}).encode()

    monkeypatch.setattr(FL, "_http_forward", fake)
    r = client.get("/api/fleet/n2/api/applications/list", auth=AUTH)
    assert r.status_code == 200 and r.headers.get("X-Fleet-Live") == "1"
    assert r.json()["applications"][0]["name"] == "liveapp"
    assert captured["url"].startswith("http://n2:8081/api/applications/list")


def test_unreachable_node_falls_back_to_stale_snapshot_with_timestamp(client, db):
    _self_primary(db)
    old = _now() - timedelta(seconds=600)
    _secondary(db, "n2", old)  # stale
    db.db.federation_servers.update_one({"server_id": "n2"},
        {"$set": {"server_id": "n2", "last_synced_at": old, "last_seen": old}}, upsert=True)
    db.db.applications.insert_one({"server_id": "n2", "name": "snapapp"})
    r = client.get("/api/fleet/n2/api/applications/list", auth=AUTH)
    assert r.status_code == 200 and r.headers.get("X-Fleet-Stale") == "1"
    j = r.json()
    assert j["stale"] is True and j["last_synced_at"] is not None
    assert any(a["name"] == "snapapp" for a in j["applications"])


def test_action_is_refused_when_target_node_is_unreachable(client, db):
    _self_primary(db)
    _secondary(db, "n2", _now() - timedelta(seconds=600))  # unreachable
    r = client.post("/api/fleet/n2/api/scans/trigger", auth=AUTH)
    assert r.status_code == 409
    assert "unavailable" in r.json().get("detail", "").lower()
    assert r.headers.get("X-Fleet-Stale") == "1"


def test_leader_persists_full_scan_payload_viewable_after_node_down(client, db):
    _self_primary(db)
    tok = client.post("/api/federation/tokens", auth=AUTH, json={"server_id": "n2"}).json()["token"]
    ing = client.post("/api/federation/ingest", headers={"X-Join-Token": tok},
        json={"server_id": "n2",
              "assets": [{"category": "docker_container", "name": "c1", "project": "p"}],
              "applications": [{"name": "p", "type": "project"}]})
    assert ing.status_code == 200
    fs = db.db.federation_servers.find_one({"server_id": "n2"})
    assert fs.get("last_synced_at") is not None  # leader stored the sync timestamp
    _secondary(db, "n2", _now() - timedelta(seconds=600))  # now down
    apps = client.get("/api/fleet/n2/api/applications/list", auth=AUTH)
    assert apps.status_code == 200 and apps.headers.get("X-Fleet-Stale") == "1"
    assert any(a["name"] == "p" for a in apps.json()["applications"])
    assets = client.get("/api/fleet/n2/api/assets/", auth=AUTH)
    assert any(a["name"] == "c1" for a in assets.json()["assets"])
