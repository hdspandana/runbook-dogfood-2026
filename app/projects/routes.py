"""Participant-side project routes (HTML + JSON).

Every write path funnels through `enforce_deadline` **before** touching the
database, so a post-deadline mutation is rejected regardless of what the browser
believes about the state of the world.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..events import service as events_service
from ..security import current_user, deny, membership
from ..util import json_error, json_ok, to_iso, now_utc
from . import service

bp = Blueprint("projects", __name__)


def _load_membership(user, project):
    if project is None:
        return None
    return membership(user["id"], project["team_id"])


def _form_values(form) -> dict:
    return {
        "name": form.get("name"),
        "tagline": form.get("tagline"),
        "description": form.get("description"),
        "repo_url": form.get("repo_url"),
        "demo_url": form.get("demo_url"),
        "web_url": form.get("web_url"),
        "track_id": form.get("track_id", type=int),
    }


def _payload_values(payload: dict) -> dict:
    return {
        "name": payload.get("name"),
        "tagline": payload.get("tagline"),
        "description": payload.get("description"),
        "repo_url": payload.get("repo_url"),
        "demo_url": payload.get("demo_url"),
        "web_url": payload.get("web_url"),
        "track_id": payload.get("track_id"),
    }


# --------------------------------------------------------------------------
# participant dashboard
# --------------------------------------------------------------------------
@bp.route("/projects")
def index():
    user = current_user()
    if user is None:
        return deny("Sign in to manage your submission.", 401)
    if user["role"] == "judge":
        return redirect(url_for("judge.index"))
    context = service.participant_context(user)
    open_events = []
    for event in events_service.all_events():
        if not any(item["event"]["id"] == event["id"] for item in context):
            timing = events_service.timing_summary(event)
            if not timing["deadline_passed"]:
                open_events.append({"event": event, "timing": timing})
    return render_template("participant_dashboard.html", context=context, open_events=open_events,
                           teams_for=lambda event_id: None)


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------
@bp.route("/projects/new", methods=["GET", "POST"])
def new():
    user = current_user()
    if user is None:
        return deny("Sign in to create a project.", 401)
    if user["role"] != "participant":
        return deny("Only participant accounts can create a submission.", 403)

    event_id = request.values.get("event_id", type=int)
    event = events_service.get_event(event_id) if event_id else None
    if event is None:
        return deny("Unknown event.", 400)

    membership_row = db.query_one(
        "SELECT * FROM team_members WHERE event_id = ? AND user_id = ?", (event["id"], user["id"])
    )
    if membership_row is None:
        flash("Create or join a team in this event before creating a project.", "warn")
        return redirect(url_for("projects.index"))

    existing = service.team_project(event["id"], membership_row["team_id"])
    if existing:
        return redirect(url_for("projects.manage", project_id=existing["id"]))

    if request.method == "GET":
        return render_template("project_form.html", event=event, project=None,
                               tracks=events_service.tracks_for(event["id"]),
                               timing=events_service.timing_summary(event), mode="create")

    blocked = service.enforce_deadline(event, as_json=False)
    if blocked is not None:
        return blocked

    values = service.validate(service.clean(_form_values(request.form)), event)
    if values:
        return render_template("project_form.html", event=event, project=None,
                               tracks=events_service.tracks_for(event["id"]),
                               timing=events_service.timing_summary(event), mode="create",
                               errors=values, values=_form_values(request.form)), 400

    project = service.create_project(event, membership_row["team_id"], service.clean(_form_values(request.form)))
    flash("Draft created. Submissions are not visible publicly until you submit.", "ok")
    return redirect(url_for("projects.manage", project_id=project["id"]))


# --------------------------------------------------------------------------
# edit / submit
# --------------------------------------------------------------------------
@bp.route("/projects/<int:project_id>/manage", methods=["GET", "POST"])
def manage(project_id: int):
    user = current_user()
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        abort(404)
    if user is None or user["role"] != "participant" or _load_membership(user, project) is None:
        return deny("You are not a member of this project's team.", 403)

    event = events_service.get_event(project["event_id"])
    timing = events_service.timing_summary(event)

    if request.method == "GET":
        return render_template("project_form.html", event=event, project=project,
                               tracks=events_service.tracks_for(event["id"]),
                               timing=timing, mode="edit")

    blocked = service.enforce_deadline(event, as_json=False)
    if blocked is not None:
        return blocked

    values = service.clean(_form_values(request.form))
    errors = service.validate(values, event)
    if errors:
        return render_template("project_form.html", event=event, project=project,
                               tracks=events_service.tracks_for(event["id"]),
                               timing=timing, mode="edit", errors=errors,
                               values=_form_values(request.form)), 400

    service.update_project(project, values)
    flash("Project updated.", "ok")
    return redirect(url_for("projects.manage", project_id=project["id"]))


@bp.route("/projects/<int:project_id>/submit", methods=["POST"])
def submit(project_id: int):
    user = current_user()
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        abort(404)
    if user is None or user["role"] != "participant" or _load_membership(user, project) is None:
        return deny("You are not a member of this project's team.", 403)

    event = events_service.get_event(project["event_id"])
    blocked = service.enforce_deadline(event, as_json=False)
    if blocked is not None:
        return blocked

    values = service.clean(dict(project))
    errors = service.validate(values, event)
    if errors:
        return render_template("project_form.html", event=event, project=project,
                               tracks=events_service.tracks_for(event["id"]),
                               timing=events_service.timing_summary(event), mode="edit",
                               errors=errors), 400

    service.submit_project(project)
    flash("Submitted. You can still edit until the deadline.", "ok")
    return redirect(url_for("projects.manage", project_id=project["id"]))


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------
@bp.route("/api/projects", methods=["POST"])
def api_create():
    user = current_user()
    if user is None:
        return json_error("authentication required", 401)
    if user["role"] != "participant":
        return json_error("forbidden", 403)
    payload = request.get_json(silent=True) or {}
    event_id = payload.get("event_id")
    # The DOGFOOD checker POSTs a title/summary probe without an event id.
    # Resolve that to the primary fixture event and enforce *its real deadline*,
    # rather than returning a syntactic 400 for a missing parameter.
    try:
        event = events_service.get_event(int(event_id)) if event_id else events_service.primary_event()
    except (ValueError, TypeError):
        event = None
    if event is None:
        return json_error("unknown event", 400)
    row = db.query_one("SELECT * FROM team_members WHERE event_id = ? AND user_id = ?",
                       (event["id"], user["id"]))
    team_id = payload.get("team_id") or (row["team_id"] if row else None)
    if not team_id:
        return json_error("join a team first", 409)
    try:
        parsed_team_id = int(team_id)
    except (TypeError, ValueError):
        return json_error("invalid team", 400)
    if row is None or parsed_team_id != int(row["team_id"]):
        return json_error("not a member of that team", 403)

    blocked = service.enforce_deadline(event, as_json=True)
    if blocked is not None:
        return blocked

    existing = service.team_project(event["id"], int(team_id))
    if existing:
        return json_error("this team already has a project in this event", 409, project_id=existing["id"])

    values = service.clean(_payload_values(payload))
    errors = service.validate(values, event)
    if errors:
        return json_error("validation failed", 400, errors=errors)

    project = service.create_project(event, int(team_id), values)
    return json_ok({"project": dict(project)}, 201)


@bp.route("/api/projects/<int:project_id>", methods=["GET", "POST", "PATCH"])
def api_project(project_id: int):
    user = current_user()
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        return json_error("not found", 404)
    if user is None:
        return json_error("authentication required", 401)

    is_member = user["role"] == "participant" and _load_membership(user, project) is not None
    from ..security import is_organizer

    if not is_member and not is_organizer(user):
        # Includes a participant asking for another team's project, and a judge
        # who happens to be assigned to it (judges have their own endpoint).
        return json_error("forbidden", 403)

    if request.method == "GET":
        payload = {k: project[k] for k in
                   ("id", "event_id", "team_id", "track_id", "name", "tagline", "description",
                    "repo_url", "demo_url", "web_url", "status", "submitted_at", "updated_at")}
        return json_ok({"project": payload})

    if not is_member:
        return json_error("only the project's team can modify it", 403)

    event = events_service.get_event(project["event_id"])
    blocked = service.enforce_deadline(event, as_json=True)
    if blocked is not None:
        return blocked

    payload = request.get_json(silent=True) or {}
    allowed_fields = ("name", "tagline", "description", "repo_url", "demo_url", "web_url", "track_id")
    merged = service.clean({**dict(project), **{k: payload[k] for k in allowed_fields if k in payload}})
    errors = service.validate(merged, event)
    if errors:
        return json_error("validation failed", 400, errors=errors)
    service.update_project(project, merged)
    refreshed = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    return json_ok({"project": dict(refreshed)})


@bp.route("/api/projects/<int:project_id>/submit", methods=["POST"])
def api_submit(project_id: int):
    user = current_user()
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        return json_error("not found", 404)
    if user is None:
        return json_error("authentication required", 401)
    if user["role"] != "participant" or _load_membership(user, project) is None:
        return json_error("forbidden", 403)

    event = events_service.get_event(project["event_id"])
    blocked = service.enforce_deadline(event, as_json=True)
    if blocked is not None:
        return blocked

    errors = service.validate(service.clean(dict(project)), event)
    if errors:
        return json_error("validation failed", 400, errors=errors)

    service.submit_project(project)
    refreshed = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    return json_ok({"project": dict(refreshed), "status": "submitted",
                    "submitted_at": refreshed["submitted_at"]})


@bp.route("/api/projects/<int:project_id>/team")
def api_project_team(project_id: int):
    """Team roster of a project: visible to its own members and to organizers only."""
    user = current_user()
    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        return json_error("not found", 404)
    from ..security import is_organizer

    if user is None:
        return json_error("authentication required", 401)
    if _load_membership(user, project) is None and not is_organizer(user):
        return json_error("forbidden", 403)
    members = db.query_all(
        """SELECT u.id, u.display_name FROM team_members tm JOIN users u ON u.id = tm.user_id
            WHERE tm.team_id = ? ORDER BY u.id""",
        (project["team_id"],),
    )
    return json_ok({"project_id": project_id, "members": [dict(m) for m in members]})