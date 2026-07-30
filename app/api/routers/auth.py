"""Auth endpoints — login, change-password, logout, me."""

import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app import auth as A
from app.api.dependencies import get_db, verify_auth
from app.core.db_manager import DBManager

router = APIRouter()

# Simple in-memory login throttle: 5 failures / 5 min per username.
_FAILS: dict = defaultdict(list)
_WINDOW = 300
_MAX_FAILS = 5


def _throttled(key: str) -> bool:
    now = time.time()
    _FAILS[key] = [t for t in _FAILS[key] if now - t < _WINDOW]
    return len(_FAILS[key]) >= _MAX_FAILS


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(req: LoginRequest, db: DBManager = Depends(get_db)):
    if _throttled(req.username):
        raise HTTPException(status_code=429, detail="too many attempts — wait a few minutes")
    user = A.get_user(db, req.username)
    if not user or not A.verify_password(req.password, user.get("password_hash", "")):
        _FAILS[req.username].append(time.time())
        raise HTTPException(status_code=401, detail="invalid username or password")
    _FAILS.pop(req.username, None)
    token = A.create_session(db, req.username)
    return {
        "token": token,
        "username": req.username,
        "role": user.get("role") or "operator",
        "must_change_password": bool(user.get("must_change_password")),
    }


class ChangePasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8)


@router.post("/change-password")
def change_password(
    req: ChangePasswordRequest,
    actor: str = Depends(verify_auth),
    db: DBManager = Depends(get_db),
):
    if req.new_password == A.DEFAULT_PASSWORD:
        raise HTTPException(status_code=400, detail="choose a password other than the default")
    A.set_password(db, actor, req.new_password)
    return {"ok": True}


@router.post("/logout")
def logout(authorization: Optional[str] = Header(None), db: DBManager = Depends(get_db)):
    if authorization and authorization.lower().startswith("bearer "):
        A.delete_session(db, authorization[7:])
    return {"ok": True}


@router.get("/me")
def me(actor: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    user = A.get_user(db, actor) or {}
    return {"username": actor, "role": user.get("role") or "operator",
            "must_change_password": bool(user.get("must_change_password"))}
