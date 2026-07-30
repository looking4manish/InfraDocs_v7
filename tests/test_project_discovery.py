"""Scattered-project discovery — multi-root, marker scan, docker dirs, full paths."""

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import project_detector as pdmod
from app.core.project_detector import ProjectDetector, attach_root_paths


def test_dedicated_root_direct_subfolders(tmp_path):
    (tmp_path / "app1").mkdir()
    (tmp_path / "app2").mkdir()
    pd = ProjectDetector(projects_root=str(tmp_path))
    assert {"app1", "app2"}.issubset(set(pd.list_projects()))


def test_scan_roots_are_marker_only(tmp_path):
    # A broad root with random folders + one real (nested) compose project.
    (tmp_path / "Downloads").mkdir()
    proj = tmp_path / "stuff" / "myapp"
    proj.mkdir(parents=True)
    (proj / "docker-compose.yml").write_text("services: {}")

    pd = ProjectDetector(
        projects_root=str(tmp_path / "nonexistent"),
        scan_roots=[str(tmp_path)],
        scan_depth=3,
    )
    projs = pd.list_projects()
    assert "myapp" in projs            # marker-bearing dir discovered
    assert "Downloads" not in projs    # a random subfolder is NOT a project
    assert pd.project_paths()["myapp"].endswith("/stuff/myapp")
    assert pd.get_project_from_path(str(proj / "data" / "x")) == "myapp"


def test_discovered_docker_dirs_and_longest_prefix(tmp_path):
    d = tmp_path / "scattered" / "dockerapp"
    d.mkdir(parents=True)
    pd = ProjectDetector(
        projects_root=str(tmp_path / "none"),
        discovered={"dockerapp": str(d)},
    )
    assert "dockerapp" in pd.list_projects()
    assert pd.get_project_from_path(str(d / "sub" / "y")) == "dockerapp"
    assert pd.get_project_from_path("/etc/nginx") == "System"


def test_attach_root_paths():
    apps = [{"name": "app1"}, {"name": "System"}]
    attach_root_paths(apps, {"app1": "/data/app1"})
    assert apps[0]["root_path"] == "/data/app1"
    assert "root_path" not in apps[1]


def test_host_root_translation(tmp_path, monkeypatch):
    # Simulate a container: host root mounted at tmp/host, real project at /srv/app.
    host = tmp_path / "host"
    (host / "srv" / "app").mkdir(parents=True)
    (host / "srv" / "app" / "docker-compose.yml").write_text("services: {}")
    monkeypatch.setattr("app.core.hostpath.HOST_ROOT", str(host))

    pd = ProjectDetector(projects_root="/nonexistent", scan_roots=["/srv"], scan_depth=3)
    assert "app" in pd.list_projects()
    # stored as the REAL host path, not the /host-prefixed one
    assert pd.project_paths()["app"] == "/srv/app"
    assert pd.get_project_from_path("/srv/app/data") == "app"


def test_path_component_fallback_for_relocated_app(tmp_path):
    # mxh discovered at <data>/mxh, but its nginx root is /home/x/mxh/dist.
    (tmp_path / "data" / "mxh").mkdir(parents=True)
    pd = ProjectDetector(projects_root=str(tmp_path / "data"))
    assert "mxh" in pd.list_projects()
    assert pd.get_project_from_path("/home/msinha/mxh/frontend/dist") == "mxh"
    assert pd.get_project_from_path("/var/www/other/dist") == "System"


# ---- full-disk multi-root discovery -------------------------------------------------

def test_direct_roots_each_child_is_an_app(tmp_path):
    """direct_roots behave like projects_root: every direct child is an app,
    even a plain install dir with no compose/.git marker (e.g. /opt/<app>)."""
    opt = tmp_path / "opt"
    srv = tmp_path / "srv"
    (opt / "grafana").mkdir(parents=True)      # plain install, no marker
    (srv / "gitea").mkdir(parents=True)
    pd = ProjectDetector(
        projects_root=str(tmp_path / "home_projects"),  # absent
        direct_roots=[str(opt), str(srv)],
    )
    projs = pd.list_projects()
    assert "grafana" in projs and "gitea" in projs
    assert pd.project_paths()["grafana"].endswith("/opt/grafana")
    assert pd.get_project_from_path(str(opt / "grafana" / "conf")) == "grafana"


def test_scan_roots_do_not_promote_home_users_to_apps(tmp_path):
    """A broad marker-hunt root (like /home) must NOT turn every subfolder into an
    app — only marker-bearing dirs surface."""
    home = tmp_path / "home"
    (home / "alice").mkdir(parents=True)                 # a user, not an app
    proj = home / "bob" / "coolapp"
    proj.mkdir(parents=True)
    (proj / ".git").mkdir()                              # a real project marker
    pd = ProjectDetector(
        projects_root=str(tmp_path / "none"),
        scan_roots=[str(home)],
        scan_depth=3,
    )
    projs = pd.list_projects()
    assert "coolapp" in projs
    assert "alice" not in projs and "bob" not in projs


def test_discovery_roots_dedup_and_order(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    pd = ProjectDetector(
        projects_root=str(a),
        direct_roots=[str(b)],
        scan_roots=[str(a), str(b)],   # dupes of the above
    )
    roots = [str(r) for r in pd.discovery_roots()]
    assert roots == [str(a), str(b)]   # deduped, order preserved


# ---- exclusion / traversal guards ---------------------------------------------------

def test_noise_dirs_are_not_descended(tmp_path):
    """A marker buried inside node_modules/venv/.git must be ignored — those hide
    thousands of stub markers that aren't real deployments."""
    buried = tmp_path / "repo" / "node_modules" / "pkg"
    buried.mkdir(parents=True)
    (buried / "docker-compose.yml").write_text("services: {}")
    pd = ProjectDetector(
        projects_root=str(tmp_path / "none"),
        scan_roots=[str(tmp_path)],
        scan_depth=5,
    )
    assert "pkg" not in pd.list_projects()


def test_skipped_mount_is_not_descended(tmp_path, monkeypatch):
    """A dir whose mountpoint fstype is network/tmpfs/etc is not traversed."""
    mnt = tmp_path / "mnt" / "nfsshare"
    proj = mnt / "app"
    proj.mkdir(parents=True)
    (proj / "docker-compose.yml").write_text("services: {}")
    # Pretend /…/mnt/nfsshare is an NFS mount the traversal must skip.
    monkeypatch.setattr(pdmod, "_skip_mountpoints", lambda: {str(mnt)})
    pd = ProjectDetector(
        projects_root=str(tmp_path / "none"),
        scan_roots=[str(tmp_path)],
        scan_depth=5,
    )
    assert "app" not in pd.list_projects()


def test_marker_scan_respects_depth_bound(tmp_path):
    """A marker deeper than scan_depth is not found (bounded traversal)."""
    deep = tmp_path / "l1" / "l2" / "l3" / "l4" / "app"
    deep.mkdir(parents=True)
    (deep / "docker-compose.yml").write_text("services: {}")
    shallow = ProjectDetector(
        projects_root=str(tmp_path / "none"), scan_roots=[str(tmp_path)], scan_depth=2
    )
    assert "app" not in shallow.list_projects()
    deeper = ProjectDetector(
        projects_root=str(tmp_path / "none"), scan_roots=[str(tmp_path)], scan_depth=6
    )
    assert "app" in deeper.list_projects()


def test_deadline_guard_triggers_and_flags_truncation(tmp_path):
    (tmp_path / "app1").mkdir()
    pd = ProjectDetector(projects_root=str(tmp_path))
    # Deadline already in the past → the budget is exhausted, truncation flagged.
    pd._deadline = time.monotonic() - 1
    assert pd._budget_exhausted() is True
    assert pd._truncated is True


def test_zero_timeout_disables_wall_clock_cap(tmp_path):
    (tmp_path / "app1").mkdir()
    pd = ProjectDetector(projects_root=str(tmp_path), scan_timeout_seconds=0)
    assert pd._deadline is None
    assert "app1" in pd.list_projects()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses filesystem permissions")
def test_unreadable_root_logs_loudly_without_crashing(tmp_path, caplog):
    """A configured root that exists but can't be read is logged LOUDLY (named
    reason) and does not crash or abort discovery of the readable roots."""
    good = tmp_path / "good"
    (good / "app_ok").mkdir(parents=True)
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o000)
    try:
        with caplog.at_level("WARNING", logger="app.core.project_detector"):
            pd = ProjectDetector(
                projects_root=str(good),
                direct_roots=[str(locked)],
            )
        assert "app_ok" in pd.list_projects()          # readable root still scanned
        assert any(
            "UNREADABLE" in r.getMessage() and str(locked) in r.getMessage()
            for r in caplog.records
        )
    finally:
        os.chmod(locked, 0o755)


def test_absent_root_is_skipped_quietly(tmp_path, caplog):
    """A root that simply doesn't exist on this box is normal — info, not a crash."""
    with caplog.at_level("INFO", logger="app.core.project_detector"):
        pd = ProjectDetector(
            projects_root=str(tmp_path / "does_not_exist"),
            scan_roots=[str(tmp_path / "also_missing")],
        )
    assert pd.list_projects() == []
    assert not any(r.levelname == "WARNING" for r in caplog.records)


# ---- deny-list walk from `/` (the V7 regression: apps under any top-level dir) ----


def test_walk_from_root_finds_app_under_arbitrary_top_level_dir(tmp_path, monkeypatch):
    """The exact UAT miss: an app under a NON-allowlisted top-level dir (/data/<app>)
    must be discovered by a default walk from `/`, with its real host path — no
    per-box override, /data never named anywhere."""
    host = tmp_path / "host"
    (host / "data" / "mxh" / ".git").mkdir(parents=True)          # marker app under /data
    (host / "data" / "acme" / "docker-compose.yml").parent.mkdir(parents=True)
    (host / "data" / "acme" / "docker-compose.yml").write_text("services: {}")
    (host / "opt" / "grafana").mkdir(parents=True)                # marker-less install root
    monkeypatch.setattr("app.core.hostpath.HOST_ROOT", str(host))

    pd = ProjectDetector(
        projects_root="/home/msinha/projects",
        scan_roots=["/"],                    # deny-list walk from root
        direct_roots=["/opt", "/srv", "/var/www"],
        scan_depth=4,
    )
    paths = pd.project_paths()
    assert paths.get("mxh") == "/data/mxh"   # discovered at its REAL host path
    assert "acme" in paths                    # compose app under /data too
    assert paths.get("grafana") == "/opt/grafana"  # direct-root install still works


def test_walk_from_root_prunes_excluded_trees(tmp_path, monkeypatch):
    """A marker inside an EXCLUDED tree (e.g. /usr/lib) must NOT become an app; a
    marker just outside it must. Exclusion is a deny-list, applied on the real path."""
    host = tmp_path / "host"
    (host / "usr" / "lib" / "somepkg" / ".git").mkdir(parents=True)   # excluded
    (host / "usr" / "local" / "realapp" / ".git").mkdir(parents=True)  # NOT excluded
    monkeypatch.setattr("app.core.hostpath.HOST_ROOT", str(host))

    pd = ProjectDetector(
        projects_root="/nonexistent",
        scan_roots=["/"],
        scan_depth=5,
        exclude_paths=["/usr/lib", "/usr/bin", "/usr/sbin", "/usr/share"],
    )
    projs = pd.list_projects()
    assert "somepkg" not in projs           # pruned: lives under excluded /usr/lib
    assert "realapp" in projs               # /usr/local is not excluded
    # the exclusion set is the built-in pseudo-dirs unioned with the config list
    assert "/usr/lib" in pd.exclude_paths and "/proc" in pd.exclude_paths


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses filesystem permissions")
def test_configured_exclusion_prunes_even_permissioned_dir(tmp_path, monkeypatch):
    """An excluded path is skipped before we even try to read it."""
    host = tmp_path / "host"
    (host / "var" / "log" / "app" / ".git").mkdir(parents=True)  # /var/log excluded
    (host / "data" / "keep" / ".git").mkdir(parents=True)
    monkeypatch.setattr("app.core.hostpath.HOST_ROOT", str(host))
    pd = ProjectDetector(
        projects_root="/nope", scan_roots=["/"], scan_depth=4,
        exclude_paths=["/var/log", "/var/cache"],
    )
    assert "keep" in pd.list_projects()
    assert "app" not in pd.list_projects()


def test_filesystem_root_unreadable_via_host_mount_fails_loud(tmp_path, monkeypatch, caplog):
    """If `/` can't be read through the /host mount (mount missing/broken), log a
    LOUD, named ERROR — a misconfigured mount must be obvious, not silent."""
    # HOST_ROOT points at a path with no readable root, so _read_base('/') is None.
    monkeypatch.setattr("app.core.hostpath.HOST_ROOT", str(tmp_path / "missing_host"))
    with caplog.at_level("ERROR", logger="app.core.project_detector"):
        pd = ProjectDetector(projects_root="/nonexistent", scan_roots=["/"])
    assert pd.list_projects() == []
    err = [r for r in caplog.records if r.levelname == "ERROR"]
    assert err and "/host mount" in err[0].getMessage()
    assert "'/'" in err[0].getMessage()


def test_preflight_logs_resolved_plan(tmp_path, caplog):
    """Preflight logs the resolved roots, exclusion set, and depth/timeout caps."""
    (tmp_path / "app").mkdir()
    with caplog.at_level("INFO", logger="app.core.project_detector"):
        ProjectDetector(
            projects_root=str(tmp_path), scan_roots=[str(tmp_path)],
            scan_depth=4, scan_timeout_seconds=90, exclude_paths=["/boot"],
        )
    plan = [r.getMessage() for r in caplog.records if "discovery plan" in r.getMessage()]
    assert plan, "a preflight discovery-plan line must be logged"
    msg = plan[0]
    assert "scan_depth=4" in msg and "timeout=90s" in msg and "/boot" in msg


# ---- config defaults + env overrides ----


def test_config_default_scan_root_is_filesystem_root():
    """Out-of-box default is a walk from `/` with a non-empty deny-list — not an
    allow-list of blessed dirs."""
    from app.core.config_loader import (
        DEFAULT_SCAN_ROOTS, DEFAULT_SCAN_EXCLUSIONS, load_config,
    )
    assert DEFAULT_SCAN_ROOTS == ["/"]
    assert "/proc" in DEFAULT_SCAN_EXCLUSIONS and "/boot" in DEFAULT_SCAN_EXCLUSIONS
    cfg = load_config(str(ROOT / "config.yml"))
    assert cfg.paths.scan_roots == ["/"]
    assert cfg.paths.scan_exclusions and "/proc" in cfg.paths.scan_exclusions
    assert "/usr/lib" in cfg.paths.scan_exclusions and "/boot" in cfg.paths.scan_exclusions


def test_empty_scan_roots_env_does_not_disable_walk(tmp_path, monkeypatch):
    """compose passes INFRADOCS_SCAN_ROOTS="" when unset — that must NOT collapse
    scan_roots to [] (which would silently disable the whole from-/ walk); it falls
    through to the config.yml / default value."""
    import shutil
    from app.core.config_loader import load_config

    cfgfile = tmp_path / "config.yml"
    shutil.copy(ROOT / "config.yml", cfgfile)
    monkeypatch.setenv("INFRADOCS_SCAN_ROOTS", "")        # empty, as compose sets it
    monkeypatch.setenv("INFRADOCS_DIRECT_ROOTS", "  ")    # whitespace
    monkeypatch.setenv("INFRADOCS_SCAN_EXCLUSIONS", "")
    cfg = load_config(str(cfgfile))
    assert cfg.paths.scan_roots == ["/"]                  # default, not []
    assert cfg.paths.direct_roots and cfg.paths.scan_exclusions


def test_scan_root_and_exclusion_env_overrides(tmp_path, monkeypatch):
    """INFRADOCS_SCAN_ROOTS / INFRADOCS_SCAN_EXCLUSIONS widen/narrow without code edits."""
    import shutil
    from app.core.config_loader import load_config

    cfgfile = tmp_path / "config.yml"
    shutil.copy(ROOT / "config.yml", cfgfile)
    monkeypatch.setenv("INFRADOCS_SCAN_ROOTS", "/data,/opt")
    monkeypatch.setenv("INFRADOCS_SCAN_EXCLUSIONS", "/proc,/boot")
    cfg = load_config(str(cfgfile))
    assert cfg.paths.scan_roots == ["/data", "/opt"]
    assert cfg.paths.scan_exclusions == ["/proc", "/boot"]
