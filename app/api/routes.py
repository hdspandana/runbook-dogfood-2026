"""Machine-readable API surface.

Two audiences:

* **public** (`/api/gallery*`, `/api/health`) — never contains judging data;
* **organizer** (`/api/events/<id>/...`) — aggregates, results, normalization and
  publishing, all behind `organizer_required`.

Everything else (judges, participants, teams, projects) lives next to its domain.
"""

from __future__ import annotations

from flask import Blueprint, abort, request

from .. import db
from ..events import service as events_service
from ..gallery.routes import public_projects
from ..judging import normalization, service as judging
from ..security import organizer_required
from ..util import json_error, json_ok, now_utc, pretty_utc, to_iso

bp = Blueprint("api", __name__)


@bp.route("/api/health")
def health():
    return json_ok({
        "status": "ok",
        "service": "runbook",
        "server_time_utc": to_iso(now_utc()),
        "database": "sqlite",
        "events": db.query_scalar("SELECT COUNT(*) FROM events", (), 0),
    })


@bp.route("/api/gallery")
def gallery():
    """Public gallery feed. Submitted projects only, public fields only."""
    event_id = request.args.get("event", type=int)
    event = events_service.get_event(event_id) if event_id else events_service.primary_event()
    if event is None:
        return json_ok({"event": None, "projects": [], "count": 0})
    search = (request.args.get("q") or "").strip()
    track_id = request.args.get("track", type=int)
    projects = public_projects(event["id"], search, track_id)
    return json_ok({
        "event": {"id": event["id"], "name": event["name"], "slug": event["slug"],
                  "submission_deadline": event["submission_deadline"],
                  "phase": events_service.phase_label(event)},
        "count": len(projects),
        # Note the absent keys: no scores, no review status, no judge data, no
        # assignment metadata, no normalization internals.
        "projects": projects,
    })


@bp.route("/api/gallery/<int:project_id>")
def gallery_project(project_id: int):
    project = db.query_one(
        """SELECT p.id, p.name, p.tagline, p.description, p.repo_url, p.demo_url, p.web_url,
                  p.submitted_at, p.status, p.event_id, t.name AS track_name, tm.name AS team_name
             FROM projects p
             LEFT JOIN tracks t ON t.id = p.track_id
             LEFT JOIN teams tm ON tm.id = p.team_id
            WHERE p.id = ?""",
        (project_id,),
    )
    if project is None or project["status"] != "submitted":
        return json_error("not found", 404)
    return json_ok({"project": dict(project)})


# --------------------------------------------------------------------------
# organizer-scoped aggregates
# --------------------------------------------------------------------------
@bp.route("/api/events/<int:event_id>/progress")
@organizer_required
def progress(event_id: int):
    event = events_service.get_event(event_id)
    if event is None:
        return json_error("not found", 404)
    progress = judging.judging_progress(event_id)
    return json_ok({
        "event": {"id": event["id"], "name": event["name"], "phase": events_service.phase_label(event)},
        "submission_deadline": event["submission_deadline"],
        "progress": progress,
        "runbook": _runbook_view(event_id)["summary"],
    })


@bp.route("/api/events/<int:event_id>/submissions")
@organizer_required
def submissions(event_id: int):
    if events_service.get_event(event_id) is None:
        return json_error("not found", 404)
    rows = db.query_all(
        """SELECT p.*, tm.name AS team_name, t.name AS track_name,
                  (SELECT COUNT(*) FROM assignments a WHERE a.project_id = p.id) AS assignments,
                  (SELECT COUNT(*) FROM reviews r WHERE r.project_id = p.id AND r.status = 'complete') AS complete_reviews
             FROM projects p
             LEFT JOIN teams tm ON tm.id = p.team_id
             LEFT JOIN tracks t ON t.id = p.track_id
            WHERE p.event_id = ? ORDER BY p.id ASC""",
        (event_id,),
    )
    return json_ok({"event_id": event_id, "count": len(rows), "projects": [dict(r) for r in rows]})


@bp.route("/api/events/<int:event_id>/assignments", methods=["GET", "POST"])
@organizer_required
def assignments(event_id: int):
    if events_service.get_event(event_id) is None:
        return json_error("not found", 404)
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        per_project = max(1, min(5, int(payload.get("judges_per_project", 2))))
        result = judging.assign_round_robin(event_id, judges_per_project=per_project)
        result["progress"] = judging.judging_progress(event_id)
        return json_ok(result)
    return json_ok(judging.judging_progress(event_id))


@bp.route("/api/events/<int:event_id>/rubric")
def rubric(event_id: int):
    """The rubric is public: it is how projects are judged, not who scored what."""
    if events_service.get_event(event_id) is None:
        return json_error("not found", 404)
    criteria = judging.rubric_for_event(event_id)
    valid, message = judging.validate_rubric(criteria)
    return json_ok({
        "event_id": event_id,
        "criteria": [dict(c) for c in criteria],
        "valid": valid,
        "message": message,
        "max_raw_total": judging.rubric_max_total(criteria),
        "fingerprint": judging.rubric_fingerprint(event_id),
    })


@bp.route("/api/events/<int:event_id>/normalization")
@organizer_required
def normalization_state(event_id: int):
    if events_service.get_event(event_id) is None:
        return json_error("not found", 404)
    return json_ok({
        "freshness": normalization.freshness(event_id),
        "live_preview": normalization.compute(event_id),
        "stored": normalization.stored_result(event_id),
    })


@bp.route("/api/events/<int:event_id>/normalize", methods=["POST"])
@organizer_required
def normalize(event_id: int):
    if events_service.get_event(event_id) is None:
        return json_error("not found", 404)
    from ..security import current_user

    user = current_user()
    result = normalization.run_and_store(event_id, user["id"])
    return json_ok({
        "normalized": True,
        "review_count": result["review_count"],
        "project_count": result["project_count"],
        "mode": result["mode"],
        "generated_at": result["generated_at"],
        "fingerprint": result["fingerprint"],
        "freshness": normalization.freshness(event_id),
    })


@bp.route("/api/events/<int:event_id>/results")
@organizer_required
def results(event_id: int):
    event = events_service.get_event(event_id)
    if event is None:
        return json_error("not found", 404)
    stored = normalization.stored_result(event_id)
    freshness = normalization.freshness(event_id)
    from ..judging import publication
    published_state = publication.state(event, freshness)
    latest_run = normalization.latest_run(event_id)
    return json_ok({
        "event_id": event_id,
        "published_at": event["results_published_at"],
        "publication_state": published_state,
        "published_result_current": published_state == "current",
        "latest_run_id": latest_run["id"] if latest_run else None,
        "published_run_id": event["results_published_run_id"],
        "freshness": freshness,
        "results": stored,
        "ranking": (stored or {}).get("projects", []),
        "unranked": (stored or {}).get("unranked_projects", []),
    })


@bp.route("/api/events/<int:event_id>/publish", methods=["POST"])
@organizer_required
def publish(event_id: int):
    if events_service.get_event(event_id) is None:
        return json_error("not found", 404)
    from ..judging import publication

    timestamp, refusal = publication.publish(event_id)
    if refusal:
        return json_ok(refusal, 409)
    return json_ok({"published": True, "published_at": timestamp,
                    "normalization_state_at_publish": "current"})


@bp.route("/api/events")
def events():
    return json_ok({
        "events": [
            {
                "id": e["id"], "name": e["name"], "slug": e["slug"],
                "submission_deadline": e["submission_deadline"],
                "judging_starts_at": e["judging_starts_at"],
                "judging_ends_at": e["judging_ends_at"],
                "results_published_at": e["results_published_at"],
                "phase": events_service.phase_label(e),
                "counts": events_service.counts(e["id"]),
            }
            for e in events_service.all_events()
        ]
    })


def _runbook_view(event_id: int) -> dict:
    from ..runbook import service as runbook

    return runbook.build(event_id)