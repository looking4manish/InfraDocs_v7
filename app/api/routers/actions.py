"""Operational action endpoints (Phase 8)."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.actions import (
    ActionError,
    ActionNotAllowed,
    ActionResult,
    SelfActionRefused,
    dispatch,
)
from app.api.dependencies import get_db, require_operator, verify_auth
from app.core.db_manager import DBManager

router = APIRouter()


class ActionRequest(BaseModel):
    action: str = Field(..., description="e.g. start, stop, restart, logs")
    args: Dict[str, Any] = Field(default_factory=dict)


def _record(
    db: DBManager,
    *,
    actor: str,
    asset: Dict[str, Any],
    action: str,
    args: Dict[str, Any],
    result: ActionResult,
    refused_reason: Optional[str] = None,
) -> None:
    db.record_action(
        {
            "actor": actor,
            "asset_id": asset.get("asset_id"),
            "asset_name": asset.get("name"),
            "category": asset.get("category"),
            "project": asset.get("project"),
            "action": action,
            "args": args,
            "status": result.status,
            "return_code": result.return_code,
            "stdout": result.stdout[-4000:] if result.stdout else "",
            "stderr": result.stderr[-4000:] if result.stderr else "",
            "duration_ms": result.duration_ms,
            "refused_reason": refused_reason,
        }
    )


@router.post("/assets/{asset_id}/action")
def asset_action(
    asset_id: str,
    req: ActionRequest = Body(...),
    db: DBManager = Depends(get_db),
    actor: str = Depends(require_operator),
):
    asset = db.db.assets.find_one({"asset_id": asset_id})
    if not asset:
        raise HTTPException(status_code=404, detail="asset not found")
    asset["_id"] = str(asset["_id"])

    try:
        result = dispatch(asset, req.action, req.args)
    except SelfActionRefused as e:
        _record(
            db, actor=actor, asset=asset, action=req.action, args=req.args,
            result=ActionResult(status="failed", stderr=str(e)),
            refused_reason="self_protect",
        )
        raise HTTPException(status_code=409, detail=str(e))
    except ActionNotAllowed as e:
        _record(
            db, actor=actor, asset=asset, action=req.action, args=req.args,
            result=ActionResult(status="failed", stderr=str(e)),
            refused_reason="not_allowed",
        )
        raise HTTPException(status_code=403, detail=str(e))

    _record(
        db, actor=actor, asset=asset, action=req.action, args=req.args,
        result=result,
    )
    return {
        "asset_id": asset["asset_id"],
        "action": req.action,
        "status": result.status,
        "return_code": result.return_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "details": result.details,
    }


@router.post("/applications/{name}/action")
def application_action(
    name: str,
    req: ActionRequest = Body(...),
    db: DBManager = Depends(get_db),
    actor: str = Depends(require_operator),
):
    """Fire an action against every container + systemd unit in an app."""
    app_doc = db.get_application(name)
    if not app_doc:
        raise HTTPException(status_code=404, detail="application not found")

    targets = []
    # Resolve containers by name (each container's asset has its name)
    for cname in app_doc.get("containers", []):
        ca = db.db.assets.find_one({"category": "docker_container", "name": cname})
        if ca:
            ca["_id"] = str(ca["_id"])
            targets.append(ca)
    # Resolve systemd units (services + timers) by name
    for uname in app_doc.get("systemd_units", []):
        sa = db.db.assets.find_one({
            "category": {"$in": ["systemd_service", "systemd_timer"]},
            "name": uname,
        })
        if sa:
            sa["_id"] = str(sa["_id"])
            targets.append(sa)

    if not targets:
        raise HTTPException(
            status_code=400,
            detail=f"application '{name}' has no actionable assets",
        )

    results = []
    for asset in targets:
        try:
            result = dispatch(asset, req.action, req.args)
            status = result.status
            err = None
        except SelfActionRefused as e:
            result = ActionResult(status="failed", stderr=str(e))
            status = "refused"
            err = "self_protect"
        except ActionNotAllowed as e:
            # Action might be valid for some categories but not others (e.g.,
            # `up` on a docker_container). Just skip with a marker.
            result = ActionResult(status="failed", stderr=str(e))
            status = "skipped"
            err = "not_allowed"

        _record(
            db, actor=actor, asset=asset, action=req.action, args=req.args,
            result=result, refused_reason=err,
        )
        results.append(
            {
                "asset_id": asset["asset_id"],
                "asset_name": asset["name"],
                "category": asset["category"],
                "status": status,
                "return_code": result.return_code,
                "stdout_tail": result.stdout[-500:] if result.stdout else "",
                "stderr_tail": result.stderr[-500:] if result.stderr else "",
                "duration_ms": result.duration_ms,
            }
        )

    return {
        "application": name,
        "action": req.action,
        "targets": len(results),
        "results": results,
    }


@router.get("/actions/")
def list_actions(
    asset_id: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    actor: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: DBManager = Depends(get_db),
    _: str = Depends(verify_auth),
):
    rows = db.get_actions(asset_id=asset_id, action=action, actor=actor, limit=limit)
    return {"count": len(rows), "actions": rows}


@router.get("/actions/allowed")
def list_allowed(_: str = Depends(verify_auth)):
    """Return the per-category action allow-list for UI to drive button state."""
    from app.actions import ALLOWED_ACTIONS, DESTRUCTIVE_ACTIONS

    return {
        "allowed": {k: sorted(v) for k, v in ALLOWED_ACTIONS.items()},
        "destructive": {k: sorted(v) for k, v in DESTRUCTIVE_ACTIONS.items()},
    }
