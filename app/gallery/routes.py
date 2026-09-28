"""Public, unauthenticated surface: home, event pages, project gallery.

Hard rule: nothing on these pages is derived from reviews, assignments or
normalization. The gallery is built from `projects`, `teams` and `tracks` only,
and only for rows with ``status = 'submitted'``.
"""

from __future__ import annotations

from flask import Blueprint, abort, render_template, request

from .. import db
from ..events import service as events_service
from ..util import now_utc

bp = Blueprint("gallery", __name__)


def public_projects(event_id: int, search: str = "", track_id: int | None = None) -> list[dict]:
    sql = [
        """SELECT p.id, p.name, p.tagline, p.description, p.repo_url, p.demo_url, p.web_url,
                  p.submitted_at, p.track_id,
                  t.name AS track_name, t.slug AS track_slug, tm.name AS team_name, tm.id AS team_id
             FROM projects p
             LEFT JOIN tracks t ON t.id = p.track_id
             LEFT JOIN teams tm ON tm.id = p.team_id
            WHERE p.event_id = ? AND p.status = 'submitted'"""
    ]
    params: list = [event_id]
    if track_id:
        sql.append("AND p.track_id = ?")
        params.append(track_id)
    if search:
        sql.append(
            "AND (LOWER(p.name) LIKE ? OR LOWER(p.tagline) LIKE ? OR LOWER(p.description) LIKE ?"
            " OR LOWER(tm.name) LIKE ? OR LOWER(COALESCE(t.name,'')) LIKE ?)"
        )
        needle = f"%{search.lower()}%"
        params.extend([needle] * 5)
    sql.append("ORDER BY p.name COLLATE NOCASE ASC")
    return [dict(row) for row in db.query_all(" ".join(sql), params)]


@bp.route("/")
def home():
    event = events_service.primary_event()
    if event is None:
        return render_template("home.html", event=None, tracks=[], prizes=[], counts={}, projects=[])
    counts = events_service.counts(event["id"])
    return render_template(
        "home.html",
        event=event,
        tracks=events_service.tracks_for(event["id"]),
        prizes=events_service.prizes_for(event["id"]),
        counts=counts,
        projects=public_projects(event["id"])[:6],
        phase=events_service.phase_label(event),
        timing=events_service.timing_summary(event),
    )


@bp.route("/events")
def event_list():
    events = []
    for event in events_service.all_events():
        events.append({"event": event, "counts": events_service.counts(event["id"]),
                       "phase": events_service.phase_label(event)})
    return render_template("events.html", events=events)


@bp.route("/events/<int:event_id>")
def event_detail(event_id: int):
    event = events_service.get_event(event_id)
    if event is None:
        abort(404)
    return render_template(
        "event_detail.html",
        event=event,
        tracks=events_service.tracks_for(event_id),
        prizes=events_service.prizes_for(event_id),
        counts=events_service.counts(event_id),
        phase=events_service.phase_label(event),
        timing=events_service.timing_summary(event),
        judged_projects=events_service.judged_projects_public(event_id)[:8],
    )


@bp.route("/gallery")
def gallery():
    event_id = request.args.get("event", type=int)
    event = events_service.get_event(event_id) if event_id else events_service.primary_event()
    if event is None:
        return render_template("gallery.html", event=None, projects=[], tracks=[], search="", track_id=None)

    search = (request.args.get("q") or "").strip()
    track_id = request.args.get("track", type=int)
    projects = public_projects(event["id"], search, track_id)
    return render_template(
        "gallery.html",
        event=event,
        projects=projects,
        tracks=events_service.tracks_for(event["id"]),
        search=search,
        track_id=track_id,
        total=len(projects),
    )


@bp.route("/gallery/<int:project_id>")
def project_detail(project_id: int):
    project = db.query_one(
        """SELECT p.*, t.name AS track_name, tm.name AS team_name,
                  e.name AS event_name, e.slug AS event_slug, e.submission_deadline, e.id AS event_id
             FROM projects p
             LEFT JOIN tracks t ON t.id = p.track_id
             LEFT JOIN teams tm ON tm.id = p.team_id
             LEFT JOIN events e ON e.id = p.event_id
            WHERE p.id = ?""",
        (project_id,),
    )
    # A draft is not a public submission: it does not exist as far as a visitor
    # is concerned, and neither do projects belonging to an unpublished event.
    if project is None or project["status"] != "submitted":
        abort(404)
    event = events_service.get_event(project["event_id"])
    prizes = [p for p in events_service.prizes_for(project["event_id"])
              if p["track_id"] in (None, project["track_id"])]
    from ..judging import publication
    return render_template(
        "project_detail.html",
        project=project,
        event=event,
        publication_state=publication.state(event) if event else "not_published",
        timing=events_service.timing_summary(event) if event else None,
        prizes=prizes,
    )