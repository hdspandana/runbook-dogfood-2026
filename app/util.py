"""Small shared helpers: UTC time handling, tokens, deterministic JSON."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from flask import jsonify


# --------------------------------------------------------------------------
# time — every deadline decision in this application is made in UTC
# --------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string into an aware UTC datetime. Returns None on junk."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def parse_form_datetime(value: str | None) -> datetime | None:
    """Accept 'YYYY-MM-DDTHH:MM' from an <input type=datetime-local>, interpreted as UTC."""
    return parse_iso(value)


def pretty_utc(value: str | datetime | None, with_date: bool = True) -> str:
    dt = value if isinstance(value, datetime) else parse_iso(value)
    if dt is None:
        return "—"
    return dt.strftime("%d %b %Y %H:%M UTC" if with_date else "%H:%M UTC")


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------
def new_token(nbytes: int = 24) -> str:
    """Invite links and session ids use the same CSPRNG primitive.

    Never sequential ids: an invite token is a 192-bit secret.
    """
    return secrets.token_urlsafe(nbytes)


def fingerprint(parts: Iterable[Any]) -> str:
    """Deterministic sha256 over a canonical JSON structure (used for STALE detection)."""
    payload = json.dumps(list(parts), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# responses
# --------------------------------------------------------------------------
def json_ok(payload: dict[str, Any], status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def json_error(message: str, status: int = 400, **extra: Any):
    """Error bodies never echo back the protected resource.

    A denied cross-judge read returns {"error": "forbidden"} and nothing else —
    no scores, no ids that were not already in the URL the caller guessed.
    """
    body: dict[str, Any] = {"error": message}
    body.update(extra)
    response = jsonify(body)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def humanise_delta(seconds: float) -> str:
    if seconds < 0:
        seconds = -seconds
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def countdown(target: datetime | None, reference: datetime | None = None) -> str:
    if target is None:
        return "—"
    reference = reference or now_utc()
    delta = target - reference
    return humanise_delta(delta.total_seconds())


__all__ = [
    "now_utc",
    "to_iso",
    "parse_iso",
    "parse_form_datetime",
    "pretty_utc",
    "new_token",
    "fingerprint",
    "json_ok",
    "json_error",
    "humanise_delta",
    "countdown",
    "timedelta",
]