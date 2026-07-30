"""InfraDocs V6 API entry point."""

import asyncio  # noqa: E402
import os  # noqa: E402
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Load .env from project root before any other module touches os.environ.
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)

from app import auth as _auth  # noqa: E402
from app import cluster as CC  # noqa: E402
from app import cluster_manager as _cluster  # noqa: E402
from app.api.dependencies import get_config, get_db  # noqa: E402
from app.api.routers import (  # noqa: E402
    actions, ai, applications, assets, auth, cluster, endpoints, federation, fleet, health, ports, projects, scans, setup, storage,
)
from app.core.logger import setup_logger  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = setup_logger("api", log_file="logs/api.log", level="INFO")
    cfg = get_config()
    db = get_db()
    db.create_indexes()
    _auth.seed_default_admin(db, cfg.auth.username)
    logger.info(f"API started for server '{cfg.server.id}'")
    app.state.gossip_task = None
    app.state.cluster_logger = logger
    # Effective flag = config default OR a persisted runtime override, so an operator's
    # in-UI enable survives a restart. The Admin tab starts/stops this task live.
    if _cluster.is_enabled(cfg, db):
        _cluster.start_gossip(app, cfg, db, logger)
    try:
        yield
    finally:
        _cluster.stop_gossip(app, logger)
        db.close()
        logger.info("API shutdown complete")


app = FastAPI(
    title="InfraDocs V6 API",
    version="0.1.0",
    description="Single-host (OCI) infrastructure documentation API.",
    lifespan=lifespan,
)

# CORS — in prod the frontend is served SAME-ORIGIN via the nginx proxy, so no
# cross-origin grant is needed at all. The only cross-origin caller is the dev vite
# server. `allow_origins=["*"]` WITH `allow_credentials=True` is both unsafe (any site
# could drive the authed API from a victim's browser) and silently ignored by browsers
# (the spec forbids that combination), so pin an explicit allow-list. Override with
# INFRADOCS_CORS_ORIGINS (comma-separated) when a real remote origin is needed.
def _cors_origins() -> list:
    raw = os.environ.get("INFRADOCS_CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    # Default: local dev only (vite). Prod is same-origin, so this grant is unused there.
    return [
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:3000", "http://127.0.0.1:3000",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(setup.router, prefix="/api/setup", tags=["setup"])
app.include_router(endpoints.router, prefix="/api/endpoints", tags=["endpoints"])
app.include_router(ai.router, prefix="/api/ai", tags=["ai"])
app.include_router(federation.router, prefix="/api/federation", tags=["federation"])
app.include_router(cluster.router, prefix="/api/cluster", tags=["cluster"])
app.include_router(fleet.router, prefix="/api/fleet", tags=["fleet"])
app.include_router(assets.router, prefix="/api/assets", tags=["assets"])
app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
app.include_router(applications.router, prefix="/api/applications", tags=["applications"])
app.include_router(ports.router, prefix="/api/ports", tags=["ports"])
app.include_router(storage.router, prefix="/api/storage", tags=["storage"])
app.include_router(scans.router, prefix="/api/scans", tags=["scans"])
# actions router declares its own /assets/{id}/action and /applications/{name}/action
# paths plus /actions/* — mount it at /api so all three sit at the right URLs.
app.include_router(actions.router, prefix="/api", tags=["actions"])


@app.get("/")
def root():
    """App-level leader redirect (no VIP/keepalived — VRRP can't cross the tailnet L3
    overlay). The primary serves the app; a non-primary serves a lightweight page that
    names the current primary and 302-redirects to it. The target follows gossip, so
    after a failover secondaries redirect to the NEW primary automatically."""
    from fastapi.responses import HTMLResponse

    db = get_db()
    s = db.db.cluster.find_one({"_id": "self"}) or {}
    if s.get("is_primary") or not s:  # primary (or un-clustered single host) serves normally
        return {"name": "InfraDocs V6", "version": "0.1.0"}
    leader_addr = CC.current_leader_address({n["node_id"]: n for n in db.db.cluster_nodes.find({}, {"_id": 0})})
    leader_id = (db.db.settings.find_one({"_id": "app"}) or {}).get("primary_node")
    if leader_addr:
        body = (f"<html><body style='font-family:sans-serif;background:#0a0e14;color:#e5e7eb'>"
                f"<p>This node is not the cluster primary. The primary is "
                f"<b>{leader_id or 'unknown'}</b>.</p>"
                f"<p>Redirecting to <a href='{leader_addr}'>{leader_addr}</a>…</p></body></html>")
        return HTMLResponse(content=body, status_code=302, headers={"Location": leader_addr})
    return HTMLResponse(
        content="<html><body>This node is a cluster secondary and no primary is currently "
                "reachable. Try again shortly or promote a node.</body></html>",
        status_code=503,
    )
