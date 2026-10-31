"""InfraDocs V7 API entry point."""

import threading  # noqa: E402
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# Load .env from project root before any other module touches os.environ.
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)

from app import cluster_runtime as RT  # noqa: E402
from app.api.dependencies import get_config, get_db  # noqa: E402
from app.api.routers import (  # noqa: E402
    actions, ai, applications, assets, auth, cluster, endpoints, federation, fleet, health, ports, projects, scans, setup, storage,
)
from app.core.logger import setup_logger  # noqa: E402


def _scan_scheduler(cfg, stop: threading.Event, logger) -> None:
    """Every server scans itself every `scanning.interval_minutes`; a secondary pushes the
    result to the primary. Runs only once setup is complete."""
    from app.api.routers.scans import run_scan_now
    interval = max(1, int(cfg.scanning.interval_minutes)) * 60
    while not stop.wait(interval):
        try:
            db = get_db()
            if (db.db.settings.find_one({"_id": "app"}) or {}).get("setup_complete"):
                run_scan_now(cfg)
        except Exception as e:  # noqa: BLE001
            logger.warning("scheduled scan failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = setup_logger("api", log_file="logs/api.log", level="INFO")
    cfg = get_config()
    db = get_db()
    db.create_indexes()
    logger.info(f"API {RT.VERSION} started for server '{cfg.server.id}'")
    if RT.start(db, cfg.server.id):
        logger.info("replica set member — election loop running")
    stop = threading.Event()
    threading.Thread(target=_scan_scheduler, args=(cfg, stop, logger), name="scan-scheduler", daemon=True).start()
    try:
        yield
    finally:
        stop.set()
        RT.stop()
        db.close()
        logger.info("API shutdown complete")


app = FastAPI(
    title="InfraDocs API",
    version=RT.VERSION,
    description="Infrastructure documentation across a replica set of servers.",
    lifespan=lifespan,
)

# CORS — Phase 4 frontend runs separately (dev) and via nginx proxy (prod).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


@app.get("/join.sh", include_in_schema=False)
def join_sh(request: Request):
    """The one-line join command on the Servers page downloads this script."""
    return cluster.join_sh_response(request)


@app.get("/api/whoami-primary", include_in_schema=False)
def whoami_primary():
    """Which member serves the UI. A secondary's web server redirects browsers here."""
    n = RT.node()
    if n is None or n.is_primary():
        return {"primary": True, "leader_address": None}
    return {"primary": False, "leader_address": RT.leader_address(), "leader_id": n.st.leader_id}


@app.get("/")
def root():
    return {"name": "InfraDocs", "version": RT.VERSION}
