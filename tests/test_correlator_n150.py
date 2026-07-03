"""Correlator resilience + N150-shaped rollup (the zero-applications bug).

N150 (home /home/manishkumarsinha, apps outside ~/projects, cloudflared/systemd/compose)
scanned 492 assets but rolled up 0 applications. Two causes: the pre-fix detector yielded empty
project_dirs on the non-standard home (fixed by the full-disk detector on main), AND a single
malformed asset could raise out of a correlation pass and abort the WHOLE rollup. These lock in:
apps surface regardless of home dir; one bad asset logs a named reason and is skipped (rollup not
zeroed); ports/storage-by-project populate once apps correlate.
"""

import logging

from app.correlator import correlate
from app.ports_registry import build_ports_registry
from app.storage_registry import build_storage_registry

SYSTEM = "System"
HOME = "/home/manishkumarsinha"


def _by_name(apps):
    return {a["name"]: a for a in apps}


def _container(name, project, compose_project=None):
    return {"category": "docker_container", "name": name, "project": project,
            "metadata": {"running": True, "compose_project": compose_project,
                         "image": f"{project}/img", "restart_policy": {"Name": "always"},
                         "ports": [], "mounts": [], "environment_keys": []}}


def _compose(name, dir_name, project):
    return {"category": "docker_compose", "name": name, "project": project,
            "metadata": {"file_path": f"{HOME}/{dir_name}/docker-compose.yml", "services": [dir_name]}}


def _systemd(name, project, exec_start=""):
    return {"category": "systemd_service", "name": name, "project": project,
            "metadata": {"unit_type": "service", "exec_start": exec_start, "environment_keys": []}}


def _cloudflared(name, project=SYSTEM):
    return {"category": "cloudflare_tunnel", "name": name, "project": project, "metadata": {}}


def _port(port, project=SYSTEM):
    return {"category": "network_port", "name": f"{port}/tcp", "project": project,
            "metadata": {"port": port, "protocol": "tcp", "process": "x", "pid": 1}}


def _mount(path, project=SYSTEM):
    return {"category": "storage_mount", "name": path, "project": project,
            "metadata": {"mountpoint": path, "kind": "mount"}}


def test_correlator_non_empty_from_n150_like_asset_mix():
    pdirs = {"nextcloud": "/opt/nextcloud", "immich": "/opt/immich", "adguard": "/opt/adguard"}
    assets = [
        _compose("nextcloud", "nextcloud", "nextcloud"),
        _container("nextcloud-app-1", "nextcloud", compose_project="nextcloud"),
        _container("immich_server", "immich", compose_project="immich"),
        _systemd("adguardhome.service", "adguard", exec_start="/opt/adguard/AdGuardHome"),
        _cloudflared("photo.example.site"),
        _systemd("wpa_supplicant@.service", SYSTEM),
        _port(53, "adguard"), _port(9100, SYSTEM),
        _mount("/mnt/backup_nextcloud", SYSTEM),
    ]
    apps = _by_name(correlate(assets, server_id="N150",
                              projects_root=f"{HOME}/projects", project_dirs=pdirs))
    project_apps = {n for n, a in apps.items() if a["type"] == "project"}
    assert {"nextcloud", "immich", "adguard"} <= project_apps
    assert "nextcloud-app-1" in apps["nextcloud"]["containers"]
    assert len(project_apps) >= 3


def test_single_unparseable_asset_logs_named_reason_and_does_not_zero_rollup(caplog):
    pdirs = {"nextcloud": "/opt/nextcloud"}
    bad = {"category": "docker_compose", "name": "broken-compose"}   # no metadata -> raises in Pass 1
    assets = [
        _compose("nextcloud", "nextcloud", "nextcloud"),
        bad,
        _container("nextcloud-app-1", "nextcloud", compose_project="nextcloud"),
    ]
    with caplog.at_level(logging.ERROR):
        apps = _by_name(correlate(assets, server_id="N150", projects_root="/nope", project_dirs=pdirs))
    assert any("broken-compose" in r.getMessage() and "skipping asset" in r.getMessage()
               for r in caplog.records)
    assert "nextcloud" in apps and apps["nextcloud"]["type"] == "project"
    assert "nextcloud-app-1" in apps["nextcloud"]["containers"]


def test_ports_and_storage_by_project_populate_when_apps_correlate():
    pdirs = {"adguard": "/opt/adguard", "nextcloud": "/opt/nextcloud"}
    assets = [
        _container("nextcloud-app-1", "nextcloud", compose_project="nextcloud"),
        _port(53, "adguard"), _port(443, "nextcloud"),
        _mount("/mnt/backup_nextcloud", "nextcloud"),
    ]
    apps = _by_name(correlate(assets, server_id="N150", projects_root="/nope", project_dirs=pdirs))
    valid = list(apps.keys())
    ports = build_ports_registry(assets, server_id="N150", valid_projects=valid)
    storage = build_storage_registry(assets, server_id="N150", projects_root="/nope", valid_projects=valid)
    assert len(ports) > 0 and any(("adguard" in str(r)) or ("nextcloud" in str(r)) for r in ports)
    assert len(storage) > 0 and any("nextcloud" in str(r) for r in storage)


def test_non_msinha_home_buckets_apps_correctly():
    pdirs = {"immich": f"{HOME}/immich", "pihole": f"{HOME}/pihole"}
    apps = _by_name(correlate([_container("immich_server", "immich")],
                              server_id="N150", projects_root=f"{HOME}/projects", project_dirs=pdirs))
    assert "immich" in apps and apps["immich"]["source"] == f"{HOME}/immich"
    assert "pihole" in apps and apps["pihole"]["type"] == "project"
    assert "immich_server" in apps["immich"]["containers"]
