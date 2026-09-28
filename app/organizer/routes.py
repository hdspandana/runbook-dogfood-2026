"""Organizer / admin surface: event configuration, judging setup, results.

Admin is implemented as an explicit capability superset of organizer (see
`security.roles_of`), plus system-level management where it matters:
admin can see every event, invite judges, run normalization and publish.
There is deliberately no separate admin dashboard — one operational dashboard
that both roles can use is easier to keep correct.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..auth import service as auth_service
from ..events import service as events_service
from ..judging import normalization, service as judging
from ..security import current_user, deny, is_organizer, organizer_required
from ..util import json_error, json_ok, new_token, now_utc, parse_iso, parse_form_datetime, to_iso

bp = Blueprint("organizer", __name__)

EVENT_FIELDS = ("name", "slug", "tagline", "location", "timezone_label", "starts_at",
                "submission_deadline", "judging_starts_at", "judging_ends_at")


def _event_or_404(event_id: int):
    event = events_service.get_event(event_id)
    if event is None:
        abort(404)
    return event


@bp.route("/organizer")
def index():
    user = current_user()
    if user is None:
        return deny("Sign in as an organizer.", 401)
    if not is_organizer(user):
        return deny("Organizer access required.", 403)
    return redirect(url_for("runbook.dashboard"))


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------
@bp.route("/organizer/events/new", methods=["GET", "POST"])
@organizer_required
def event_new():
    if request.method == "GET":
        return render_template("organizer_event_form.html", event=None, errors=[], values={})

    values = {key: request.form.get(key, "") for key in EVENT_FIELDS}
    errors = _validate_event(values, existing=None)
    track_names = [line.strip() for line in (request.form.get("tracks") or "").splitlines() if line.strip()]
    prize_names = [line.strip() for line in (request.form.get("prizes") or "").splitlines() if line.strip()]
    if not track_names:
        errors.append("Define at least one track.")
    if not prize_names:
        errors.append("Define at least one prize.")
    if errors:
        return render_template("organizer_event_form.html", event=None, errors=errors, values=values), 400

    stamp = to_iso(now_utc())
    user = current_user()
    with db.transaction(db.get_db()):
        event_id = db.insert(
            """INSERT INTO events (name, slug, tagline, location, timezone_label, starts_at,
                                   submission_deadline, judging_starts_at, judging_ends_at,
                                   results_published_at, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (values["name"], values["slug"], values["tagline"], values["location"],
             values["timezone_label"] or "UTC", to_iso(parse_form_datetime(values["starts_at"])),
             to_iso(parse_form_datetime(values["submission_deadline"])),
             to_iso(parse_form_datetime(values["judging_starts_at"])),
             to_iso(parse_form_datetime(values["judging_ends_at"])),
             user["id"], stamp),
        )
        for order, name in enumerate(track_names):
            db.insert(
                "INSERT INTO tracks (event_id, name, slug, description, sort_order) VALUES (?, ?, ?, '', ?)",
                (event_id, name, _slugify(name), order),
            )
        for order, name in enumerate(prize_names):
            db.insert(
                "INSERT INTO prizes (event_id, track_id, name, description, rank) VALUES (?, NULL, ?, '', ?)",
                (event_id, name, order + 1),
            )
        # every new event starts with the standard four-criterion rubric, which
        # the organizer can then edit.
        for order, (name, weight, description) in enumerate(_DEFAULT_RUBRIC):
            db.insert(
                """INSERT INTO rubric_criteria (event_id, name, description, weight_pct, max_score, sort_order)
                   VALUES (?, ?, ?, ?, 10, ?)""",
                (event_id, name, description, weight, order),
            )
    flash("Event created.", "ok")
    return redirect(url_for("organizer.event_edit", event_id=event_id))


@bp.route("/organizer/events/<int:event_id>/edit", methods=["GET", "POST"])
@organizer_required
def event_edit(event_id: int):
    event = _event_or_404(event_id)
    if request.method == "GET":
        return render_template("organizer_event_form.html", event=event, errors=[], values=dict(event))

    values = {key: request.form.get(key, "") for key in EVENT_FIELDS}
    errors = _validate_event(values, existing=event)
    if errors:
        return render_template("organizer_event_form.html", event=event, errors=errors, values=values), 400

    db.execute(
        """UPDATE events SET name = ?, slug = ?, tagline = ?, location = ?, timezone_label = ?,
                             starts_at = ?, submission_deadline = ?, judging_starts_at = ?,
                             judging_ends_at = ?
           WHERE id = ?""",
        (values["name"], values["slug"], values["tagline"], values["location"],
         values["timezone_label"] or "UTC", to_iso(parse_form_datetime(values["starts_at"])),
         to_iso(parse_form_datetime(values["submission_deadline"])),
         to_iso(parse_form_datetime(values["judging_starts_at"])),
         to_iso(parse_form_datetime(values["judging_ends_at"])), event_id),
    )
    flash("Event updated.", "ok")
    return redirect(url_for("organizer.event_edit", event_id=event_id))


_DEFAULT_RUBRIC = [
    ("Innovation", 25, "Does this do something meaningfully new, or measurably better?"),
    ("Execution", 30, "Does it work? Is it engineered rather than demonstrated?"),
    ("Impact", 25, "Who benefits, and by how much, and how soon?"),
    ("Presentation", 20, "Can a stranger understand the project and its evidence?"),
]


def _slugify(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "track"


def _validate_event(values: dict, existing) -> list[str]:
    errors: list[str] = []
    if not (values.get("name") or "").strip():
        errors.append("Event name is required.")
    if not (values.get("slug") or "").strip():
        errors.append("Event slug is required.")
    else:
        clash = db.query_one("SELECT id FROM events WHERE slug = ?", (values["slug"].strip(),))
        if clash and (existing is None or clash["id"] != existing["id"]):
            errors.append("That slug is already used by another event.")
    starts = parse_form_datetime(values.get("starts_at"))
    deadline = parse_form_datetime(values.get("submission_deadline"))
    j_start = parse_form_datetime(values.get("judging_starts_at"))
    j_end = parse_form_datetime(values.get("judging_ends_at"))
    if not starts:
        errors.append("Start must be an ISO-ish datetime (YYYY-MM-DDTHH:MM, interpreted as UTC).")
    if not deadline:
        errors.append("Submission deadline must be a datetime (YYYY-MM-DDTHH:MM UTC).")
    if not j_start or not j_end:
        errors.append("Judging window start and end are required.")
    if starts and deadline and deadline <= starts:
        errors.append("Submission deadline must be after the event start.")
    if deadline and j_start and j_start < deadline:
        errors.append("Judging must not start before the submission deadline.")
    if j_end and j_start and j_end < j_start:
        errors.append("Judging must not end before it starts.")
    if existing is not None and existing["results_published_at"]:
        if any(to_iso(dt) != existing[key] for key, dt in
               (("starts_at", starts), ("submission_deadline", deadline),
                ("judging_starts_at", j_start), ("judging_ends_at", j_end)) if dt):
            errors.append("A published event's schedule cannot be changed.")
    return errors


# --------------------------------------------------------------------------
# tracks & prizes
# --------------------------------------------------------------------------
@bp.route("/organizer/events/<int:event_id>/tracks", methods=["GET", "POST"])
@organizer_required
def tracks(event_id: int):
    event = _event_or_404(event_id)
    if request.method == "POST":
        action = request.form.get("action", "track")
        if action == "track":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Track name is required.", "warn")
            else:
                next_order = db.query_scalar(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM tracks WHERE event_id = ?", (event_id,), 0)
                db.execute(
                    "INSERT OR IGNORE INTO tracks (event_id, name, slug, description, sort_order) VALUES (?, ?, ?, ?, ?)",
                    (event_id, name, _slugify(name), (request.form.get("description") or "").strip(), next_order),
                )
                flash(f"Track “{name}” added.", "ok")
        elif action == "prize":
            name = (request.form.get("prize_name") or "").strip()
            track_id = request.form.get("prize_track", type=int)
            if not name:
                flash("Prize name is required.", "warn")
            elif track_id and not db.query_one(
                "SELECT id FROM tracks WHERE id = ? AND event_id = ?", (track_id, event_id)
            ):
                flash("That prize track does not belong to this event.", "warn")
            else:
                db.execute(
                    "INSERT OR IGNORE INTO prizes (event_id, track_id, name, description, rank) VALUES (?, ?, ?, ?, ?)",
                    (event_id, track_id or None, name, (request.form.get("prize_description") or "").strip(),
                     request.form.get("prize_rank", type=int)),
                )
                flash(f"Prize “{name}” added.", "ok")
        return redirect(url_for("organizer.tracks", event_id=event_id))

    track_counts = {
        int(row["track_id"]): row["c"]
        for row in db.query_all(
            "SELECT track_id, COUNT(*) c FROM projects WHERE event_id = ? GROUP BY track_id", (event_id,)
        )
    }
    return render_template("organizer_tracks.html", event=event,
                           tracks=events_service.tracks_for(event_id),
                           prizes=events_service.prizes_for(event_id),
                           track_counts=track_counts)


@bp.route("/organizer/events/<int:event_id>/tracks/<int:track_id>/delete", methods=["POST"])
@organizer_required
def track_delete(event_id: int, track_id: int):
    _event_or_404(event_id)
    in_use = db.query_scalar("SELECT COUNT(*) FROM projects WHERE track_id = ? AND event_id = ?",
                             (track_id, event_id), 0)
    if in_use:
        flash(f"Cannot remove track: {in_use} project(s) belong to it. Move them first.", "warn")
        return redirect(url_for("organizer.tracks", event_id=event_id))
    db.execute("DELETE FROM tracks WHERE id = ? AND event_id = ?", (track_id, event_id))
    flash("Track removed.", "ok")
    return redirect(url_for("organizer.tracks", event_id=event_id))


# --------------------------------------------------------------------------
# rubric
# --------------------------------------------------------------------------
@bp.route("/organizer/events/<int:event_id>/rubric", methods=["GET", "POST"])
@organizer_required
def rubric(event_id: int):
    event = _event_or_404(event_id)
    criteria = judging.rubric_for_event(event_id)

    if request.method == "POST":
        # The rubric must be configured *before* scoring. Deleting or changing
        # criterion IDs after reviews exist would cascade-delete score rows and
        # undermine judging integrity; never let an organizer do that silently.
        existing_reviews = db.query_scalar(
            "SELECT COUNT(*) FROM reviews WHERE event_id = ? AND status = 'complete'", (event_id,), 0
        )
        if existing_reviews:
            flash("Rubric is locked after the first completed review. Create a new event/rubric for a different scoring model.", "warn")
            return redirect(url_for("organizer.rubric", event_id=event_id))
        with db.transaction(db.get_db()):
            for criterion in criteria:
                prefix = f"criterion_{criterion['id']}_"
                if f"{prefix}name" not in request.form:
                    continue
                name = (request.form.get(f"{prefix}name") or "").strip()
                weight = request.form.get(f"{prefix}weight", type=float)
                maximum = request.form.get(f"{prefix}max_score", type=float) or 10.0
                description = (request.form.get(f"{prefix}description") or "").strip()
                if request.form.get(f"{prefix}delete"):
                    db.execute("DELETE FROM rubric_criteria WHERE id = ? AND event_id = ?",
                               (criterion["id"], event_id))
                    continue
                if not name:
                    continue
                db.execute(
                    """UPDATE rubric_criteria SET name = ?, description = ?, weight_pct = ?, max_score = ?
                        WHERE id = ? AND event_id = ?""",
                    (name, description, weight if weight is not None else 0.0, maximum,
                     criterion["id"], event_id),
                )
            new_name = (request.form.get("new_name") or "").strip()
            if new_name:
                db.execute(
                    """INSERT OR IGNORE INTO rubric_criteria
                       (event_id, name, description, weight_pct, max_score, sort_order)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (event_id, new_name, (request.form.get("new_description") or "").strip(),
                     request.form.get("new_weight", type=float) or 0.0,
                     request.form.get("new_max_score", type=float) or 10.0,
                     db.query_scalar("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM rubric_criteria WHERE event_id = ?",
                                     (event_id,), 0)),
                )

        criteria = judging.rubric_for_event(event_id)
        valid, message = judging.validate_rubric(criteria)
        flash(message, "ok" if valid else "warn")
        return redirect(url_for("organizer.rubric", event_id=event_id))

    valid, message = judging.validate_rubric(criteria)
    return render_template("organizer_rubric.html", event=event, criteria=criteria,
                           valid=valid, message=message,
                           max_total=judging.rubric_max_total(criteria),
                           fingerprint=judging.rubric_fingerprint(event_id))


# --------------------------------------------------------------------------
# judges & invites
# --------------------------------------------------------------------------
@bp.route("/organizer/events/<int:event_id>/judges", methods=["GET", "POST"])
@organizer_required
def judges(event_id: int):
    event = _event_or_404(event_id)
    user = current_user()
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        display_name = (request.form.get("display_name") or "").strip()
        track_id = request.form.get("track_id", type=int)
        if "@" not in email:
            flash("Enter a valid email address.", "warn")
            return redirect(url_for("organizer.judges", event_id=event_id))
        if track_id and not db.query_one("SELECT id FROM tracks WHERE id = ? AND event_id = ?", (track_id, event_id)):
            flash("That judge track does not belong to this event.", "warn")
            return redirect(url_for("organizer.judges", event_id=event_id))

        existing_user = auth_service.find_user_by_email(email)
        if existing_user is not None and existing_user["role"] not in ("judge", "admin"):
            flash("That email belongs to a non-judge account.", "warn")
            return redirect(url_for("organizer.judges", event_id=event_id))

        already = db.query_one("SELECT * FROM judge_invites WHERE event_id = ? AND email = ?", (event_id, email))
        if already and already["accepted_by"]:
            flash("That judge already accepted an invite for this event.", "warn")
            return redirect(url_for("organizer.judges", event_id=event_id))

        token = new_token(24)
        db.execute(
            """INSERT OR IGNORE INTO judge_invites
               (event_id, email, token, track_id, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, email, token, track_id or None, user["id"], to_iso(now_utc())),
        )
        if already:
            db.execute("UPDATE judge_invites SET token = ?, track_id = ? WHERE id = ?",
                       (token, track_id or None, already["id"]))
        if existing_user is not None:
            # A judge account that already exists is attached to the event straight away.
            db.execute(
                "UPDATE judge_invites SET accepted_by = ?, accepted_at = ? WHERE event_id = ? AND email = ?",
                (existing_user["id"], to_iso(now_utc()), event_id, email),
            )
            flash(f"{email} can now be assigned in this event.", "ok")
        else:
            flash(f"Invite created for {email}. No email is sent — share the link.", "ok")
        return redirect(url_for("organizer.judges", event_id=event_id))

    invites = db.query_all(
        """SELECT i.*, t.name AS track_name, u.display_name AS accepted_name
             FROM judge_invites i
             LEFT JOIN tracks t ON t.id = i.track_id
             LEFT JOIN users u ON u.id = i.accepted_by
            WHERE i.event_id = ? ORDER BY i.id ASC""",
        (event_id,),
    )
    judge_rows = db.query_all(
        """SELECT u.*, COUNT(a.id) AS assignments
             FROM users u
             JOIN judge_invites i ON i.accepted_by = u.id AND i.event_id = ?
             LEFT JOIN assignments a ON a.judge_id = u.id AND a.event_id = ?
            WHERE u.role = 'judge' GROUP BY u.id ORDER BY u.id ASC""",
        (event_id, event_id),
    )
    return render_template("organizer_judges.html", event=event, invites=invites, judges=judge_rows,
                           tracks=events_service.tracks_for(event_id))


@bp.route("/judge-invite/<token>", methods=["GET", "POST"])
def judge_invite(token: str):
    """Self-hosted recruitment: an organizer-issued link, no email infrastructure."""
    invite = db.query_one("SELECT * FROM judge_invites WHERE token = ?", (token,))
    if invite is None:
        return render_template("error.html", code=404, message="That judge invite is invalid."), 404
    event = events_service.get_event(invite["event_id"])

    track = db.query_one("SELECT * FROM tracks WHERE id = ?", (invite["track_id"],)) if invite["track_id"] else None
    if request.method == "GET":
        return render_template("judge_invite.html", invite=invite, event=event, track=track,
                               has_account=auth_service.find_user_by_email(invite["email"]) is not None)

    user = current_user()
    if user is not None:
        if user["role"] != "judge" or user["email"].lower() != invite["email"].lower():
            return deny("This invite is for the named judge account only.", 403)
        db.execute("UPDATE judge_invites SET accepted_by = ?, accepted_at = ? WHERE id = ?",
                   (user["id"], to_iso(now_utc()), invite["id"]))
        flash("Invite accepted. Your assigned projects are listed below.", "ok")
        return redirect(url_for("judge.index", event=event["id"]))

    password = request.form.get("password") or ""
    if len(password) < 8:
        return render_template("judge_invite.html", invite=invite, event=event, track=track, has_account=False,
                               error="Choose a password of at least 8 characters."), 400
    existing = auth_service.find_user_by_email(invite["email"])
    if existing is None:
        user_id = auth_service.create_user(invite["email"], password,
                                           request.form.get("display_name") or invite["email"], "judge")
    else:
        if not auth_service.check_password_hash(existing["password_hash"], password):
            return deny("An account with that email already exists.", 409)
        user_id = existing["id"]
    db.execute("UPDATE judge_invites SET accepted_by = ?, accepted_at = ? WHERE id = ?",
               (user_id, to_iso(now_utc()), invite["id"]))
    session_token = auth_service.create_session(user_id, request.headers.get("User-Agent"))
    response = redirect(url_for("judge.index", event=event["id"]))
    return auth_service.set_session_cookie(response, session_token)


# --------------------------------------------------------------------------
# assignments
# --------------------------------------------------------------------------
@bp.route("/organizer/events/<int:event_id>/assignments", methods=["GET", "POST"])
@organizer_required
def assignments(event_id: int):
    event = _event_or_404(event_id)
    if request.method == "POST":
        per_project = max(1, min(5, request.form.get("judges_per_project", type=int) or 2))
        result = judging.assign_round_robin(event_id, judges_per_project=per_project)
        flash(f"Round-robin assignment complete: {result['created']} new assignment(s).", "ok")
        if result["tracks_skipped"]:
            flash(f"{len(result['tracks_skipped'])} project(s) had no eligible judge in their track.", "warn")
        return redirect(url_for("organizer.assignments", event_id=event_id))

    progress = judging.judging_progress(event_id)
    rows = db.query_all(
        """SELECT a.id, a.project_id, p.name AS project_name, t.name AS track_name, a.track_id,
                  u.display_name AS judge_name, u.email AS judge_email, u.id AS judge_id,
                  r.status AS review_status, r.raw_total
             FROM assignments a
             JOIN projects p ON p.id = a.project_id
             LEFT JOIN tracks t ON t.id = p.track_id
             JOIN users u ON u.id = a.judge_id
             LEFT JOIN reviews r ON r.project_id = a.project_id AND r.judge_id = a.judge_id
            WHERE a.event_id = ?
            ORDER BY p.id ASC, u.id ASC""",
        (event_id,),
    )
    return render_template("organizer_assignments.html", event=event, rows=rows, progress=progress,
                           judges=db.query_all("SELECT id, display_name, email FROM users WHERE role = 'judge' ORDER BY id"),
                           elapsed=None)


# --------------------------------------------------------------------------
# submissions & progress
# --------------------------------------------------------------------------
@bp.route("/organizer/events/<int:event_id>/submissions")
@organizer_required
def submissions(event_id: int):
    event = _event_or_404(event_id)
    rows = db.query_all(
        """SELECT p.*, tm.name AS team_name, t.name AS track_name,
                  (SELECT COUNT(*) FROM assignments a WHERE a.project_id = p.id) AS assignments,
                  (SELECT COUNT(*) FROM reviews r WHERE r.project_id = p.id AND r.status = 'complete') AS complete_reviews
             FROM projects p
             LEFT JOIN teams tm ON tm.id = p.team_id
             LEFT JOIN tracks t ON t.id = p.track_id
            WHERE p.event_id = ? ORDER BY p.status DESC, p.id ASC""",
        (event_id,),
    )
    return render_template("organizer_submissions.html", event=event, rows=rows,
                           counts=events_service.counts(event_id),
                           timing=events_service.timing_summary(event))


@bp.route("/organizer/events/<int:event_id>/progress")
@organizer_required
def progress(event_id: int):
    event = _event_or_404(event_id)
    return render_template("organizer_progress.html", event=event,
                           progress=judging.judging_progress(event_id),
                           timing=events_service.timing_summary(event))


# --------------------------------------------------------------------------
# normalization & results
# --------------------------------------------------------------------------
@bp.route("/organizer/events/<int:event_id>/results", methods=["GET", "POST"])
@organizer_required
def results(event_id: int):
    event = _event_or_404(event_id)
    user = current_user()
    if request.method == "POST":
        result = normalization.run_and_store(event_id, user["id"])
        flash(f"Normalization complete: {result['review_count']} completed review(s) normalized.", "ok")
        return redirect(url_for("organizer.results", event_id=event_id))

    freshness = normalization.freshness(event_id)
    stored = normalization.stored_result(event_id)
    live = normalization.compute(event_id) if request.method == "GET" else None
    history = db.query_all(
        "SELECT * FROM normalization_runs WHERE event_id = ? ORDER BY id DESC LIMIT 10", (event_id,)
    )
    from ..judging import publication
    progress = judging.judging_progress(event_id)
    return render_template("organizer_results.html", event=event, freshness=freshness, stored=stored,
                           live=live, history=history, progress=progress,
                           latest_run=normalization.latest_run(event_id),
                           publication_state=publication.state(event, freshness, progress))


@bp.route("/organizer/events/<int:event_id>/publish", methods=["POST"])
@organizer_required
def publish(event_id: int):
    _event_or_404(event_id)
    from ..judging import publication

    timestamp, refusal = publication.publish(event_id)
    if refusal:
        return render_template("error.html", code=409,
                               message="Results not ready: " + refusal["reason"] + "."), 409
    flash("Results published from a current, fully reviewed normalization.", "ok")
    return redirect(url_for("organizer.results", event_id=event_id))
