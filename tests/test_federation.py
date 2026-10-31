"""Scan ingest on the primary: fleet credential, membership, primary-only, stale-term fencing,
and per-server slices."""

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.federation as F
from tests import cluster_fixture as CF
from app import election as E
from app.api import dependencies as api_deps
from app.api.main import app
from app.core.config_loader import load_config
from app.core.db_manager import DBManager
from app.fleet_auth import set_fleet_secret
from datetime import datetime, timezone

TEST_DB = "infradocs_ingest_test"
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


def _push(client, sid="n2", term=1, secret="SEC", assets=None):
    return client.post("/api/federation/ingest", headers={"X-Fleet-Auth": secret},
                       json={"server_id": sid, "term": term,
                             "assets": assets or [{"asset_id": f"{sid}:a", "name": "c1"}],
                             "applications": [{"name": "app1", "application_id": f"{sid}:app:app1"}]})


def test_ingest_accepts_a_member_and_scopes_its_slice(client, db):
    set_fleet_secret(db, "SEC")
    CF.self_primary(NODE)
    CF.add_secondary("n2", datetime.now(timezone.utc))
    CF.add_secondary("n3", datetime.now(timezone.utc))
    assert _push(client, "n2").status_code == 200
    assert _push(client, "n3").status_code == 200
    assert _push(client, "n2", assets=[{"asset_id": "n2:b", "name": "c2"}]).status_code == 200
    assert {a["name"] for a in db.db.assets.find({"server_id": "n2"})} == {"c2"}
    assert db.db.assets.count_documents({"server_id": "n3"}) == 1
    assert db.db.applications.count_documents({}) == 2       # same name on two servers is fine


def test_ingest_refusals(client, db):
    set_fleet_secret(db, "SEC")
    assert _push(client).status_code == 403 or _push(client).status_code == 409
    CF.self_primary(NODE)
    CF.add_secondary("n2", datetime.now(timezone.utc))
    assert _push(client, secret="WRONG").status_code == 403
    assert _push(client, sid="stranger").status_code == 403
    assert _push(client, term=7).status_code == 409           # a newer term: this primary is stale
    E.step_down(RT_node().st, 0)
    assert _push(client).status_code == 409                   # not the primary any more


def RT_node():
    from app import cluster_runtime as RT
    return RT._node


def test_push_sends_fleet_header_and_term(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake(req, timeout=0):
        captured["req"] = req
        return _Resp()

    monkeypatch.setattr(F.urllib.request, "urlopen", fake)
    F.push_to_primary("http://p:8081", "SEC", "n2", [{"name": "x"}], [], term=4)
    req = captured["req"]
    assert req.full_url == "http://p:8081/api/federation/ingest"
    assert req.headers["X-fleet-auth"] == "SEC"
    assert b'"term": 4' in req.data


def test_normalize_url():
    assert F.normalize_url("oci:8081") == "http://oci:8081"
    assert F.normalize_url("http://oci") == "http://oci:8081"
    assert F.normalize_url("https://infra.example.com/") == "https://infra.example.com"
