"""Auth core — bcrypt passwords + opaque DB-stored session tokens.

Replaces the single config-password Basic auth with real accounts: a seeded
`admin` (default password Changeme001, flagged must_change_password), bcrypt
hashes, and server-side sessions (revocable, no JWT/secret to manage). The
frontend logs in -> gets a token -> sends it as `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt

DEFAULT_PASSWORD = "Changeme001"
SESSION_TTL = timedelta(days=7)


def hash_password(plain: str) -> str:
    # bcrypt caps at 72 bytes; encode + slice keeps long inputs valid.
    return bcrypt.hashpw(plain.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode()[:72], hashed.encode())
    except Exception:
        return False


def seed_default_admin(db, username: str, default_password: str = DEFAULT_PASSWORD) -> None:
    """Create the initial admin (must change password) if there are no users."""
    if db.db.users.count_documents({}) == 0:
        db.db.users.insert_one({
            "username": username,
            "password_hash": hash_password(default_password),
            "must_change_password": True,
            "role": "operator",  # first account is a full operator; viewers are opt-in
            "created_at": datetime.now(timezone.utc),
        })


def get_user(db, username: str) -> Optional[dict]:
    return db.db.users.find_one({"username": username})


def create_session(db, username: str) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.db.sessions.insert_one({
        "token": token,
        "username": username,
        "created_at": now,
        "expires_at": now + SESSION_TTL,
    })
    return token


def validate_session(db, token: str) -> Optional[str]:
    if not token:
        return None
    s = db.db.sessions.find_one({"token": token})
    if not s:
        return None
    exp = s.get("expires_at")
    if exp is not None:
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            db.db.sessions.delete_one({"token": token})
            return None
    return s.get("username")


def delete_session(db, token: str) -> None:
    if token:
        db.db.sessions.delete_one({"token": token})


def set_password(db, username: str, new_password: str) -> None:
    db.db.users.update_one(
        {"username": username},
        {"$set": {
            "password_hash": hash_password(new_password),
            "must_change_password": False,
        }},
    )
