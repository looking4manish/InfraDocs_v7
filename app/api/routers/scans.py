"""Scan endpoints — list history, trigger a new scan (background)."""

import threading
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends
from pymongo import DESCENDING

from app.api.dependencies import get_config, get_db, verify_auth
from app.core.config_loader import Config
from app.core.db_manager import DBManager
from app.core.project_detector import (
    ProjectDetector, attach_root_paths, discover_docker_projects,
)
from app.core.logger import get_scan_logger
from app.correlator import SYSTEM_BUCKET, correlate
from app.ports_registry import build_ports_registry
from app.scanners.registry import SCANNERS
from app.storage_registry import build_storage_registry

router = APIRouter()


def _run_scan_job(scan_id: str, cfg: Config):
    """Background worker: runs all enabled scanners and writes results.

    Stores a `scan_logs` doc keyed by `scan_id` so the API can report status.
    """
    db = DBManager(uri=cfg.mongodb.uri, database=cfg.mongodb.database)
    started = datetime.now(timezone.utc)

    # Mark started
    db.db.scan_logs.update_one(
        {"scan_id": scan_id},
        {
            "$set": {
                "scan_id": scan_id,
                "status": "running",
                "started_at": started,
            }
        },
        upsert=True,
    )

    pd = ProjectDetector(
        projects_root=cfg.paths.projects_root,
        scan_roots=cfg.paths.scan_roots,
        direct_roots=cfg.paths.direct_roots,
        scan_depth=cfg.paths.scan_depth,
        scan_timeout_seconds=cfg.paths.scan_timeout_seconds,
        exclude_paths=cfg.paths.scan_exclusions,
        discovered=discover_docker_projects(),
        logger=get_scan_logger("api"),
    )
    per_scanner = []
    all_assets = []
    failed = []

    for name in cfg.scanning.enabled_scanners:
        cls = SCANNERS.get(name)
        if not cls:
            continue
        sc = cls(server_id=cfg.server.id, project_detector=pd)
        result = sc.execute()
        per_scanner.append(
            {
                "scanner": result["scanner"],
                "status": result["status"],
                "assets_found": result["assets_found"],
                "duration_seconds": result["duration_seconds"],
                "errors": result["errors"],
            }
        )
        for asset in result["assets"]:
            db.upsert_asset(asset)
            all_assets.append(asset)
        if result["status"] == "failed":
            failed.append(result["scanner"])

    # Correlation pass
    applications = correlate(
        all_assets,
        server_id=cfg.server.id,
        projects_root=cfg.paths.projects_root,
        direct_roots=cfg.paths.direct_roots,
        project_dirs=pd.project_paths(),  # host-aware, all configured roots
    )
    attach_root_paths(applications, pd.project_paths())
    apps_written = db.replace_applications(applications, server_id=cfg.server.id)
    # Assets that disappeared since the last scan of THIS server are removed; other servers'
    # assets (pushed to the primary) are left alone.
    seen = [a.get("asset_id") for a in all_assets if a.get("asset_id")]
    db.db.assets.delete_many({"server_id": cfg.server.id, "asset_id": {"$nin": seen}})

    # Ports + storage registries — the UI "Scan now" was leaving these stale
    # (only the CLI agent built them), so storage/ports summaries read 0.
    valid_projects = pd.list_projects() + [SYSTEM_BUCKET]
    db.replace_ports(build_ports_registry(
        all_assets, server_id=cfg.server.id, valid_projects=valid_projects,
    ), server_id=cfg.server.id)
    db.replace_storage(build_storage_registry(
        all_assets, server_id=cfg.server.id,
        projects_root=cfg.paths.projects_root, valid_projects=valid_projects,
    ), server_id=cfg.server.id)

    # Data sync: a secondary pushes its scan to whichever member is primary now. After a
    # failover the target follows the election. Missing a push is fine: the next scan repeats it.
    push_to_leader(cfg, all_assets, applications)

    finished = datetime.now(timezone.utc)
    db.db.scan_logs.update_one(
        {"scan_id": scan_id},
        {
            "$set": {
                "status": "failed" if failed else "success",
                "finished_at": finished,
                "duration_seconds": (finished - started).total_seconds(),
                "total_assets": len(all_assets),
                "applications_built": apps_written,
                "scanners": per_scanner,
                "failed_scanners": failed,
            }
        },
    )
    db.close()


def push_to_leader(cfg: Config, assets, applications) -> bool:
    import logging
    from app import cluster_runtime as RT
    n = RT.node()
    if n is None or n.is_primary():
        return False
    target = RT.leader_address()
    if not target:
        return False
    try:
        from app import federation as _federation
        from app.fleet_auth import get_fleet_secret
        _db = DBManager(uri=cfg.mongodb.uri, database=cfg.mongodb.database)
        try:
            secret = get_fleet_secret(_db)
        finally:
            _db.close()
        _federation.push_to_primary(target, secret, cfg.server.id, assets, applications, term=RT.current_term())
        return True
    except Exception as e:  # noqa: BLE001 — never fail a scan because the primary is unreachable
        logging.getLogger("app.scan").warning("scan push to %s failed: %s", target, e)
        return False


_scan_lock = threading.Lock()


def run_scan_now(cfg: Optional[Config] = None) -> Optional[str]:
    """Run one scan synchronously unless one is already running. Returns the scan id, or None
    when another scan was in progress."""
    from app.api.dependencies import get_config as _gc
    if not _scan_lock.acquire(blocking=False):
        return None
    try:
        scan_id = uuid4().hex
        _run_scan_job(scan_id, cfg or _gc())
        return scan_id
    finally:
        _scan_lock.release()


@router.post("/trigger", status_code=202)
def trigger_scan(
    background: BackgroundTasks,
    cfg: Config = Depends(get_config),
    _: str = Depends(verify_auth),
):
    scan_id = uuid4().hex
    background.add_task(run_scan_now, cfg)
    return {"scan_id": scan_id, "status": "queued"}


@router.get("/")
def list_scans(
    limit: int = 25,
    db: DBManager = Depends(get_db),
    _: str = Depends(verify_auth),
):
    cursor = db.db.scan_logs.find({}).sort("started_at", DESCENDING).limit(limit)
    out = []
    for s in cursor:
        s["_id"] = str(s["_id"])
        out.append(s)
    return {"count": len(out), "scans": out}


@router.get("/{scan_id}")
def get_scan(
    scan_id: str,
    db: DBManager = Depends(get_db),
    _: str = Depends(verify_auth),
):
    s = db.db.scan_logs.find_one({"scan_id": scan_id})
    if not s:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="scan not found")
    s["_id"] = str(s["_id"])
    return s
