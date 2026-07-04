"""Storage registry (Phase 7C).

Unifies four kinds of storage entities into one collection so every byte
on disk can be traced to either a project bucket or System:

  - mount         — every df-listed filesystem (with byte-exact totals)
  - docker_volume — named docker volumes (mountpoint + du-walked size)
  - project_tree  — each ~/projects/<name> directory (du-walked size)
  - bind_mount    — every container bind mount source path (du-walked size)

Owner attribution: mounts inherit project from the path under
projects_root (already done by the storage scanner via ProjectDetector);
docker volumes inherit from compose label / using container; project
trees are always project; bind mounts are bucketed by the path or by
the using container's app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from app.scanners.docker import _dir_size_bytes


# Bind mounts of host system paths (the classic monitoring-stack pattern, e.g.
# node-exporter/cAdvisor mounting `/`, `/proc`, `/rootfs`) are NOT app data — and
# du-walking them yields meaningless host-wide totals. We still record the mount,
# but don't attribute a size to it.
_UNSIZABLE_BIND_EXACT = frozenset({
    "/", "/etc", "/usr", "/var", "/var/lib/docker", "/host", "/hostfs", "/rootfs",
})
_UNSIZABLE_BIND_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/var/run", "/boot")


def _is_unsizable_bind(src: str) -> bool:
    """True for host system-path bind mounts that must not be du-walked."""
    if not src:
        return True
    p = os.path.normpath(src)
    if p in _UNSIZABLE_BIND_EXACT:
        return True
    return any(p == pre or p.startswith(pre + "/") for pre in _UNSIZABLE_BIND_PREFIXES)


def _new_row(
    *,
    kind: str,
    storage_id: str,
    name: str,
    path: Optional[str],
    owner_project: str,
    server_id: str,
) -> Dict[str, Any]:
    return {
        "storage_id": storage_id,
        "kind": kind,
        "name": name,
        "path": path,
        "owner_project": owner_project,
        "owner_app_id": f"{server_id}:app:{owner_project}",
        "size_bytes": 0,
        "total_bytes": None,
        "used_bytes": None,
        "free_bytes": None,
        "fstype": None,
        "device": None,
        "usage_percent": None,
        "evidence_sources": [],
    }


def _container_app_name(
    container_asset: Dict[str, Any], valid_projects: Set[str]
) -> str:
    """Match the correlator's container routing for owner attribution."""
    meta = container_asset["metadata"]
    compose_proj = meta.get("compose_project")
    if compose_proj and compose_proj in valid_projects and compose_proj != "System":
        return compose_proj
    proj = container_asset.get("project")
    if proj and proj in valid_projects and proj != "System":
        return proj
    return "System"


def _mount_owner(
    target: str, bind_to_app: Dict[str, str], valid_projects: Set[str]
) -> str:
    """Best app owner for a whole-disk mount (e.g. /mnt/immich_data) that the path-under-
    projects_root rule leaves as System. Two signals, strongest first:
      1) a CONTAINER of some app bind-mounts a path at or under this mount → that app owns it
         (e.g. the immich container mounts /mnt/immich_data);
      2) the mount path contains an app name as a token (immich_data, backup_immich →
         'immich'; backup_nextcloud → 'nextcloud') — this is what catches dedicated data /
         backup drives that no container mounts. Longest name wins to avoid partials.
    Falls back to System. Never changes the byte total — only who the bytes are attributed to."""
    tnorm = os.path.normpath(target or "")
    if not tnorm or tnorm == "/":
        return "System"
    # (1) container bind under this mount
    for src, app in bind_to_app.items():
        if app not in valid_projects or app == "System":
            continue
        s = os.path.normpath(src)
        if s == tnorm or s.startswith(tnorm.rstrip("/") + "/"):
            return app
    # (2) app-name token in the mount path
    low = target.lower()
    best: Optional[str] = None
    for p in valid_projects:
        if p == "System" or len(p) < 3:
            continue
        if re.search(r"(?:^|[^a-z0-9])" + re.escape(p.lower()) + r"(?:[^a-z0-9]|$)", low):
            if best is None or len(p) > len(best):
                best = p
    return best or "System"


def _path_owner(path: str, valid_projects: Set[str], projects_root: str) -> str:
    """Return the owning project if `path` is under projects_root/<X>, else System."""
    if not path:
        return "System"
    try:
        rel = Path(path).resolve().relative_to(Path(projects_root))
        parts = rel.parts
        if parts and parts[0] in valid_projects and parts[0] != "System":
            return parts[0]
    except (ValueError, OSError):
        pass
    return "System"


def build_storage_registry(
    assets: List[Dict[str, Any]],
    *,
    server_id: str,
    projects_root: str,
    valid_projects: Iterable[str],
) -> List[Dict[str, Any]]:
    """Produce one storage row per (kind, path-or-name)."""
    valid = set(valid_projects)
    rows: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}

    def _commit(row: Dict[str, Any]) -> None:
        if row["storage_id"] in by_id:
            existing = by_id[row["storage_id"]]
            for ev in row["evidence_sources"]:
                if ev not in existing["evidence_sources"]:
                    existing["evidence_sources"].append(ev)
            # Prefer non-null totals/sizes if the duplicate has them.
            for k in ("size_bytes", "total_bytes", "used_bytes", "free_bytes",
                      "fstype", "device", "usage_percent"):
                if existing.get(k) in (None, 0) and row.get(k) not in (None, 0):
                    existing[k] = row[k]
            return
        by_id[row["storage_id"]] = row
        rows.append(row)

    # Map every container bind-source -> its app, so a whole-disk mount can be attributed to
    # the app that actually stores data on it (e.g. /mnt/immich_data -> immich).
    bind_to_app: Dict[str, str] = {}
    for c in assets:
        if c.get("category") != "docker_container":
            continue
        app_name = _container_app_name(c, valid)
        if app_name == "System":
            continue
        for src in c["metadata"].get("bind_mount_sources") or []:
            if src:
                bind_to_app.setdefault(os.path.normpath(src), app_name)

    # ---- mounts -----------------------------------------------------------
    for m in assets:
        if m.get("category") != "storage_mount":
            continue
        meta = m["metadata"]
        target = m["name"]
        owner = m.get("project") or "System"
        if owner not in valid:
            owner = "System"
        # A whole-disk mount left as System by the path-under-projects_root rule: try to
        # attribute it to the app that stores data there (container bind, or name-in-path).
        if owner == "System":
            owner = _mount_owner(target, bind_to_app, valid)
        row = _new_row(
            kind="mount",
            storage_id=f"{server_id}:storage:mount:{target}",
            name=target,
            path=target,
            owner_project=owner,
            server_id=server_id,
        )
        row["size_bytes"] = meta.get("used_bytes") or 0
        row["total_bytes"] = meta.get("total_bytes")
        row["used_bytes"] = meta.get("used_bytes")
        row["free_bytes"] = meta.get("free_bytes")
        row["fstype"] = meta.get("fstype")
        row["device"] = meta.get("source")
        row["usage_percent"] = meta.get("usage_percent")
        row["evidence_sources"].append({"kind": "df", "source": target})
        _commit(row)

    # ---- docker volumes ---------------------------------------------------
    for v in assets:
        if v.get("category") != "docker_volume":
            continue
        meta = v["metadata"]
        owner = v.get("project") or "System"
        if owner not in valid:
            owner = "System"
        row = _new_row(
            kind="docker_volume",
            storage_id=f"{server_id}:storage:volume:{v['name']}",
            name=v["name"],
            path=meta.get("mountpoint"),
            owner_project=owner,
            server_id=server_id,
        )
        row["size_bytes"] = meta.get("size_bytes") or 0
        row["evidence_sources"].append({"kind": "docker_volume", "source": v["name"]})
        _commit(row)

    # ---- project trees: one row per ~/projects/<name> ---------------------
    root = Path(projects_root)
    if root.exists():
        for proj_dir in sorted(root.iterdir()):
            if not proj_dir.is_dir() or proj_dir.name.startswith("."):
                continue
            name = proj_dir.name
            if name not in valid:
                continue
            row = _new_row(
                kind="project_tree",
                storage_id=f"{server_id}:storage:tree:{name}",
                name=name,
                path=str(proj_dir),
                owner_project=name,
                server_id=server_id,
            )
            row["size_bytes"] = _dir_size_bytes(str(proj_dir))
            row["evidence_sources"].append({"kind": "du", "source": str(proj_dir)})
            _commit(row)

    # ---- bind mounts from containers --------------------------------------
    for c in assets:
        if c.get("category") != "docker_container":
            continue
        app_name = _container_app_name(c, valid)
        for src in c["metadata"].get("bind_mount_sources") or []:
            if not src:
                continue
            # Prefer path-based ownership; fall back to using-container's app.
            path_owner = _path_owner(src, valid, projects_root)
            owner = path_owner if path_owner != "System" else app_name
            row = _new_row(
                kind="bind_mount",
                storage_id=f"{server_id}:storage:bind:{src}",
                name=src,
                path=src,
                owner_project=owner,
                server_id=server_id,
            )
            if _is_unsizable_bind(src):
                # System/observability mount — record it, but don't size the host.
                row["size_bytes"] = 0
                row["evidence_sources"].append(
                    {"kind": "container_bind", "source": c["name"], "note": "system_mount_unsized"}
                )
            else:
                row["size_bytes"] = _dir_size_bytes(src)
                row["evidence_sources"].append(
                    {"kind": "container_bind", "source": c["name"]}
                )
            _commit(row)

    rows.sort(key=lambda r: (r["kind"], r["owner_project"], r["name"]))
    return rows
