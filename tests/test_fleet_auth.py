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
from tests import cluster_fixture as CF
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
    CF.self_primary(NODE)


def _secondary(db, nid, seen, address="http://n2:8081"):
    CF.add_secondary(nid, seen, address)


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
