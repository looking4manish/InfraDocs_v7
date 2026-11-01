"""V7 (1-Nov-2026, OCI): a home folder is never a project, and `systemctl --user` units are
scanned and attributed like system units (mdb-playground's studio/app/backend)."""

import types

from app.core.project_detector import ProjectDetector, is_home_dir
from app.scanners import systemd as S
from app import actions as A


def _pd():
    return ProjectDetector(projects_root="/nonexistent", scan_roots=[], direct_roots=[], exclude_paths=[])


def test_home_dir_rule():
    assert is_home_dir("/home/msinha") and is_home_dir("/root")
    assert not is_home_dir("/home/msinha/projects/mdb-playground")
    assert not is_home_dir("/home") and not is_home_dir("/opt/app")


def test_home_dir_is_never_promoted_or_added():
    pd = _pd()
    assert pd.register_project_from_dir("/home/msinha") == "System"
    pd._add("msinha", __import__("pathlib").Path("/home/msinha"))
    assert "msinha" not in pd.list_projects()
    # a project inside the home folder still counts
    assert pd.register_project_from_dir("/home/msinha/infradocs") == "infradocs"


def _fake_run(units):
    """units: {name: WorkingDirectory}. Answers list-units / list-unit-files / show / is-enabled
    and records every command it was given."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        out = ""
        if "list-units" in cmd:
            want = [c for c in cmd if c.startswith("--type=")][0].split("=")[1]
            out = "\n".join(f"{n} loaded active running x" for n in units if n.endswith("." + want))
        elif "show" in cmd:
            out = f"WorkingDirectory={units.get(cmd[-1], '')}\nFragmentPath=/home/msinha/.config/systemd/user/{cmd[-1]}\nUnitFileState=enabled"
        return types.SimpleNamespace(stdout=out, returncode=0)
    return run, calls


def test_user_units_scanned_as_owner_and_attributed(monkeypatch, tmp_path):
    (tmp_path / "1001").mkdir()
    (tmp_path / "1001" / "bus").write_text("")
    (tmp_path / "1002").mkdir()               # no bus: no running manager, skipped
    monkeypatch.setattr(S, "_RUN_USER", str(tmp_path))
    monkeypatch.setattr(S, "_host_passwd", lambda: {1001: ("msinha", 1001)})
    monkeypatch.setattr(S.os, "geteuid", lambda: 0)
    pd = _pd()
    pd._add("mdb-playground", __import__("pathlib").Path("/home/msinha/projects/mdb-playground"))
    sysrun, _ = _fake_run({})
    userrun, calls = _fake_run({"mdbpg-backend.service": "/home/msinha/projects/mdb-playground/backend",
                                "mdbpg-studio.service": "/home/msinha/projects/mdb-playground/frontend"})

    def run(cmd, **kw):
        return (userrun if "--user" in cmd else sysrun)(cmd, **kw)
    monkeypatch.setattr(S.subprocess, "run", run)

    assets = S.SystemdScanner(server_id="oci", project_detector=pd).scan()
    by = {a["name"]: a for a in assets}
    assert set(by) == {"mdbpg-backend.service", "mdbpg-studio.service"}
    b = by["mdbpg-backend.service"]
    assert b["project"] == "mdb-playground"
    assert b["asset_id"] == "oci:service:user:msinha:mdbpg-backend.service"
    assert b["metadata"]["scope"] == "user" and b["metadata"]["uid"] == 1001
    # every user-manager call ran AS the user, against that user's bus
    for c in calls:
        assert c[:4] == ["setpriv", "--reuid=1001", "--regid=1001", "--clear-groups"]
        assert f"DBUS_SESSION_BUS_ADDRESS=unix:path={tmp_path}/1001/bus" in c


def test_user_unit_action_runs_as_owner(monkeypatch):
    seen = {}
    monkeypatch.setattr(S.os, "geteuid", lambda: 0)
    monkeypatch.setattr(A, "_run_subprocess", lambda cmd, **kw: seen.setdefault("cmd", cmd))
    asset = {"name": "mdbpg-backend.service", "category": "systemd_service",
             "metadata": {"scope": "user", "uid": 1001, "gid": 1001, "user": "msinha"}}
    A._act_systemd(asset, "restart", {})
    assert seen["cmd"][0] == "setpriv" and seen["cmd"][-3:] == ["--user", "restart", "mdbpg-backend.service"]
    assert "sudo" not in seen["cmd"]
