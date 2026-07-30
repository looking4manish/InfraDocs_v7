"""Scan endpoints — list history, trigger a new scan (background)."""

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends
from pymongo import DESCENDING

from app.api.dependencies import get_config, get_db, require_operator, verify_auth
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
    degraded = []   # ran but hit per-item errors (partial_success) — surfaced, not hidden

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
        elif result["status"] != "success":
            degraded.append(result["scanner"])

    # Correlation pass
    applications = correlate(
        all_assets,
        server_id=cfg.server.id,
        projects_root=cfg.paths.projects_root,
        direct_roots=cfg.paths.direct_roots,
        project_dirs=pd.project_paths(),  # host-aware, all configured roots
    )
    attach_root_paths(applications, pd.project_paths())
    apps_written = db.replace_applications(applications)

    # Ports + storage registries — the UI "Scan now" was leaving these stale
    # (only the CLI agent built them), so storage/ports summaries read 0.
    valid_projects = pd.list_projects() + [SYSTEM_BUCKET]
    db.replace_ports(build_ports_registry(
        all_assets, server_id=cfg.server.id, valid_projects=valid_projects,
    ))
    db.replace_storage(build_storage_registry(
        all_assets, server_id=cfg.server.id,
        projects_root=cfg.paths.projects_root, valid_projects=valid_projects,
    ))

    # Data sync: a non-primary pushes its scan to the CURRENT leader (direct mesh).
    # The target follows gossip — whichever node currently serves as primary — so after
    # a failover the push retargets automatically. Missing a few rounds is fine (scan
    # data is re-derivable). No shared DB; reuses the direct /ingest endpoint.
    self_doc = db.db.cluster.find_one({"_id": "self"}) or {}
    fed = db.db.settings.find_one({"_id": "app"}) or {}
    if not self_doc.get("is_primary") and fed.get("join_token"):
        from app import cluster as _cluster
        roster = {n["node_id"]: n for n in db.db.cluster_nodes.find({}, {"_id": 0})}
        target = _cluster.current_leader_address(roster) or fed.get("primary_url")
        if target:
            try:
                from app import federation as _federation
                _federation.push_to_primary(
                    target, fed["join_token"], cfg.server.id, all_assets, applications,
                )
            except Exception:
                pass  # never fail a scan because the leader is unreachable

    finished = datetime.now(timezone.utc)
    db.db.scan_logs.update_one(
        {"scan_id": scan_id},
        {
            "$set": {
                "status": "failed" if failed else ("degraded" if degraded else "success"),
                "finished_at": finished,
                "duration_seconds": (finished - started).total_seconds(),
                "total_assets": len(all_assets),
                "applications_built": apps_written,
                "scanners": per_scanner,
                "failed_scanners": failed,
                "degraded_scanners": degraded,
            }
        },
    )
    db.close()


@router.post("/trigger", status_code=202)
def trigger_scan(
    background: BackgroundTasks,
    cfg: Config = Depends(get_config),
    _: str = Depends(require_operator),
):
    scan_id = uuid4().hex
    background.add_task(_run_scan_job, scan_id, cfg)
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
