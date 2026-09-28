"""Authorization core.

Rule of the codebase: **a route never decides permissions on its own.**
Routes call the helpers below, which answer concrete questions such as
"may *this* user read *this* review?" instead of the useless
"is this user a judge?".

The decorators return real 401/403 status codes with empty-ish bodies.
They never silently redirect a programmatic client to the login page, because
a redirect hides an authorization failure behind a 302 and would make a
denial look like a success to a checker.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Optional

from flask import g, redirect, request, url_for

from . import db
from .auth import service as auth_service
from .util import json_error


# --------------------------------------------------------------------------
# current identity
# --------------------------------------------------------------------------
def load_current_user() -> Optional[Any]:
    """Derive the authenticated identity from the session cookie ONLY.

    There is no code path anywhere in this application that reads `judge_id`,
    `user_id`, `role` or `team_id` from a query string, form field, JSON body
    or header to decide who the caller is.
    """
    if "current_user" not in g:
        g.current_user = auth_service.resolve_session(auth_service.current_session_token())
        g.session_token = auth_service.current_session_token()
    return g.current_user


def current_user():
    return load_current_user()


def is_authenticated() -> bool:
    return load_current_user() is not None


def roles_of(user) -> set[str]:
    if user is None:
        return set()
    if user["role"] == "admin":
        # Admin is a real, explicit role: it holds organizer capabilities plus
        # system-level management. We express that as an explicit capability
        # set rather than sprinkling `role == 'admin'` checks through routes.
        return {"admin", "organizer"}
    return {user["role"]}


def has_role(user, *roles: str) -> bool:
    return bool(roles_of(user) & set(roles))


def is_organizer(user) -> bool:
    return has_role(user, "organizer")


def is_judge(user) -> bool:
    return user is not None and user["role"] == "judge"


# --------------------------------------------------------------------------
# denial helpers
# --------------------------------------------------------------------------
def wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return best == "application/json" and request.accept_mimetypes[best] >= request.accept_mimetypes["text/html"]


def deny(message: str, status: int = 403):
    if wants_json():
        return json_error(message, status)
    from flask import render_template, abort

    if status == 401:
        return render_template("error.html", code=401, message="Authentication required."), 401
    # Render the denial page and RETURN the status — never abort() inside a
    # helper that callers may want to compose.
    return render_template("error.html", code=status, message=message), status


def deny_404():
    """Used when the caller must not even learn that a protected row exists."""
    from flask import abort

    abort(404)


# --------------------------------------------------------------------------
# decorators
# --------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_authenticated():
            return deny("Authentication required.", 401)
        return view(*args, **kwargs)

    return wrapper


def roles_required(*roles: str):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                return deny("Authentication required.", 401)
            if not has_role(user, *roles):
                return deny("Your role is not permitted to perform this action.", 403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def organizer_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return deny("Authentication required.", 401)
        if not is_organizer(user):
            return deny("Organizer access required.", 403)
        return view(*args, **kwargs)

    return wrapper


def judge_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return deny("Authentication required.", 401)
        if not is_judge(user):
            return deny("Judge access required.", 403)
        return view(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------
# resource-level questions (the interesting part)
# --------------------------------------------------------------------------
def get_event(event_id: int):
    return db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))


def get_project(project_id: int):
    return db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))


def get_review(review_id: int):
    return db.query_one("SELECT * FROM reviews WHERE id = ?", (review_id,))


def membership(user_id: int, team_id: int):
    return db.query_one(
        "SELECT * FROM team_members WHERE user_id = ? AND team_id = ?", (user_id, team_id)
    )


def user_team_in_event(user_id: int, event_id: int):
    return db.query_one(
        "SELECT * FROM team_members WHERE user_id = ? AND event_id = ?", (user_id, event_id)
    )


def is_project_member(user, project) -> bool:
    """True only for a participant who is on the project's team."""
    if user is None or project is None:
        return False
    return membership(user["id"], project["team_id"]) is not None


def judge_assignment(user, project):
    """The assignment row that lets this judge touch this project, or None."""
    if user is None or project is None:
        return None
    return db.query_one(
        """SELECT a.* FROM assignments a
            JOIN projects p ON p.id = a.project_id
            JOIN judge_invites i ON i.event_id = a.event_id AND i.accepted_by = a.judge_id
           WHERE a.project_id = ? AND a.judge_id = ? AND a.event_id = p.event_id
             AND a.track_id = p.track_id
             AND (i.track_id IS NULL OR i.track_id = p.track_id)""",
        (project["id"], user["id"]),
    )


def judge_can_access_project(user, project) -> bool:
    """A judge may read/review a project only when assigned to it.

    Being a judge in the same event is NOT sufficient — that is exactly the
    IDOR that a `role == 'judge'` check would let through.
    """
    if not is_judge(user) and not (user is not None and "judge" in roles_of(user)):
        return False
    return judge_assignment(user, project) is not None


def judge_track_scope(user, event_id: int) -> set[int]:
    """Track ids in which this judge holds at least one assignment."""
    rows = db.query_all(
        """SELECT DISTINCT a.track_id FROM assignments a
             JOIN projects p ON p.id = a.project_id
             JOIN judge_invites i ON i.event_id = a.event_id AND i.accepted_by = a.judge_id
            WHERE a.judge_id = ? AND a.event_id = ? AND a.event_id = p.event_id
              AND a.track_id = p.track_id AND a.track_id IS NOT NULL
              AND (i.track_id IS NULL OR i.track_id = p.track_id)""",
        (user["id"], event_id),
    )
    return {r["track_id"] for r in rows}


def judge_can_access_track(user, event_id: int, track_id: Optional[int]) -> bool:
    if track_id is None:
        return False
    return track_id in judge_track_scope(user, event_id)


def can_read_review(user, review) -> bool:
    """Review reads: the review's own judge, or an organizer/admin. Nobody else."""
    if user is None or review is None:
        return False
    if is_organizer(user):
        return True
    return review["judge_id"] == user["id"]


# --------------------------------------------------------------------------
# deadline enforcement (server-side UTC)
# --------------------------------------------------------------------------
def submissions_closed(event, at=None) -> bool:
    from .util import now_utc, parse_iso

    deadline = parse_iso(event["submission_deadline"])
    reference = at or now_utc()
    return deadline is not None and reference >= deadline


def judging_open(event, at=None) -> bool:
    from .util import now_utc, parse_iso

    reference = at or now_utc()
    start = parse_iso(event["judging_starts_at"])
    end = parse_iso(event["judging_ends_at"])
    return (start is None or reference >= start) and (end is None or reference < end)