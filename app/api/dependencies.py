"""FastAPI dependencies: config, DB connection, basic-auth."""

import os
import secrets
import time
from functools import lru_cache
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.core.config_loader import Config, load_config
from app.core.db_manager import DBManager


# auto_error=False so verify_auth() can decide what to do when no header
# is present — needed for the INFRADOCS_AUTH_DISABLED bypass below.
_basic = HTTPBasic(auto_error=False)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _auth_disabled(request: Optional[Request]) -> bool:
    """The open-house dev bypass — honored ONLY for a loopback caller.

    Leaving INFRADOCS_AUTH_DISABLED=1 set must never silently expose the API to the
    network: a genuinely remote client still has to authenticate. A host-local dev
    client (127.0.0.1/::1) can use it. NOTE: the API runs host-network behind a
    same-host nginx, so a proxied request also presents a loopback peer — do not rely
    on this as network isolation; it is a guard against the flag being honored for
    direct remote connections, not a substitute for keeping the flag OFF in a real deploy."""
    if os.environ.get("INFRADOCS_AUTH_DISABLED", "").lower() not in (
        "1", "true", "yes", "on",
    ):
        return False
    host = request.client.host if (request and request.client) else None
    return host in _LOOPBACK_HOSTS


# In-memory throttle for the HTTP Basic credential path (the /login endpoint has its
# own). Without it, Authorization: Basic against any endpoint is an unthrottled brute
# force channel that bypasses the login throttle entirely.
_BASIC_FAILS: dict = {}
_BASIC_WINDOW = 300      # seconds
_BASIC_MAX = 10          # failures per window per username before a 429


def _basic_throttled(key: str, now: float) -> bool:
    hits = [t for t in _BASIC_FAILS.get(key, []) if now - t < _BASIC_WINDOW]
    _BASIC_FAILS[key] = hits
    return len(hits) >= _BASIC_MAX


def _basic_fail(key: str, now: float) -> None:
    _BASIC_FAILS.setdefault(key, []).append(now)


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


@lru_cache(maxsize=1)
def get_db() -> DBManager:
    cfg = get_config()
    return DBManager(uri=cfg.mongodb.uri, database=cfg.mongodb.database)


# Paths a user with must_change_password may still reach, so they can rotate it
# (and load/unload the session) without being locked out of the whole API.
_PW_CHANGE_EXEMPT = frozenset({
    "/api/auth/change-password", "/api/auth/logout", "/api/auth/me", "/api/auth/login",
})


def _enforce_password_change(db: DBManager, username: str, request: Optional[Request]) -> None:
    """Block a must_change_password user from every endpoint except the exempt auth paths.
    Server-side enforcement (not just the frontend redirect) so the default seeded
    credential can't be used against the API directly until it is rotated."""
    if request is not None and request.url.path in _PW_CHANGE_EXEMPT:
        return
    from app import auth as A  # lazy — avoids import cycle
    u = A.get_user(db, username)
    if u and u.get("must_change_password"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="password change required — rotate the default password first",
        )


def verify_auth(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_fleet_auth: Optional[str] = Header(None),
    credentials: Optional[HTTPBasicCredentials] = Depends(_basic),
    db: DBManager = Depends(get_db),
) -> str:
    # Open-house mode: set INFRADOCS_AUTH_DISABLED=1 in .env to bypass auth (loopback only).
    if _auth_disabled(request):
        return credentials.username if credentials else "anonymous"

    # Fleet service credential: the shared secret the leader's node-switcher proxy presents
    # when forwarding a request to a peer. A distinct 'fleet' principal — never a user session,
    # so a proxied call is accepted without the peer knowing the operator's login.
    if x_fleet_auth:
        from app.fleet_auth import fleet_auth_ok
        if fleet_auth_ok(db, x_fleet_auth):
            return "fleet"

    from app import auth as A  # lazy to avoid import cycle

    # 1) Bearer session token (issued by /api/auth/login — the web path).
    if authorization and authorization.lower().startswith("bearer "):
        username = A.validate_session(db, authorization[7:].strip())
        if username:
            _enforce_password_change(db, username, request)
            return username

    # 2) HTTP Basic — web users (bcrypt) OR the bootstrap config credential.
    if credentials is not None:
        now = time.time()
        key = credentials.username or "?"
        # Brute-force guard on the Basic path (the /login endpoint has its own throttle).
        if _basic_throttled(key, now):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many failed attempts — wait a few minutes",
            )
        user = A.get_user(db, credentials.username)
        if user and A.verify_password(credentials.password, user.get("password_hash", "")):
            _BASIC_FAILS.pop(key, None)
            _enforce_password_change(db, credentials.username, request)
            return credentials.username
        # Bootstrap / service-account path: the config username+password. Lets
        # curl, scanners, and the agent authenticate without the web login flow.
        # Disabled entirely when no password is configured (no committed fallback).
        cfg = get_config()
        expected = os.environ.get(cfg.auth.password_env) or cfg.auth.dev_password
        if expected and credentials.username == cfg.auth.username and secrets.compare_digest(
            credentials.password, expected
        ):
            _BASIC_FAILS.pop(key, None)
            return credentials.username
        _basic_fail(key, now)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )


# Well-known non-user principals returned by verify_auth().
FLEET_PRINCIPAL = "fleet"        # the leader→peer proxy relay credential (X-Fleet-Auth)
ANON_PRINCIPAL = "anonymous"     # only when INFRADOCS_AUTH_DISABLED=1 (local dev)


def require_real_user(actor: str = Depends(verify_auth)) -> str:
    """Reject the relayed 'fleet' service principal on user/credential-management
    endpoints (join-token mint/revoke, account changes). A peer relay carries the
    fleet secret only to forward an already-authorized operator request — it must
    never be able to mint new fleet trust anchors or alter accounts on the peer."""
    if actor == FLEET_PRINCIPAL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not permitted for the fleet service principal",
        )
    return actor


# ---------------------------------------------------------------------------
# Roles (RBAC). Two tiers: 'viewer' (read-only) and 'operator' (can fire actions,
# teardowns, cluster transitions, token mint). Historically every authenticated
# user was a full admin; to stay backward-compatible an account with NO explicit
# role is treated as 'operator', and 'viewer' is an opt-in downgrade. Create a
# read-only account by setting users.role = "viewer" in the DB.
# ---------------------------------------------------------------------------
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"


def get_role(db: DBManager, principal: str) -> str:
    # Relayed fleet requests + the auth-disabled dev bypass are operator-capable.
    if principal in (FLEET_PRINCIPAL, ANON_PRINCIPAL):
        return ROLE_OPERATOR
    from app import auth as A  # lazy — avoids import cycle
    u = A.get_user(db, principal)
    if u is None:
        # config-credential service account (not a DB user) — operator-capable.
        return ROLE_OPERATOR
    return u.get("role") or ROLE_OPERATOR


def require_operator(
    actor: str = Depends(verify_auth), db: DBManager = Depends(get_db)
) -> str:
    """Gate for any state-changing / privileged endpoint. Viewers get 403.
    The 'fleet' relay principal is operator-capable so cross-node actions work."""
    if get_role(db, actor) != ROLE_OPERATOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="operator role required for this action",
        )
    return actor


def require_operator_user(
    actor: str = Depends(require_real_user), db: DBManager = Depends(get_db)
) -> str:
    """Operator role AND a real user (not the relayed fleet principal) — for
    fleet-credential-management endpoints (join-token mint/revoke)."""
    if get_role(db, actor) != ROLE_OPERATOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="operator role required",
        )
    return actor
