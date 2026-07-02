"""Cross-node fleet auth — the shared-secret fix for the login-loop. DB-backed; skips
without Mongo. The peer HTTP forwarder is injected (no real network)."""

import io
import os
import sys
import urllib.error
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
from app.fleet_auth import set_fleet_secret

TEST_DB = "infradocs_fleet_auth_test"


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
        {"$set": {"node_id": NODE, "priority": 1, "is_primary": True, "address": "http://localhost:8090"}}, upsert=True)


def _secondary(db, nid, seen):
    db.db.cluster_nodes.update_one({"node_id": nid},
        {"$set": {"node_id": nid, "priority": 2, "address": "http://n2:8081", "is_primary": False, "last_seen": seen}},
        upsert=True)


def test_shared_secret_accepted_and_proxy_presents_it(client, db, monkeypatch):
    # The exact loop scenario, asserted fixed: (1) a peer holding the shared secret ACCEPTS a
    # fleet-credentialed call (no basic auth, no session, no 401); (2) the leader's proxy
    # PRESENTS that shared secret to the peer instead of the operator's browser token.
    set_fleet_secret(db, "SEC-123")
    accepted = client.get("/api/fleet/nodes", headers={"X-Fleet-Auth": "SEC-123"})
    assert accepted.status_code == 200

    _self_primary(db)
    _secondary(db, "n2", _now())  # reachable
    cap = {}

    def fake(method, url, headers, body, timeout=10):
        cap.update(headers)
        return 200, "application/json", b'{"applications": [{"name": "liveapp"}]}'

    monkeypatch.setattr(FL, "_http_forward", fake)
    live = client.get("/api/fleet/n2/api/applications/list", auth=AUTH)
    assert live.status_code == 200 and live.headers.get("X-Fleet-Live") == "1"
    assert cap.get("X-Fleet-Auth") == "SEC-123"     # leader authenticates fleet-wide
    assert "Authorization" not in cap               # browser session is NOT forwarded


def test_peer_auth_failure_falls_back_to_stale_not_login_redirect(client, db, monkeypatch):
    set_fleet_secret(db, "SEC")
    _self_primary(db)
    _secondary(db, "n2", _now())  # reachable via gossip, but the peer will reject the credential

    def fake(method, url, headers, body, timeout=10):
        raise urllib.error.HTTPError(url, 401, "unauthorized", {}, io.BytesIO(b"nope"))

    monkeypatch.setattr(FL, "_http_forward", fake)
    r = client.get("/api/fleet/n2/api/applications/list", auth=AUTH)
    assert r.status_code != 401           # never a 401 to the browser -> no login redirect loop
    assert r.status_code == 200
    assert r.headers.get("X-Fleet-Stale") == "1" and r.headers.get("X-Fleet-Auth-Failed") == "1"
    assert "auth" in r.json().get("reason", "").lower()


def test_enroll_delivers_fleet_secret_and_peer_validates_leader_token(client, db, monkeypatch):
    # Bidirectional reachability: stub the primary's back-connection to the secondary.
    monkeypatch.setattr("app.federation.ping_node", lambda url, timeout=8: {"server_id": "n2"})
    _self_primary(db)
    tok = client.post("/api/federation/tokens", auth=AUTH, json={"server_id": "n2"}).json()["token"]
    r = client.post("/api/federation/enroll",
        json={"server_id": "n2", "secondary_url": "http://n2:8081", "join_token": tok, "priority": 2})
    assert r.status_code == 200 and r.json()["ok"] is True
    secret = r.json()["fleet_secret"]
    assert secret  # enroll delivered the shared secret over the token-gated channel
    # A node holding that shared secret validates a leader-presented fleet credential.
    assert client.get("/api/fleet/nodes", headers={"X-Fleet-Auth": secret}).status_code == 200
    # And an already-enrolled node can re-fetch it with its durable join token (convergence).
    conv = client.post("/api/federation/fleet-secret", headers={"X-Join-Token": tok}, json={"server_id": "n2"})
    assert conv.status_code == 200 and conv.json()["fleet_secret"] == secret
