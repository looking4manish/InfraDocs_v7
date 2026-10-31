"""Phase 3 API tests against the live MongoDB replica set."""

import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import dependencies as api_deps
from app.api.main import app
from app.core.config_loader import load_config
from app.core.db_manager import DBManager


PHASE3_TEST_DB = "infradocs_phase3_test"


def _resolve_auth():
    """Match verify_auth: env INFRADOCS_API_PASSWORD wins; dev_password is fallback."""
    _cfg = load_config(str(ROOT / "config.yml"))
    return (
        _cfg.auth.username,
        os.environ.get(_cfg.auth.password_env) or _cfg.auth.dev_password,
    )


AUTH = _resolve_auth()


@pytest.fixture(scope="module")
def cfg():
    return load_config(str(ROOT / "config.yml"))


@pytest.fixture
def client(cfg, monkeypatch):
    """A TestClient with deps overridden to use a throwaway test DB."""
    if not os.environ.get(cfg.mongodb.uri_env):
        pytest.skip("MongoDB URI not configured")

    # Override get_db to use the test DB
    test_db = DBManager(uri=cfg.mongodb.uri, database=PHASE3_TEST_DB)
    test_db.create_indexes()

    # Seed some assets
    sample_assets = [
        {
            "server_id": "oci",
            "asset_id": "oci:service:nginx.service",
            "category": "systemd_service",
            "name": "nginx.service",
            "status": "active",
            "project": "System",
            "metadata": {"unit_type": "service"},
            "health_indicators": {"active": True, "loaded": True},
        },
        {
            "server_id": "oci",
            "asset_id": "oci:container:abc123",
            "category": "docker_container",
            "name": "openwebui",
            "status": "running",
            "project": "openwebui",
            "metadata": {"image": "openwebui:latest"},
            "health_indicators": {"running": True, "restarts": 0},
        },
        {
            "server_id": "oci",
            "asset_id": "oci:mount:/data",
            "category": "storage_mount",
            "name": "/data",
            "status": "mounted",
            "project": "System",
            "metadata": {"usage_percent": 42},
            "health_indicators": {"usage_percent": 42, "near_full": False},
        },
    ]
    for a in sample_assets:
        test_db.upsert_asset(a)

    app.dependency_overrides[api_deps.get_db] = lambda: test_db
    app.dependency_overrides[api_deps.get_config] = lambda: cfg

    yield TestClient(app)

    app.dependency_overrides.clear()
    test_db.client.drop_database(PHASE3_TEST_DB)
    test_db.close()


def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["name"] == "InfraDocs"


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mongo"]["ok"] is True


def test_auth_required(client):
    r = client.get("/api/assets/")
    assert r.status_code == 401


def test_wrong_password(client):
    r = client.get("/api/assets/", auth=("msinha", "wrong"))
    assert r.status_code == 401


def test_list_assets(client):
    r = client.get("/api/assets/", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert {a["category"] for a in body["assets"]} >= {
        "systemd_service",
        "docker_container",
        "storage_mount",
    }


def test_list_assets_filter_by_category(client):
    r = client.get("/api/assets/", params={"category": "docker_container"}, auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["assets"][0]["name"] == "openwebui"


def test_list_assets_filter_by_project(client):
    r = client.get("/api/assets/", params={"project": "openwebui"}, auth=AUTH)
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_assets_categories(client):
    r = client.get("/api/assets/categories", auth=AUTH)
    assert r.status_code == 200
    cats = {row["category"]: row["count"] for row in r.json()["categories"]}
    assert cats == {
        "systemd_service": 1,
        "docker_container": 1,
        "storage_mount": 1,
    }


def test_get_asset_by_id(client):
    r = client.get("/api/assets/oci:container:abc123", auth=AUTH)
    assert r.status_code == 200
    assert r.json()["name"] == "openwebui"


def test_get_asset_404(client):
    r = client.get("/api/assets/oci:container:doesnotexist", auth=AUTH)
    assert r.status_code == 404


def test_projects_list(client):
    r = client.get("/api/projects/list", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    by_name = {p["name"]: p for p in body["projects"]}
    assert "openwebui" in by_name
    assert "System" in by_name
    assert by_name["openwebui"]["asset_count"] == 1
    assert by_name["System"]["asset_count"] == 2
    # Health score should be a non-negative int
    for p in body["projects"]:
        assert 0 <= p["health_score"] <= 100


def test_project_detail(client):
    r = client.get("/api/projects/openwebui", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "openwebui"
    assert len(body["assets"]) == 1


def test_project_detail_404(client):
    r = client.get("/api/projects/NotARealProject", auth=AUTH)
    assert r.status_code == 404


def test_scan_trigger_returns_queued(client):
    """Trigger endpoint returns 202 immediately with a scan_id."""
    r = client.post("/api/scans/trigger", auth=AUTH)
    assert r.status_code == 202
    body = r.json()
    assert "scan_id" in body
    assert body["status"] == "queued"


def test_list_scans_empty(client):
    r = client.get("/api/scans/", auth=AUTH)
    assert r.status_code == 200
    assert "scans" in r.json()


def test_scan_get_404(client):
    r = client.get("/api/scans/nonexistent_id", auth=AUTH)
    assert r.status_code == 404
