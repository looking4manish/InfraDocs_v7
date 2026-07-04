"""Phase 7C — storage registry tests."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.storage_registry import build_storage_registry


VALID = ["System", "openwebui", "OCI_Dashboard", "InfraDocs_V6"]


def _mount(target, *, source="/dev/sda1", fstype="ext4", used=10_000, total=100_000, project="System"):
    return {
        "category": "storage_mount",
        "asset_id": f"oci:mount:{target}",
        "name": target,
        "status": "mounted",
        "project": project,
        "metadata": {
            "source": source,
            "fstype": fstype,
            "size": "100G",
            "used": "10G",
            "available": "90G",
            "usage_percent": 10,
            "size_bytes": total,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": total - used,
        },
    }


def _volume(name, *, mountpoint=None, size=1024, project="System", compose_project=None):
    return {
        "category": "docker_volume",
        "asset_id": f"oci:volume:{name}",
        "name": name,
        "status": "in_use",
        "project": project,
        "metadata": {
            "mountpoint": mountpoint or f"/var/lib/docker/volumes/{name}/_data",
            "size_bytes": size,
            "compose_project": compose_project,
            "labels": {},
        },
    }


def _container(name, *, project="System", compose_project=None, bind_sources=()):
    return {
        "category": "docker_container",
        "asset_id": f"oci:container:{name}",
        "name": name,
        "status": "running",
        "project": project,
        "metadata": {
            "compose_project": compose_project,
            "bind_mount_sources": list(bind_sources),
        },
    }


def test_mount_produces_one_row_with_bytes(tmp_path):
    rows = build_storage_registry(
        [_mount("/data", used=50_000_000, total=200_000_000)],
        server_id="oci",
        projects_root=str(tmp_path),
        valid_projects=VALID,
    )
    mounts = [r for r in rows if r["kind"] == "mount"]
    assert len(mounts) == 1
    m = mounts[0]
    assert m["name"] == "/data"
    assert m["used_bytes"] == 50_000_000
    assert m["total_bytes"] == 200_000_000
    assert m["free_bytes"] == 150_000_000
    assert m["fstype"] == "ext4"
    assert m["owner_project"] == "System"


def test_mount_attributed_to_app_by_container_bind(tmp_path):
    # a container of app 'openwebui' stores data on /mnt/owui_data → that whole mount is theirs
    rows = build_storage_registry(
        [_mount("/mnt/owui_data", used=236_000_000_000, project="System"),
         _container("owui", project="openwebui", bind_sources=["/mnt/owui_data/uploads"])],
        server_id="oci", projects_root=str(tmp_path), valid_projects=VALID,
    )
    m = next(r for r in rows if r["kind"] == "mount")
    assert m["owner_project"] == "openwebui"          # not System
    assert m["used_bytes"] == 236_000_000_000         # byte total unchanged


def test_mount_attributed_to_app_by_name_in_path(tmp_path):
    # a dedicated data/backup drive no container mounts still maps by app-name token
    rows = build_storage_registry(
        [_mount("/mnt/openwebui_data", project="System"),
         _mount("/mnt/backup_openwebui", project="System")],
        server_id="oci", projects_root=str(tmp_path), valid_projects=VALID,
    )
    for r in [x for x in rows if x["kind"] == "mount"]:
        assert r["owner_project"] == "openwebui", r["name"]


def test_unrelated_mount_stays_system(tmp_path):
    rows = build_storage_registry(
        [_mount("/", project="System"), _mount("/boot/efi", project="System")],
        server_id="oci", projects_root=str(tmp_path), valid_projects=VALID,
    )
    assert all(r["owner_project"] == "System" for r in rows if r["kind"] == "mount")


def test_docker_volume_attributed_to_compose_project(tmp_path):
    rows = build_storage_registry(
        [_volume("openwebui_data", project="openwebui", size=1_000_000)],
        server_id="oci",
        projects_root=str(tmp_path),
        valid_projects=VALID,
    )
    vol = next(r for r in rows if r["kind"] == "docker_volume")
    assert vol["name"] == "openwebui_data"
    assert vol["size_bytes"] == 1_000_000
    assert vol["owner_project"] == "openwebui"


def test_project_trees_seeded_per_directory(tmp_path):
    """Every ~/projects/<name> on disk produces a project_tree row."""
    (tmp_path / "openwebui").mkdir()
    (tmp_path / "openwebui" / "f").write_bytes(b"x" * 5000)
    (tmp_path / "OCI_Dashboard").mkdir()
    (tmp_path / "OCI_Dashboard" / "f").write_bytes(b"y" * 2000)
    rows = build_storage_registry(
        [],
        server_id="oci",
        projects_root=str(tmp_path),
        valid_projects=VALID,
    )
    trees = {r["name"]: r for r in rows if r["kind"] == "project_tree"}
    assert "openwebui" in trees
    assert "OCI_Dashboard" in trees
    assert trees["openwebui"]["size_bytes"] >= 5000
    assert trees["openwebui"]["owner_project"] == "openwebui"


def test_bind_mount_under_project_root_attributes_to_project(tmp_path):
    """Container bind from /home/msinha/projects/openwebui/data → openwebui."""
    proj = tmp_path / "openwebui"
    proj.mkdir()
    bind_dir = proj / "data"
    bind_dir.mkdir()
    (bind_dir / "f").write_bytes(b"z" * 3000)
    rows = build_storage_registry(
        [_container("openwebui", project="openwebui", bind_sources=[str(bind_dir)])],
        server_id="oci",
        projects_root=str(tmp_path),
        valid_projects=VALID,
    )
    binds = [r for r in rows if r["kind"] == "bind_mount"]
    assert len(binds) == 1
    assert binds[0]["owner_project"] == "openwebui"
    assert binds[0]["size_bytes"] >= 3000


def test_system_bind_mounts_are_not_sized(tmp_path):
    """Monitoring stacks bind `/`, `/proc`, etc. — record them but never du-walk
    the host. Regression: a `/` bind was sized at ~1 PB via /proc/kcore's phantom
    apparent size, showing 'Storage 1016 TB' on the dashboard."""
    rows = build_storage_registry(
        [_container("node-exporter", project="db-observability",
                    bind_sources=["/", "/proc", "/var/run/docker.sock"])],
        server_id="oci",
        projects_root=str(tmp_path),
        valid_projects={"db-observability"},
    )
    binds = [r for r in rows if r["kind"] == "bind_mount"]
    assert binds, "system bind mounts should still be recorded"
    for b in binds:
        assert b["size_bytes"] == 0, f"system mount {b['path']} must not be sized"


def test_bind_mount_outside_project_root_uses_container_app(tmp_path):
    """A bind to /var/lib/postgres but container owned by openwebui → openwebui."""
    rows = build_storage_registry(
        [_container("openwebui-db", compose_project="openwebui",
                    bind_sources=["/var/lib/postgres"])],
        server_id="oci",
        projects_root=str(tmp_path),
        valid_projects=VALID,
    )
    binds = [r for r in rows if r["kind"] == "bind_mount"]
    assert binds[0]["owner_project"] == "openwebui"


def test_unknown_project_falls_back_to_system(tmp_path):
    """Mount tagged with a project that's not in the valid list → System."""
    rows = build_storage_registry(
        [_mount("/old/proj", project="DeletedProject")],
        server_id="oci",
        projects_root=str(tmp_path),
        valid_projects=VALID,
    )
    assert rows[0]["owner_project"] == "System"


def test_storage_id_uniqueness(tmp_path):
    """Two mounts on the same target collapse to one row, not two."""
    rows = build_storage_registry(
        [_mount("/data"), _mount("/data")],
        server_id="oci",
        projects_root=str(tmp_path),
        valid_projects=VALID,
    )
    mounts = [r for r in rows if r["kind"] == "mount"]
    assert len(mounts) == 1


def test_owner_app_id_format(tmp_path):
    rows = build_storage_registry(
        [_mount("/data", project="openwebui")],
        server_id="oci",
        projects_root=str(tmp_path),
        valid_projects=VALID,
    )
    assert rows[0]["owner_app_id"] == "oci:app:openwebui"


def test_kinds_present_in_mixed_input(tmp_path):
    """All four kinds appear when their evidence is present."""
    (tmp_path / "openwebui").mkdir()
    (tmp_path / "openwebui" / "f").write_bytes(b"x")
    assets = [
        _mount("/data"),
        _volume("v1"),
        _container("c1", bind_sources=["/etc/x"]),
    ]
    rows = build_storage_registry(
        assets, server_id="oci",
        projects_root=str(tmp_path),
        valid_projects=VALID,
    )
    kinds = {r["kind"] for r in rows}
    assert kinds == {"mount", "docker_volume", "project_tree", "bind_mount"}
