"""Teardown ('Kill Button') — plan ordering + execution guards (all mocked)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import teardown
from app.teardown import build_plan, execute_plan

ROOT_DIR = "/home/msinha/projects"


def _app(name="web", **kw):
    base = {
        "name": name, "type": "project", "containers": [], "images": [],
        "volumes": [], "nginx_sites": [], "nginx_detail": [], "certificates": [],
        "systemd_units": [], "links": [{"via": "x"}], "compose_file": None,
        "project_dir": f"{ROOT_DIR}/{name}",
    }
    base.update(kw)
    return base


def test_plan_backups_first_skips_shared_and_dir_last():
    web = _app(
        "web", containers=["web-app"],
        compose_file=f"{ROOT_DIR}/web/docker-compose.yml",
        volumes=[{"name": "web_data", "mountpoint": "/mp"}, {"name": "shared_vol"}],
        images=["nginx:latest"],
    )
    api = _app("api", images=["nginx:latest"], volumes=[{"name": "shared_vol"}])
    plan = build_plan(web, [web, api], projects_root=ROOT_DIR)
    ops = [o["op"] for o in plan["ops"]]

    assert ops[0] == "backup_dir"
    # every op before the first destructive one is a critical backup
    first_destructive = next(i for i, o in enumerate(plan["ops"]) if o.get("destructive"))
    assert first_destructive > 0
    assert all(plan["ops"][i].get("critical") for i in range(first_destructive))
    # shared volume skipped, never an op target
    assert any(s["name"] == "shared_vol" for s in plan["skipped"])
    assert "shared_vol" not in [o["target"] for o in plan["ops"]]
    # data removal is last, and flagged
    assert ops[-1] == "remove_dir"
    assert plan["data_loss"] is True


def test_plan_refuses_system_and_bad_project_dir():
    sysb = _app("System", type="system")
    assert build_plan(sysb, [sysb], projects_root=ROOT_DIR)["refusals"]
    bad = _app("web", project_dir="/etc")  # not <root>/web
    assert any("refusing" in r for r in build_plan(bad, [bad], projects_root=ROOT_DIR)["refusals"])


def test_plan_self_protects_infradocs_units():
    app = _app("web", systemd_units=["infradocs-v6-api.service", "web.service"])
    plan = build_plan(app, [app], projects_root=ROOT_DIR)
    assert any(s["name"] == "infradocs-v6-api.service" for s in plan["skipped"])
    assert "infradocs-v6-api.service" not in [o["target"] for o in plan["ops"]]


def test_execute_aborts_when_backup_fails(tmp_path):
    proj = tmp_path / "web"; proj.mkdir()
    web = _app("web", containers=["web-app"], project_dir=str(proj))
    plan = build_plan(web, [web], projects_root=str(tmp_path))
    db = MagicMock()

    def fake_run(cmd, timeout=120):
        if cmd[0] == "tar":  # backup fails
            return {"status": "failed", "rc": 1, "stdout": "", "stderr": "disk full"}
        return {"status": "success", "rc": 0, "stdout": "", "stderr": ""}

    with patch.object(teardown, "_run", side_effect=fake_run):
        res = execute_plan(plan, db, "msinha", projects_root=str(tmp_path))

    assert res["aborted"] is True
    statuses = {r["op"]: r["status"] for r in res["results"]}
    assert statuses.get("remove_container") == "skipped"  # nothing destructive ran
    assert statuses.get("remove_dir") == "skipped"


def test_execute_fails_loud_when_backup_dir_is_readonly(tmp_path):
    # The projects volume mounted read-only in the API container = os.makedirs raises OSError.
    # Teardown must NOT crash with an invisible 500 — it returns a structured aborted result
    # with a NAMED reason and records it to the actions log, and deletes nothing.
    proj = tmp_path / "web"; proj.mkdir()
    web = _app("web", containers=["web-app"], project_dir=str(proj))
    plan = build_plan(web, [web], projects_root=str(tmp_path))
    db = MagicMock()

    with patch.object(teardown.os, "makedirs",
                      side_effect=OSError(30, "Read-only file system")), \
         patch.object(teardown, "_run") as run:
        res = execute_plan(plan, db, "msinha", projects_root=str(tmp_path))

    assert res["aborted"] is True
    assert res["summary"]["ok"] is False
    assert res["summary"]["failed"] and "read-only" in res["summary"]["failed"][0]["reason"].lower()
    run.assert_not_called()                       # nothing destructive ran
    assert db.record_action.called               # the failure IS surfaced to the actions log


def test_execute_runs_all_when_backups_ok(tmp_path):
    proj = tmp_path / "web"; proj.mkdir()
    web = _app("web", containers=["web-app"], project_dir=str(proj))
    plan = build_plan(web, [web], projects_root=str(tmp_path))
    db = MagicMock()
    with patch.object(teardown, "_run",
                      return_value={"status": "success", "rc": 0, "stdout": "", "stderr": ""}):
        res = execute_plan(plan, db, "msinha", projects_root=str(tmp_path))
    assert res["aborted"] is False
    assert all(r["status"] == "success" for r in res["results"])
    assert db.record_action.call_count == len(plan["ops"])


def test_remove_dir_guard_refuses_outside_root(tmp_path):
    # Defense in depth: even if a bad path reaches the executor, it won't rm it.
    r = teardown._run_op({"op": "remove_dir", "target": "/etc"}, str(tmp_path), str(tmp_path), "web")
    assert r["status"] == "failed" and "refusing" in r["stderr"]
