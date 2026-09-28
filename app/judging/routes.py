"""Judge interface and every endpoint that can carry scoring information.

Identity rules enforced here (see tests/judging/):

* The current judge is always `current_user()`, i.e. the session cookie.
  Any `judge_id` sent by the client is treated as an *assertion to check*, never
  as a fact: if it disagrees with the session it is a 403, not an override.
* A judge may read a review only if `review.judge_id == session user id`.
* A judge may read a project only if an assignment row exists for that pair.
* A judge may read track-scoped protected data only for tracks they are assigned in.
* Organizers/admins may read everything in an event. Participants may read none.
* Denials carry no values: `{"error": "forbidden"}` and nothing else.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..events import service as events_service
from ..security import (current_user, deny, is_judge, is_organizer, judge_assignment,
                        judge_can_access_track, judge_track_scope, judging_open)
from ..util import json_error, json_ok, now_utc, parse_iso, pretty_utc, to_iso
from . import service as judging

bp = Blueprint("judge", __name__)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _judging_window_error(event):
    """Judging is locked outside its window (mirrors the submission deadline gate)."""
    now = now_utc()
    start = parse_iso(event["judging_starts_at"])
    end = parse_iso(event["judging_ends_at"])
    if start and now < start:
        return json_error("judging has not opened yet", 403,
                          detail=f"Judging opens at {pretty_utc(start)}.", server_time_utc=to_iso(now))
    if end and now >= end:
        return json_error("judging window closed", 403,
                          detail=f"Judging closed at {pretty_utc(end)}; scores are locked.",
                          server_time_utc=to_iso(now))
    return None


def _judge_review_payload(review, criteria) -> dict:
    scores = judging.scores_for_review(review["id"])
    rows = []
    for criterion in criteria:
        cid = int(criterion["id"])
        rows.append({
            "criterion_id": cid,
            "name": criterion["name"],
            "weight_pct": criterion["weight_pct"],
            "max_score": criterion["max_score"],
            "score": scores.get(cid),
            "contribution": round(scores.get(cid, 0) * criterion["weight_pct"] / 100.0, 4)
            if cid in scores else None,
        })
    return {
        "review_id": review["id"],
        "project_id": review["project_id"],
        "judge_id": review["judge_id"],
        "status": review["status"],
        "raw_total": review["raw_total"],
        "comment": review["comment"],
        "submitted_at": review["submitted_at"],
        "updated_at": review["updated_at"],
        "criteria": rows,
    }


# --------------------------------------------------------------------------
# judge queue
# --------------------------------------------------------------------------
@bp.route("/judge")
def index():
    user = current_user()
    if user is None:
        return deny("Sign in to review.", 401)
    if not is_judge(user) and not is_organizer(user):
        return deny("Judge access required.", 403)

    event_id = request.args.get("event", type=int)
    if is_organizer(user) and not event_id:
        event = events_service.primary_event()
    else:
        rows = db.query_all(
            "SELECT event_id, COUNT(*) c FROM assignments WHERE judge_id = ? GROUP BY event_id ORDER BY c DESC",
            (user["id"],),
        )
        if not rows and is_organizer(user):
            event = events_service.primary_event()
        elif not rows:
            event = None
        else:
            event = events_service.get_event(event_id or rows[0]["event_id"])
    if event is None:
        return render_template("judge_queue.html", event=None, queue=[], stats=None,
                               track_scope=[], events=events_service.all_events())

    queue = judging.judge_queue(user["id"], event["id"])
    complete = sum(1 for row in queue if row["review_status"] == "complete")
    stats = {
        "assigned": len(queue),
        "complete": complete,
        "remaining": len(queue) - complete,
        "percent": round(100.0 * complete / len(queue), 1) if queue else 0.0,
    }
    tracks = {row["track_id"]: row["track_name"] for row in queue}
    return render_template(
        "judge_queue.html",
        event=event,
        queue=queue,
        stats=stats,
        track_scope=[{"id": tid, "name": name} for tid, name in sorted(tracks.items(), key=lambda kv: (kv[0] is None, kv[0]))],
        events=events_service.all_events(),
        scoring_open=judging_open(event),
        window_end=pretty_utc(event["judging_ends_at"]),
    )


@bp.route("/judge/projects/<int:project_id>", methods=["GET", "POST"])
def review(project_id: int):
    user = current_user()
    if user is None:
        return deny("Sign in to review.", 401)
    if not is_judge(user) and not is_organizer(user):
        return deny("Judge access required.", 403)

    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None or project["status"] != "submitted":
        abort(404)

    assignment = judge_assignment(user, project) if is_judge(user) else None
    if is_judge(user) and assignment is None:
        # Assigned-only access. Being a judge in this event is not enough.
        return deny("You are not assigned to this project.", 403)

    event = events_service.get_event(project["event_id"])
    criteria = judging.rubric_for_event(event["id"])
    review_row = db.query_one(
        "SELECT * FROM reviews WHERE project_id = ? AND judge_id = ?", (project_id, user["id"])
    )
    if review_row is None:
        return deny("You have no review slot for this project.", 403)

    team = db.query_one("SELECT * FROM teams WHERE id = ?", (project["team_id"],))
    track = db.query_one("SELECT * FROM tracks WHERE id = ?", (project["track_id"],))

    if request.method == "POST":
        window_error = _judging_window_error(event)
        if window_error is not None:
            return window_error
        scores = {}
        for criterion in criteria:
            raw = request.form.get(f"score_{criterion['id']}")
            if raw not in (None, ""):
                try:
                    scores[int(criterion["id"])] = float(raw)
                except ValueError:
                    return deny("Scores must be numbers.", 400)
        intent = request.form.get("intent", "save")
        result = judging.save_review(review_row, criteria, scores, request.form.get("comment", ""),
                                     submit=(intent == "submit"))
        if not result["ok"]:
            return render_template("judge_review.html", project=project, event=event, team=team,
                                   track=track, criteria=criteria, review=review_row,
                                   scores=scores, errors=result["errors"],
                                   mode="review", scoring_open=judging_open(event)), 400
        flash("Review saved." if intent != "submit" else "Review submitted.", "ok")
        return redirect(url_for("judge.index", event=event["id"]))

    scores = judging.scores_for_review(review_row["id"])
    return render_template("judge_review.html", project=project, event=event, team=team, track=track,
                           criteria=criteria, review=review_row, scores=scores, mode="review",
                           scoring_open=judging_open(event))


# --------------------------------------------------------------------------
# JSON endpoints (these are what the security matrix hammers)
# --------------------------------------------------------------------------
@bp.route("/api/judging/assignments")
def api_assignments():
    user = current_user()
    if user is None:
        return json_error("authentication required", 401)
    if not is_judge(user):
        return json_error("forbidden", 403)
    event_id = request.args.get("event", type=int)
    rows = db.query_all(
        """SELECT a.id AS assignment_id, a.event_id, a.project_id, a.track_id, p.name AS project,
                  r.id AS review_id, r.status AS review_status
             FROM assignments a JOIN projects p ON p.id = a.project_id
             JOIN judge_invites i ON i.event_id = a.event_id AND i.accepted_by = a.judge_id
             LEFT JOIN reviews r ON r.project_id = a.project_id AND r.judge_id = a.judge_id
            WHERE a.judge_id = ? AND (? IS NULL OR a.event_id = ?)
              AND a.event_id = p.event_id AND a.track_id = p.track_id
              AND (i.track_id IS NULL OR i.track_id = p.track_id)
            ORDER BY a.project_id ASC""",
        (user["id"], event_id, event_id),
    )
    return json_ok({"judge_id": user["id"], "assignments": [dict(r) for r in rows]})


@bp.route("/api/judging/reviews/<int:review_id>")
def api_review(review_id: int):
    """Read one review.

    The only identity consulted is the session. `?judge_id=` is accepted as an
    assertion and compared against the session identity; a mismatch is a 403 so
    a caller cannot probe for the existence of another judge's scores.
    """
    user = current_user()
    if user is None:
        return json_error("authentication required", 401)
    if not is_judge(user) and not is_organizer(user):
        return json_error("forbidden", 403)

    asserted = request.args.get("judge_id", type=int)
    if asserted is not None and asserted != user["id"] and not is_organizer(user):
        return json_error("forbidden", 403)

    review = db.query_one("SELECT * FROM reviews WHERE id = ?", (review_id,))
    if review is None:
        return json_error("forbidden", 403) if not is_organizer(user) else json_error("not found", 404)

    if not is_organizer(user):
        project = db.query_one("SELECT * FROM projects WHERE id = ?", (review["project_id"],))
        if review["judge_id"] != user["id"] or judge_assignment(user, project) is None:
            # Judge A asking for Judge B's review, a participant, or a review
            # whose assignment is no longer valid: same answer, no values.
            return json_error("forbidden", 403)

    event = events_service.get_event(review["event_id"])
    criteria = judging.rubric_for_event(review["event_id"])
    payload = _judge_review_payload(review, criteria)
    payload["event_id"] = review["event_id"]
    payload["event"] = event["name"] if event else ""
    payload["judging_window_open"] = judging_open(event) if event else False
    return json_ok(payload)


@bp.route("/api/judging/projects/<int:project_id>/scores")
def api_project_scores(project_id: int):
    """A judge's own review of a project.

    Judges: only their own (asserted judge_id must match the session).
    Organizers: any judge's, by explicit `judge_id`.
    Participants and visitors: always 403/401.
    """
    user = current_user()
    if user is None:
        return json_error("authentication required", 401)

    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        return json_error("not found", 404)

    asserted = request.args.get("judge_id", type=int)

    if is_judge(user):
        if asserted is not None and asserted != user["id"]:
            return json_error("forbidden", 403)
        if judge_assignment(user, project) is None:
            return json_error("forbidden", 403)
        judge_id = user["id"]
    elif is_organizer(user):
        judge_id = asserted
        if judge_id is None:
            return json_ok({"project_id": project_id, "reviews": _organizer_project_reviews(project_id)})
    else:
        return json_error("forbidden", 403)

    review = db.query_one("SELECT * FROM reviews WHERE project_id = ? AND judge_id = ?", (project_id, judge_id))
    if review is None:
        return json_error("forbidden", 403)
    if is_judge(user) and review["judge_id"] != user["id"]:
        return json_error("forbidden", 403)
    criteria = judging.rubric_for_event(project["event_id"])
    return json_ok(_judge_review_payload(review, criteria))


def _organizer_project_reviews(project_id: int) -> list[dict]:
    rows = db.query_all(
        """SELECT r.*, u.display_name AS judge_name FROM reviews r
             JOIN users u ON u.id = r.judge_id WHERE r.project_id = ? ORDER BY r.judge_id""",
        (project_id,),
    )
    return [
        {
            "review_id": r["id"],
            "judge_id": r["judge_id"],
            "judge": r["judge_name"],
            "status": r["status"],
            "raw_total": r["raw_total"],
            "comment": r["comment"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


@bp.route("/api/judging/projects/<int:project_id>/review", methods=["POST"])
def api_save_review(project_id: int):
    user = current_user()
    if user is None:
        return json_error("authentication required", 401)
    if not is_judge(user):
        return json_error("forbidden", 403)

    project = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None or project["status"] != "submitted":
        return json_error("not found", 404)
    if judge_assignment(user, project) is None:
        return json_error("forbidden", 403)

    event = events_service.get_event(project["event_id"])
    window_error = _judging_window_error(event)
    if window_error is not None:
        return window_error

    payload = request.get_json(silent=True) or {}
    if payload.get("judge_id") is not None:
        try:
            asserted_id = int(payload["judge_id"])
        except (TypeError, ValueError):
            return json_error("forbidden", 403)
        if asserted_id != user["id"]:
            # Attempt to write a score as somebody else.
            return json_error("forbidden", 403)

    criteria = judging.rubric_for_event(event["id"])
    incoming = payload.get("scores") or {}
    if not isinstance(incoming, dict):
        return json_error("scores must be an object keyed by criterion id", 400)
    try:
        scores = {int(k): float(v) for k, v in incoming.items()}
    except (TypeError, ValueError):
        return json_error("scores must be numeric", 400)
    review_row = judging.get_or_create_review(event["id"], project_id, user["id"])
    if review_row is None:
        return json_error("forbidden", 403)
    result = judging.save_review(review_row, criteria, scores, payload.get("comment", ""),
                                submit=bool(payload.get("submit", True)))
    if not result["ok"]:
        return json_error("validation failed", 400, errors=result["errors"])
    refreshed = db.query_one("SELECT * FROM reviews WHERE id = ?", (review_row["id"],))
    return json_ok({"saved": True, "review": _judge_review_payload(refreshed, criteria)})


@bp.route("/api/judging/tracks/<int:track_id>/progress")
def api_track_progress(track_id: int):
    """Track-scoped protected judging data.

    A judge sees only tracks they hold assignments in; a Track A judge asking
    about Track B is rejected even though both tracks belong to the same event.
    """
    user = current_user()
    if user is None:
        return json_error("authentication required", 401)
    track = db.query_one("SELECT * FROM tracks WHERE id = ?", (track_id,))
    if track is None:
        return json_error("not found", 404)

    if is_organizer(user):
        pass
    elif is_judge(user):
        if not judge_can_access_track(user, track["event_id"], track_id):
            return json_error("forbidden", 403)
    else:
        return json_error("forbidden", 403)

    required = db.query_scalar(
        """SELECT COUNT(*) FROM assignments a JOIN projects p ON p.id = a.project_id
            WHERE a.track_id = ? AND p.status = 'submitted'""", (track_id,), 0)
    if is_organizer(user):
        complete = db.query_scalar(
            """SELECT COUNT(*) FROM reviews r JOIN projects p ON p.id = r.project_id
                WHERE p.track_id = ? AND r.status = 'complete' AND p.status = 'submitted'""",
            (track_id,), 0)
    else:
        complete = db.query_scalar(
            """SELECT COUNT(*) FROM reviews r JOIN projects p ON p.id = r.project_id
                WHERE p.track_id = ? AND r.judge_id = ? AND r.status = 'complete' AND p.status = 'submitted'""",
            (track_id, user["id"]), 0)
        required = db.query_scalar(
            """SELECT COUNT(*) FROM assignments a JOIN projects p ON p.id = a.project_id
                WHERE a.track_id = ? AND a.judge_id = ? AND p.status = 'submitted'""",
            (track_id, user["id"]), 0)
    return json_ok({
        "track_id": track_id,
        "track": track["name"],
        "event_id": track["event_id"],
        "scope": "organizer" if is_organizer(user) else "own_assignments_only",
        "required_reviews": required,
        "complete_reviews": complete,
        # Deliberately no per-project scores and no other judge's progress:
        # a judge must not be able to infer peer scores from "harmless" stats.
    })


@bp.route("/api/judging/scope")
def api_scope():
    """Self-describing authorization scope for the current session."""
    user = current_user()
    if user is None:
        return json_ok({"role": "visitor", "tracks": [], "assignments": 0})
    if is_judge(user):
        events = db.query_all("SELECT DISTINCT event_id FROM assignments WHERE judge_id = ?", (user["id"],))
        scope = {}
        for row in events:
            scope[str(row["event_id"])] = sorted(judge_track_scope(user, row["event_id"]))
        return json_ok({
            "role": "judge",
            "judge_id": user["id"],
            "track_scope": scope,
            "assignments": db.query_scalar("SELECT COUNT(*) FROM assignments WHERE judge_id = ?",
                                           (user["id"],), 0),
        })
    return json_ok({"role": user["role"], "judge_id": None})