"""A systemd-tracked app under a non-marker path (e.g. /home/data/project/<app>) must
surface as a project — its WorkingDirectory is proof it's a real app, even without a
.git/docker-compose marker and even outside the direct roots. Regression for the UAT
miss where such a folder never showed up.
"""

from app.correlator import correlate
from app.core.project_detector import ProjectDetector, _parse_skip_mounts
from app.scanners.systemd import SystemdScanner


# ---- containerized mount parsing (the overlay-root bug) ----

def test_skip_mounts_never_includes_root_and_strips_host_prefix():
    # A container's /proc/mounts: root is overlay, /host is the bind of it, and a real
    # host nfs/tmpfs submount shows up under /host.
    lines = [
        "overlay / overlay rw 0 0",
        "overlay /host overlay ro 0 0",
        "nfs4 /host/mnt/share nfs4 rw 0 0",
        "tmpfs /host/run/user/0 tmpfs rw 0 0",
        "/dev/sda1 /host/data ext4 rw 0 0",   # real disk — must NOT be skipped
    ]
    skip = _parse_skip_mounts(lines, "/host")
    assert "/" not in skip, "the filesystem root (overlay) must never be a skip mount"
    assert "/host" not in skip
    assert "/mnt/share" in skip and "/run/user/0" in skip   # real host paths, prefix stripped
    assert "/data" not in skip                              # ext4 is not a skipped fstype


def test_overlay_root_does_not_block_promotion():
    # Simulate the container: '/' in skip_mounts must not make every dir unpromotable.
    pd = ProjectDetector(projects_root="/nonexistent", scan_roots=[], direct_roots=[],
                         exclude_paths=[])
    pd._skip_mounts = {"/mnt/share"}   # a correctly-parsed skip set (no "/")
    assert pd.is_promotable_dir("/home/data/project/mdb-discovery") is True


# ---- detector.register_project_from_dir ----

def _pd():
    # No filesystem walk (scan_roots empty); mirror the prod exclusion set.
    return ProjectDetector(
        projects_root="/nonexistent", scan_roots=[], direct_roots=[],
        exclude_paths=["/proc", "/sys", "/usr/lib", "/var/lib/docker", "/var/cache"],
    )


def test_register_promotes_app_under_arbitrary_top_level_dir():
    pd = _pd()
    assert pd.register_project_from_dir("/home/data/project/mdb-discovery") == "mdb-discovery"
    assert pd.project_paths()["mdb-discovery"] == "/home/data/project/mdb-discovery"
    # and now assets under it attribute correctly
    assert pd.get_project_from_path("/home/data/project/mdb-discovery/db") == "mdb-discovery"


def test_register_rejects_reserved_and_excluded_paths():
    pd = _pd()
    assert pd.register_project_from_dir("/") == "System"
    assert pd.register_project_from_dir("/opt") == "System"          # bare top-level dir
    assert pd.register_project_from_dir("/usr/lib/python3/x") == "System"  # system top
    assert pd.register_project_from_dir("/var/lib/docker/x") == "System"   # excluded tree
    assert pd.register_project_from_dir("relative/path") == "System"
    assert pd.list_projects() == []


def test_register_allows_opt_and_srv_children():
    pd = _pd()
    assert pd.register_project_from_dir("/opt/grafana") == "grafana"
    assert pd.register_project_from_dir("/srv/gitea") == "gitea"


# ---- systemd scanner uses it ----

def _unit_asset(pd, unit, show):
    sc = SystemdScanner(server_id="oci", project_detector=pd)
    sc._is_enabled = lambda n: True  # avoid a real `systemctl is-enabled` subprocess
    return sc._build_unit_asset(
        "service", "systemd_service", unit,
        load_state="loaded", active_state="active", sub_state="running", show=show,
    )


def test_systemd_service_workingdir_promotes_new_project():
    pd = _pd()
    asset = _unit_asset(pd, "mdb-discovery.service", {
        "FragmentPath": "/etc/systemd/system/mdb-discovery.service",  # unit file in /etc → System
        "WorkingDirectory": "/home/data/project/mdb-discovery",       # …but the app lives here
        "ExecStart": "", "UnitFileState": "enabled",
    })
    assert asset["project"] == "mdb-discovery"
    assert pd.project_paths().get("mdb-discovery") == "/home/data/project/mdb-discovery"


def test_systemd_system_service_stays_system():
    pd = _pd()
    # A real OS service: unit in /lib/systemd, working dir under a system tree.
    asset = _unit_asset(pd, "cloud-init.service", {
        "FragmentPath": "/lib/systemd/system/cloud-init.service",
        "WorkingDirectory": "/var/lib/cloud", "ExecStart": "/usr/bin/cloud-init",
    })
    assert asset["project"] == "System"
    assert "cloud" not in pd.list_projects() and "cloud-init.service" not in pd.list_projects()


def test_systemd_service_execstart_promotes_stripping_bin_venv():
    # No WorkingDirectory; the app dir is inferred from the ExecStart binary path,
    # walking past the venv/bin wrappers.
    pd = _pd()
    asset = _unit_asset(pd, "mdb-discovery.service", {
        "FragmentPath": "/etc/systemd/system/mdb-discovery.service",
        "WorkingDirectory": "",
        "ExecStart": "{ path=/home/data/project/mdb-discovery/venv/bin/python ; argv[]=... }",
    })
    assert asset["project"] == "mdb-discovery"
    assert pd.project_paths().get("mdb-discovery") == "/home/data/project/mdb-discovery"


def test_multi_service_app_collapses_to_one_project(monkeypatch):
    # The real UAT case: two units for one app, one running from the app root and one
    # from a subdir. The pre-pass must promote the shallowest dir so BOTH attribute to
    # a single `mdb-discovery` — no bogus `backend` project.
    pd = _pd()
    shows = {
        "mdb-discovery-proxy.service": {
            "FragmentPath": "/etc/systemd/system/mdb-discovery-proxy.service",
            "WorkingDirectory": "/home/data/project/mdb-discovery",
            "ExecStart": "{ path=/home/msinha/.local/bin/uvicorn ; argv[]=... }",
        },
        "mdb-discovery-backend.service": {
            "FragmentPath": "/etc/systemd/system/mdb-discovery-backend.service",
            "WorkingDirectory": "/home/data/project/mdb-discovery/backend",
            "ExecStart": "{ path=/home/data/project/mdb-discovery/backend/.venv/bin/uvicorn ; argv[]=... }",
        },
    }
    sc = SystemdScanner(server_id="oci", project_detector=pd)
    sc._show_cache = {}
    sc._is_enabled = lambda n: True
    monkeypatch.setattr(sc, "_list_unit_file_names",
                        lambda t: list(shows) if t == "service" else [])
    monkeypatch.setattr(sc, "_systemctl_show", lambda n: shows.get(n, {}))
    # run only the promotion pre-pass: stub the shared command runner so the real
    # `systemctl list-units` augmentation is skipped (returning None is its documented
    # failure/best-effort contract) and only the patched fakes above drive the pass.
    monkeypatch.setattr(sc, "run_cmd", lambda *a, **k: None)
    sc._promote_service_dirs()

    a_proxy = sc._build_unit_asset("service", "systemd_service", "mdb-discovery-proxy.service",
                                   load_state="loaded", active_state="active", sub_state="running",
                                   show=shows["mdb-discovery-proxy.service"])
    a_back = sc._build_unit_asset("service", "systemd_service", "mdb-discovery-backend.service",
                                  load_state="loaded", active_state="active", sub_state="running",
                                  show=shows["mdb-discovery-backend.service"])
    assert a_proxy["project"] == "mdb-discovery"
    assert a_back["project"] == "mdb-discovery"          # NOT "backend"
    assert "backend" not in pd.list_projects()
    assert pd.project_paths()["mdb-discovery"] == "/home/data/project/mdb-discovery"


def test_promoted_service_dir_becomes_a_correlator_bucket():
    pd = _pd()
    asset = _unit_asset(pd, "mdb-discovery.service", {
        "FragmentPath": "/etc/systemd/system/mdb-discovery.service",
        "WorkingDirectory": "/home/data/project/mdb-discovery", "ExecStart": "",
    })
    apps = {a["name"]: a for a in correlate(
        [asset], server_id="oci", projects_root="/nonexistent",
        project_dirs=pd.project_paths(),   # includes the just-registered dir
    )}
    assert "mdb-discovery" in apps
    assert apps["mdb-discovery"]["source"] == "/home/data/project/mdb-discovery"
    assert "mdb-discovery.service" in apps["mdb-discovery"]["systemd_units"]
