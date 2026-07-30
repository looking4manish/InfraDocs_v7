"""FastAPI dependencies: config, DB connection, basic-auth."""

import os
import secrets
from functools import lru_cache
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.core.config_loader import Config, load_config
from app.core.db_manager import DBManager


# auto_error=False so verify_auth() can decide what to do when no header
# is present — needed for the INFRADOCS_AUTH_DISABLED bypass below.
_basic = HTTPBasic(auto_error=False)


def _auth_disabled() -> bool:
    return os.environ.get("INFRADOCS_AUTH_DISABLED", "").lower() in (
        "1", "true", "yes", "on",
    )


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
    # Open-house mode: set INFRADOCS_AUTH_DISABLED=1 in .env to bypass auth.
    if _auth_disabled():
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
        user = A.get_user(db, credentials.username)
        if user and A.verify_password(credentials.password, user.get("password_hash", "")):
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
            return credentials.username

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
