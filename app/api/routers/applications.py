"""Applications endpoints — the application-centric view."""

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.dependencies import get_config, get_db, require_operator, verify_auth
from app.blast_radius import compute_blast_radius
from app.core.config_loader import Config
from app.core.db_manager import DBManager
from app.teardown import TeardownRefused, build_plan, execute_plan

router = APIRouter()
_log = logging.getLogger("app.teardown")


class TeardownRequest(BaseModel):
    dry_run: bool = Field(True, description="True = preview only; nothing is removed")
    confirm: str = Field("", description="Must equal the project name to execute")


@router.get("/list")
def list_applications(
    internet_exposed: bool | None = Query(None),
    db: DBManager = Depends(get_db),
    _: str = Depends(verify_auth),
):
    query = {}
    if internet_exposed is not None:
        query["internet_exposed"] = internet_exposed
    apps = list(db.db.applications.find(query).sort("name", 1))
    for a in apps:
        a["_id"] = str(a["_id"])
    return {"count": len(apps), "applications": apps}


@router.get("/{name}/blast-radius")
def application_blast_radius(
    name: str,
    db: DBManager = Depends(get_db),
    _: str = Depends(verify_auth),
):
    """Read-only teardown preview: every asset a kill would touch, with
    data-loss and shared (must-not-remove) flags. Changes nothing."""
    app = db.get_application(name)
    if not app:
        raise HTTPException(status_code=404, detail="application not found")
    all_apps = list(db.db.applications.find({}))
    return compute_blast_radius(app, all_apps)


@router.post("/{name}/teardown")
def application_teardown(
    name: str,
    req: TeardownRequest = Body(...),
    db: DBManager = Depends(get_db),
    cfg: Config = Depends(get_config),
    actor: str = Depends(require_operator),
):
    """The Kill Button. dry_run=True (default) returns the ordered plan and removes
    nothing. dry_run=False requires confirm == project name and executes the
    teardown (backup-first; shared/protected assets skipped; every step audited)."""
    app = db.get_application(name)
    if not app:
        raise HTTPException(status_code=404, detail="application not found")
    all_apps = list(db.db.applications.find({}))
    plan = build_plan(app, all_apps, projects_root=cfg.paths.projects_root)

    if req.dry_run:
        return {"dry_run": True, **plan}

    if plan["refusals"]:
        raise HTTPException(status_code=409, detail="; ".join(plan["refusals"]))
    if req.confirm != name:
        raise HTTPException(
            status_code=400,
            detail=f"confirm must equal '{name}' to execute the teardown",
        )
    try:
        result = execute_plan(plan, db, actor, projects_root=cfg.paths.projects_root)
    except TeardownRefused as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # never let teardown die with an invisible 500 traceback
        _log.exception("teardown of %s crashed", name)
        raise HTTPException(status_code=500,
                            detail=f"teardown of '{name}' failed: {type(e).__name__}: {e}")
    return {"dry_run": False, **result}


@router.get("/{name}")
def get_application(
    name: str,
    db: DBManager = Depends(get_db),
    _: str = Depends(verify_auth),
):
    app = db.get_application(name)
    if not app:
        raise HTTPException(status_code=404, detail="application not found")
    return app
