"""Correlator — multi-root app discovery.

Regression coverage for the UAT bug where apps installed OUTSIDE ~/projects
(/opt, /srv, /var/www, …) never surfaced. The fix routes the ProjectDetector's
host-mount-aware `project_dirs` (real host paths across ALL configured roots)
into correlate(), so every discovered app becomes a bucket even with no running
assets — and its own directory (wherever it lives) is what gets sized.
"""

import os

from app.correlator import correlate


def _by_name(apps):
    return {a["name"]: a for a in apps}


def test_project_dirs_materializes_buckets_for_apps_outside_projects_root(tmp_path):
    # Apps discovered across several roots, none under projects_root, none with
    # any assets — they must still each show up as a project bucket.
    opt_app = tmp_path / "opt" / "grafana"
    srv_app = tmp_path / "srv" / "gitea"
    www_app = tmp_path / "var" / "www" / "marketing-site"
    for d in (opt_app, srv_app, www_app):
        d.mkdir(parents=True)
    project_dirs = {
        "grafana": str(opt_app),
        "gitea": str(srv_app),
        "marketing-site": str(www_app),
    }

    apps = _by_name(correlate(
        [], server_id="test",
        projects_root=str(tmp_path / "home" / "projects"),  # empty / non-existent
        project_dirs=project_dirs,
    ))

    for name, path in project_dirs.items():
        assert name in apps, f"{name} installed outside projects_root should surface"
        assert apps[name]["type"] == "project"
        assert apps[name]["source"] == path
        # project_dir is the app's OWN discovered dir, not projects_root/<name>.
        assert apps[name]["project_dir"] == path
    assert "System" in apps


def test_supplied_project_dirs_take_precedence_over_bare_walk(tmp_path):
    # When project_dirs is supplied, the correlator must NOT also walk
    # projects_root/direct_roots itself (that bare walk isn't /host-aware).
    (tmp_path / "should_not_appear").mkdir()
    apps = _by_name(correlate(
        [], server_id="test",
        projects_root=str(tmp_path),
        direct_roots=[str(tmp_path)],
        project_dirs={"only_this": "/opt/only_this"},
    ))
    assert "only_this" in apps
    assert "should_not_appear" not in apps


def test_app_outside_projects_root_gets_sized(tmp_path):
    app_dir = tmp_path / "opt" / "bigapp"
    app_dir.mkdir(parents=True)
    (app_dir / "data.bin").write_bytes(b"x" * 4096)

    apps = _by_name(correlate(
        [], server_id="test",
        projects_root=str(tmp_path / "nope"),
        project_dirs={"bigapp": str(app_dir)},
    ))
    assert apps["bigapp"]["project_dir_size_bytes"] >= 4096
    assert apps["bigapp"]["total_size_bytes"] >= 4096
    # The app's own dir is listed as a storage path (real host path).
    assert str(app_dir) in apps["bigapp"]["storage_paths"]


def test_asset_tagged_to_discovered_app_lands_in_its_bucket(tmp_path):
    # A container tagged to an app discovered under /opt joins that app, not System.
    app_dir = tmp_path / "opt" / "grafana"
    app_dir.mkdir(parents=True)
    assets = [{
        "category": "docker_container", "name": "grafana-1", "project": "grafana",
        "metadata": {"running": True, "image": "grafana/grafana"},
    }]
    apps = _by_name(correlate(
        assets, server_id="test",
        projects_root=str(tmp_path / "nope"),
        project_dirs={"grafana": str(app_dir)},
    ))
    assert "grafana-1" in apps["grafana"]["containers"]
    assert "grafana-1" not in apps["System"]["containers"]


def test_container_host_mount_sizing(tmp_path, monkeypatch):
    """In a container the app's real path is /opt/grafana but it is readable at
    /host/opt/grafana. project_dir must store the REAL path while sizing reads
    through the /host mount — the exact case that was invisible during UAT."""
    import app.core.hostpath as hp

    host = tmp_path / "host"
    (host / "opt" / "grafana").mkdir(parents=True)
    (host / "opt" / "grafana" / "big.bin").write_bytes(b"y" * 8192)
    monkeypatch.setattr(hp, "HOST_ROOT", str(host))

    apps = _by_name(correlate(
        [], server_id="test",
        projects_root="/home/msinha/projects",
        project_dirs={"grafana": "/opt/grafana"},  # REAL host path (as detector reports)
    ))
    g = apps["grafana"]
    assert g["project_dir"] == "/opt/grafana"        # real host path stored
    assert g["project_dir_size_bytes"] >= 8192       # sized via the /host mount
    assert g["total_size_bytes"] >= 8192


def test_fallback_bare_walk_when_no_project_dirs(tmp_path):
    # No project_dirs supplied (native/tests): fall back to walking
    # projects_root + direct_roots directly.
    (tmp_path / "projects" / "alpha").mkdir(parents=True)
    (tmp_path / "opt" / "beta").mkdir(parents=True)
    apps = _by_name(correlate(
        [], server_id="test",
        projects_root=str(tmp_path / "projects"),
        direct_roots=[str(tmp_path / "opt")],
    ))
    assert "alpha" in apps and "beta" in apps


def test_data_app_surfaces_end_to_end_from_root_walk(tmp_path, monkeypatch):
    """V7 regression: an app under /data (non-allowlisted) is discovered by the
    default walk from `/` and surfaces as an application bucket with its real host
    path — no per-box INFRADOCS_* override, /data never named."""
    import app.core.project_detector as pdmod
    import app.core.hostpath as hp
    from app.core.config_loader import DEFAULT_SCAN_ROOTS, DEFAULT_SCAN_EXCLUSIONS

    host = tmp_path / "host"
    (host / "data" / "mxh" / ".git").mkdir(parents=True)
    monkeypatch.setattr(hp, "HOST_ROOT", str(host))

    pd = pdmod.ProjectDetector(
        projects_root="/home/msinha/projects",
        scan_roots=DEFAULT_SCAN_ROOTS,            # ["/"]
        direct_roots=["/opt", "/srv", "/var/www"],
        scan_depth=4,
        exclude_paths=DEFAULT_SCAN_EXCLUSIONS,
    )
    apps = _by_name(correlate(
        [], server_id="test",
        projects_root="/home/msinha/projects",
        project_dirs=pd.project_paths(),
    ))
    assert "mxh" in apps, "app under /data must surface as a bucket via the / walk"
    assert apps["mxh"]["source"] == "/data/mxh"   # real host path, not /host-prefixed


def test_detector_project_paths_feed_correlator_end_to_end(tmp_path):
    """The real wiring: ProjectDetector discovers across roots, its project_paths()
    seed the correlator — an /opt app with no assets shows up."""
    from app.core.project_detector import ProjectDetector

    (tmp_path / "home" / "projects" / "webapp").mkdir(parents=True)
    (tmp_path / "opt" / "grafana").mkdir(parents=True)
    (tmp_path / "srv" / "gitea").mkdir(parents=True)

    pd = ProjectDetector(
        projects_root=str(tmp_path / "home" / "projects"),
        direct_roots=[str(tmp_path / "opt"), str(tmp_path / "srv")],
    )
    apps = _by_name(correlate(
        [], server_id="test",
        projects_root=str(tmp_path / "home" / "projects"),
        project_dirs=pd.project_paths(),
    ))
    for name in ("webapp", "grafana", "gitea"):
        assert name in apps, f"{name} discovered by detector should be a bucket"
