"""Application configuration.

Everything is environment-driven with self-hosting defaults so that
``docker compose up`` works with zero configuration, and ``python -m runbook``
works on a laptop with no network.
"""

from __future__ import annotations

import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _auto_flag(name: str):
    """Return True, False, or the string "auto" (decide per request)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip().lower() in {"", "auto"}:
        return "auto"
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Read environment at *instantiation* so tests can change it per app."""

    def __init__(self) -> None:
        # --- storage -------------------------------------------------------
        self.DATA_DIR = Path(os.environ.get("RUNBOOK_DATA_DIR", "/data"))
        self.SECRET_KEY = os.environ.get("RUNBOOK_SECRET_KEY", "dev-only-not-a-secret")
        self.SEED_ON_BOOT = _bool("RUNBOOK_SEED_ON_BOOT", True)

        # --- sessions ------------------------------------------------------
        # Opaque, server-side sessions: the cookie holds only a random token.
        # Flask's flash() uses its own signed cookie. Give it a *different*
        # name so a flash after a successful mutation never overwrites our
        # opaque server-side authentication cookie.
        self.SESSION_COOKIE_NAME = "runbook_flashes"
        self.SESSION_TTL_HOURS = int(os.environ.get("RUNBOOK_SESSION_TTL_HOURS", "24"))
        # "auto" resolves per request: behind HTTPS (a reverse proxy or a framed
        # preview host) the cookie becomes Secure + SameSite=None, which such a
        # frame requires; plain local HTTP stays Lax without Secure, because a
        # Secure cookie is never stored on http://localhost.
        self.SESSION_COOKIE_SECURE = _auto_flag("RUNBOOK_COOKIE_SECURE")
        self.SESSION_COOKIE_SAMESITE = _auto_flag("RUNBOOK_COOKIE_SAMESITE")
        self.SESSION_COOKIE_HTTPONLY = True

        # Minimum completed reviews before a judge's personal mean/sigma is
        # trusted for cross-judge normalization. See JUDGING.md.
        self.MIN_REVIEWS_FOR_NORMALIZATION = int(os.environ.get("RUNBOOK_MIN_REVIEWS_FOR_NORM", "3"))
        self.JSON_SORT_KEYS = False
        self.MAX_CONTENT_LENGTH = 2 * 1024 * 1024

    @property
    def DATABASE(self) -> str:
        return str(self.DATA_DIR / "runbook.sqlite3")


def ensure_data_dir(cfg: Config) -> None:
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)