"""Teardown works for an app WHEREVER it lives on disk (the stale ~/projects-only guard is
gone), while keeping the structural catastrophe guards, shared-skip, confirm-gated execution,
and the fleet unreachable 409 gate."""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import teardown
from app.teardown import build_plan, execute_plan, _structural_refusal


def _app(name="web", **kw):
    base = {"name": name, "type": "project", "containers": [], "images": [],
            "volumes": [], "nginx_sites": [], "nginx_detail": [], "certificates": [],
            "systemd_units": [], "links": [{"via": "x"}], "compose_file": None,
            "project_dir": f"/home/data/project/{name}"}
    base.update(kw)
    return base


def test_teardown_permitted_for_app_outside_projects_root():
    # The EXACT failing case: an app discovered at /home/data/project/mdb-discovery.
    app = _app("mdb-discovery", project_dir="/home/data/project/mdb-discovery")
    plan = build_plan(app, [app], projects_root="/home/msinha/projects")
    assert plan["refusals"] == []                                          # no longer refused
    ops = {o["op"]: o["target"] for o in plan["ops"]}
    assert ops.get("remove_dir") == "/home/data/project/mdb-discovery"     # its OWN dir is the target
    assert ops.get("backup_dir") == "/home/data/project/mdb-discovery"


def test_structural_guards_refuse_root_home_and_system_trees():
    assert _structural_refusal("")                                   # empty
    assert _structural_refusal("/")                                  # filesystem root
    assert _structural_refusal("/opt")                               # top-level dir (depth 1)
    assert _structural_refusal("/etc")                               # system tree root
    assert _structural_refusal("/var/lib/thing")                     # under a system tree
    assert _structural_refusal("/usr/local/app")
    assert _structural_refusal(os.path.expanduser("~"))              # $HOME itself
    # a path that NORMALISES (../..) out of the app dir into a system tree is refused
    assert _structural_refusal("/home/data/project/app/../../../../etc")
    # genuine deep app dirs under any root are allowed
    assert _structural_refusal("/home/data/project/mdb-discovery") is None
    assert _structural_refusal("/srv/apps/myapp") is None
    # and build_plan surfaces a dangerous dir as an explicit refusal (not a silent allow)
    bad = _app("web", project_dir="/")
    assert any("refusing" in r for r in build_plan(bad, [bad], projects_root="/home/msinha/projects")["refusals"])


def test_shared_asset_still_skipped_not_removed():
    web = _app("web", volumes=[{"name": "web_data"}, {"name": "shared_vol"}])
    api = _app("api", volumes=[{"name": "shared_vol"}])
    plan = build_plan(web, [web, api], projects_root="/home/msinha/projects")
    assert any(s["name"] == "shared_vol" for s in plan["skipped"])         # shared -> skipped
    assert "shared_vol" not in [o["target"] for o in plan["ops"]]          # never an op target
    assert "web_data" in [o["target"] for o in plan["ops"]]                # owner's own vol still removed


def test_confirm_gated_execution_stops_units_and_removes_assets(tmp_path):
    proj = tmp_path / "app"; proj.mkdir()
    app = _app("app", containers=["app-c"], systemd_units=["app.service"], project_dir=str(proj))
    plan = build_plan(app, [app], projects_root=str(tmp_path))
    cmds = []

    def rec(cmd, timeout=120):
        cmds.append(list(cmd))
        return {"status": "success", "rc": 0, "stdout": "", "stderr": ""}

    db = MagicMock()
    with patch.object(teardown, "_run", side_effect=rec):
        res = execute_plan(plan, db, "msinha", projects_root=str(tmp_path))

    assert res["aborted"] is False
    flat = [" ".join(c) for c in cmds]
    assert any("systemctl disable --now app.service" in c for c in flat)   # units actually stopped
    assert any(c.startswith("docker rm -f app-c") for c in flat)           # container removed
    assert any(c.startswith("rm -rf ") and "app" in c for c in flat)       # dir removed
    s = res["summary"]
    assert s["ok"] is True and s["failed"] == []
    done = {r["op"] for r in s["removed_stopped"]}
    assert {"disable_unit", "remove_container", "remove_dir"} <= done


# ---- fleet unreachable 409 gate (DB-backed; skips without Mongo) ----

def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def db_fx():
    from app.core.config_loader import load_config
    from app.core.db_manager import DBManager
    cfg = load_config(str(ROOT / "config.yml"))
    if not os.environ.get(cfg.mongodb.uri_env):
        pytest.skip("MongoDB URI not configured")
    d = DBManager(uri=cfg.mongodb.uri, database="infradocs_teardown_fleet_test")
    yield d
    d.client.drop_database("infradocs_teardown_fleet_test")
    d.close()


@pytest.fixture
def client_fx(db_fx):
    from fastapi.testclient import TestClient
    from app.api import dependencies as api_deps
    from app.api.main import app as fastapi_app
    from app.core.config_loader import load_config
    cfg = load_config(str(ROOT / "config.yml"))
    fastapi_app.dependency_overrides[api_deps.get_db] = lambda: db_fx
    fastapi_app.dependency_overrides[api_deps.get_config] = lambda: cfg
    yield TestClient(fastapi_app)
    fastapi_app.dependency_overrides.clear()


def test_teardown_via_fleet_proxy_refused_when_node_unreachable(client_fx, db_fx):
    from app.core.config_loader import load_config
    cfg = load_config(str(ROOT / "config.yml"))
    node = cfg.server.id
    auth = (cfg.auth.username, os.environ.get(cfg.auth.password_env) or cfg.auth.dev_password)
    from tests import cluster_fixture as CF
    CF.self_primary(node)
    CF.add_secondary("n2", _now() - timedelta(seconds=600))
    # Teardown fired at an UNREACHABLE node via the switcher must hit the 409 action-gate.
    r = client_fx.post("/api/fleet/n2/api/applications/web/teardown", auth=auth,
                       json={"dry_run": False, "confirm": "web"})
    assert r.status_code == 409
    assert "unavailable" in r.json().get("detail", "").lower()
    assert r.headers.get("X-Fleet-Stale") == "1"
