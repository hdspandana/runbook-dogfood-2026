"""Authentication: password hashing, opaque server-side sessions, user lookup."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from flask import current_app, request
from werkzeug.security import check_password_hash, generate_password_hash

from .. import db
from ..util import new_token, now_utc, to_iso

ROLES = ("participant", "judge", "organizer", "admin")
AUTH_COOKIE_NAME = "runbook_session"


def hash_password(raw: str) -> str:
    return generate_password_hash(raw, method="pbkdf2:sha256:260000")


def create_user(email: str, password: str, display_name: str, role: str = "participant") -> int:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    return db.insert(
        """INSERT INTO users (email, password_hash, display_name, role, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (email.strip().lower(), hash_password(password), display_name.strip() or email, role, to_iso(now_utc())),
    )


def find_user_by_email(email: str) -> Optional[Any]:
    return db.query_one("SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),))


def find_user(user_id: int) -> Optional[Any]:
    return db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))


def authenticate(email: str, password: str) -> Optional[Any]:
    user = find_user_by_email(email)
    if user is None:
        # Constant-ish work either way; do not disclose whether the email exists.
        check_password_hash(
            "pbkdf2:sha256:260000$AAAAAAAAAAAAAAAA$"
            "0000000000000000000000000000000000000000000000000000000000000000",
            password or "",
        )
        return None
    if not check_password_hash(user["password_hash"], password or ""):
        return None
    return user


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------
def create_session(user_id: int, user_agent: str | None = None) -> str:
    token = new_token(32)
    ttl = timedelta(hours=current_app.config["SESSION_TTL_HOURS"])
    db.execute(
        "INSERT INTO sessions (id, user_id, created_at, expires_at, user_agent) VALUES (?, ?, ?, ?, ?)",
        (token, user_id, to_iso(now_utc()), to_iso(now_utc() + ttl), (user_agent or "")[:200]),
    )
    return token


def resolve_session(token: str | None):
    """Resolve a session cookie to a user row, or None.

    Expiry is enforced server side; the cookie carries no claims at all.
    """
    if not token:
        return None
    row = db.query_one(
        """SELECT u.*, s.expires_at AS session_expires_at
             FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.id = ?""",
        (token,),
    )
    if row is None:
        return None
    from ..util import parse_iso

    expiry = parse_iso(row["session_expires_at"])
    if expiry is not None and expiry <= now_utc():
        destroy_session(token)
        return None
    return row


def destroy_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE id = ?", (token,))


def current_session_token() -> str | None:
    return request.cookies.get(AUTH_COOKIE_NAME)


def _cookie_flags() -> tuple[bool, str]:
    """Resolve the Secure / SameSite pair for the current request.

    The live preview this project is demonstrated in is an HTTPS host framing
    the app cross-site, where a SameSite=Lax cookie is dropped and sign-in would
    appear to "randomly" fail. A reverse proxy terminates TLS, so we key off
    X-Forwarded-Proto as well as the request scheme.
    """
    secure_config = current_app.config.get("AUTH_COOKIE_SECURE", "auto")
    samesite_config = current_app.config.get("AUTH_COOKIE_SAMESITE", "auto")
    if secure_config == "auto":
        forwarded = request.headers.get("X-Forwarded-Proto", "")
        secure = request.is_secure or forwarded.split(",")[0].strip() == "https"
    else:
        secure = bool(secure_config)
    if samesite_config == "auto":
        samesite = "None" if secure else "Lax"
    else:
        samesite = str(samesite_config)
    return secure, samesite


def set_session_cookie(response, token: str):
    ttl = timedelta(hours=current_app.config["SESSION_TTL_HOURS"])
    secure, samesite = _cookie_flags()
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=int(ttl.total_seconds()),
        httponly=current_app.config["SESSION_COOKIE_HTTPONLY"],
        samesite=samesite,
        secure=secure,
        path="/",
    )
    return response


def clear_session_cookie(response):
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response