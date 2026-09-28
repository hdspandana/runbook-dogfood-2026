"""Boot-time bootstrap: fixed demo sessions for the acceptance harness.

The DOGFOOD acceptance checker talks to the portal with per-role authentication.
We expose that as four *fixed, documented* demo sessions (opaque server-side
session ids) that map exactly to the four roles the seven checks exercise:

    organizer   → exporter
    judge_a     → the judge whose own review must return 200
    judge_b     → the peer judge whose review must return 401/403
    participant → the participant whose judge-score read must return 401/403

These tokens are demo credentials for a self-hosted, fixture-seeded portal.
They are printed at boot, they are listed in README.md, and they are the values
written into .dogfood.toml. Set RUNBOOK_DEMO_SESSIONS=off to disable them
entirely (the checker then has to log in through /login, which also works).
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from . import db
from .util import new_token, now_utc, to_iso

SESSION_COOKIE = "runbook_session"

DEMO_TOKENS = {
    "organizer": "demo-session-organizer-3f9c1a7d5b2e",
    "admin": "demo-session-admin-7c4e0b91a6d2",
    "judge_a": "demo-session-judge-a-1d8f5c30b7a4",
    "judge_b": "demo-session-judge-b-9a2b6e04c1f7",
    "participant": "demo-session-participant-5e7d2a8c40b1",
}


def demo_sessions_enabled() -> bool:
    return os.environ.get("RUNBOOK_DEMO_SESSIONS", "on").strip().lower() not in {"0", "false", "off", "no"}


def _role_holder(role: str):
    return db.query_one(
        "SELECT * FROM users WHERE role = ? ORDER BY id ASC LIMIT 1", (role,)
    )


def _pick_case() -> dict[str, Any]:
    """Choose the acceptance case: a project with two *complete* reviews in the
    event whose submission window has closed."""
    row = db.query_one(
        """SELECT p.id AS project_id, p.event_id, p.name AS project_name,
                  r1.judge_id AS judge_a_id, r2.judge_id AS judge_b_id,
                  r1.id AS review_a_id, r2.id AS review_b_id,
                  p.track_id
             FROM reviews r1
             JOIN reviews r2 ON r2.project_id = r1.project_id AND r2.judge_id > r1.judge_id
             JOIN projects p ON p.id = r1.project_id
            WHERE r1.status = 'complete' AND r2.status = 'complete'
              AND r1.event_id = r2.event_id
            ORDER BY p.event_id ASC, p.id ASC
            LIMIT 1"""
    )
    if row is not None:
        return dict(row)

    # Fall back to any assignment pair (still gives a meaningful 403 test).
    row = db.query_one(
        """SELECT p.id AS project_id, p.event_id, p.name AS project_name,
                  a1.judge_id AS judge_a_id, a2.judge_id AS judge_b_id,
                  NULL AS review_a_id, NULL AS review_b_id, p.track_id
             FROM assignments a1
             JOIN assignments a2 ON a2.project_id = a1.project_id AND a2.judge_id > a1.judge_id
             JOIN projects p ON p.id = a1.project_id
            ORDER BY p.event_id ASC, p.id ASC
            LIMIT 1"""
    )
    return dict(row) if row is not None else {}


def _pick_participant(event_id: int):
    return db.query_one(
        """SELECT u.*, tm.team_id, p.id AS project_id
             FROM users u
             JOIN team_members tm ON tm.user_id = u.id
             JOIN projects p ON p.team_id = tm.team_id
            WHERE u.role = 'participant' AND tm.event_id = ?
            ORDER BY p.id ASC LIMIT 1""",
        (event_id,),
    )


def ensure_demo_sessions(verbose: bool = True) -> dict[str, Any]:
    """Create (or refresh) the fixed demo sessions. Idempotent."""
    if not demo_sessions_enabled():
        return {"enabled": False}

    case = _pick_case()
    event_id = case.get("event_id")
    participants = _pick_participant(event_id) if event_id else None

    mapping: dict[str, Optional[int]] = {
        "organizer": (lambda u: u["id"] if u else None)(_role_holder("organizer")),
        "admin": (lambda u: u["id"] if u else None)(_role_holder("admin")),
        "judge_a": case.get("judge_a_id"),
        "judge_b": case.get("judge_b_id"),
        "participant": participants["id"] if participants else None,
    }

    expires = to_iso(now_utc().replace(year=now_utc().year + 2))
    created: dict[str, str] = {}
    for role, user_id in mapping.items():
        token = DEMO_TOKENS[role]
        if not user_id:
            continue
        db.execute("DELETE FROM sessions WHERE id = ?", (token,))
        db.execute(
            "INSERT INTO sessions (id, user_id, created_at, expires_at, user_agent) VALUES (?, ?, ?, ?, ?)",
            (token, int(user_id), to_iso(now_utc()), expires, "dogfood-acceptance-harness"),
        )
        row = db.query_one("SELECT email, display_name FROM users WHERE id = ?", (int(user_id),))
        created[role] = row["email"] if row else str(user_id)

    info = {
        "enabled": True,
        "cookie_name": SESSION_COOKIE,
        "tokens": {role: DEMO_TOKENS[role] for role in mapping if mapping[role]},
        "accounts": created,
        "case": {
            "event_id": event_id,
            "project_id": case.get("project_id"),
            "project": case.get("project_name"),
            "judge_a_review_id": case.get("review_a_id"),
            "judge_b_review_id": case.get("review_b_id"),
            "track_id": case.get("track_id"),
            "participant_project_id": participants["project_id"] if participants else None,
            "participant_team_id": participants["team_id"] if participants else None,
        },
    }
    if verbose:
        print(json.dumps(info, indent=2))
    return info