"""Pytest config — load .env before any tests inspect environment.

Also expose an `auth` fixture so API tests use the same credentials the
running API expects (env var → dev_password fallback). Hard-coding
("msinha", "msinha123") in tests would silently break the moment the
operator sets INFRADOCS_API_PASSWORD in .env.
"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)

# Tests always exercise the auth-enforced path, even if .env has
# INFRADOCS_AUTH_DISABLED=1 (the toggle is for production access only).
# Setting to "0" (rather than popping) so app.api.main's own
# load_dotenv(override=False) call at import time doesn't resurrect the
# .env value — override=False only writes vars that are absent.
os.environ["INFRADOCS_AUTH_DISABLED"] = "0"

# Production ships NO committed config-credential fallback (auth.dev_password is None).
# The API tests exercise the config-credential Basic path, so provide an explicit
# password here via env — never a value baked into the repo.
os.environ.setdefault("INFRADOCS_API_PASSWORD", "test-api-password")


@pytest.fixture(scope="session")
def auth():
    """Return (username, password) matching app.api.dependencies.verify_auth."""
    # Import lazily so the .env load above wins before config_loader reads env.
    import sys

    sys.path.insert(0, str(ROOT))
    from app.core.config_loader import load_config

    cfg = load_config(str(ROOT / "config.yml"))
    password = os.environ.get(cfg.auth.password_env) or cfg.auth.dev_password
    return (cfg.auth.username, password)
