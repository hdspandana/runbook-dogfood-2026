"""Team formation: create a team, share an invite link, join through it.

Invite secrets are 192-bit URL-safe random tokens (`secrets.token_urlsafe(24)`).
Sequential ids are never used as invite secrets, and the token is compared by
primary-key lookup so a wrong token simply does not resolve.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..events import service as events_service
from ..security import current_user, deny, membership
from ..util import json_error, json_ok, new_token, now_utc, to_iso

bp = Blueprint("teams", __name__)


def _event_or_404(event_id: int):
    event = events_service.get_event(event_id)
    if event is None:
        abort(404)
    return event


@bp.route("/teams/create", methods=["POST"])
def create():
    user = current_user()
    if user is None:
        return deny("Sign in to form a team.", 401)
    if user["role"] != "participant":
        return deny("Only participant accounts can form a team.", 403)

    event_id = request.form.get("event_id", type=int)
    name = (request.form.get("name") or "").strip()
    event = _event_or_404(event_id) if event_id else None
    if event is None:
        return deny("Unknown event.", 400)
    from ..projects.service import enforce_deadline

    closed = enforce_deadline(event, as_json=False)
    if closed is not None:
        return closed

    existing = db.query_one(
        "SELECT * FROM team_members WHERE event_id = ? AND user_id = ?", (event["id"], user["id"])
    )
    if existing:
        # "A participant can belong to at most one team per event" — the schema
        # enforces it (UNIQUE(event_id, user_id)); here we return a useful answer.
        return deny("You are already on a team in this event.", 409)

    if not name:
        return deny("A team name is required.", 400)
    if db.query_one("SELECT id FROM teams WHERE event_id = ? AND name = ?", (event["id"], name)):
        return deny("That team name is taken in this event.", 409)

    stamp = to_iso(now_utc())
    with db.transaction(db.get_db()):
        team_id = db.insert(
            """INSERT INTO teams (event_id, name, invite_token, created_by, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (event["id"], name, new_token(24), user["id"], stamp),
        )
        db.insert(
            """INSERT INTO team_members (event_id, team_id, user_id, is_lead, created_at)
               VALUES (?, ?, ?, 1, ?)""",
            (event["id"], team_id, user["id"], stamp),
        )
    flash(f"Team “{name}” created. Share the invite link with your teammates.", "ok")
    return redirect(url_for("teams.detail", team_id=team_id))


@bp.route("/teams/<int:team_id>")
def detail(team_id: int):
    user = current_user()
    team = db.query_one("SELECT * FROM teams WHERE id = ?", (team_id,))
    if team is None:
        abort(404)
    if user is None or membership(user["id"], team_id) is None:
        return deny("You are not a member of this team.", 403)

    members = db.query_all(
        """SELECT u.*, tm.is_lead FROM team_members tm JOIN users u ON u.id = tm.user_id
            WHERE tm.team_id = ? ORDER BY tm.is_lead DESC, u.id ASC""",
        (team_id,),
    )
    project = db.query_one("SELECT * FROM projects WHERE event_id = ? AND team_id = ?",
                           (team["event_id"], team_id))
    event = events_service.get_event(team["event_id"])
    return render_template(
        "team_detail.html",
        team=team,
        members=members,
        project=project,
        event=event,
        timing=events_service.timing_summary(event),
        invite_url=url_for("teams.join", token=team["invite_token"], _external=False),
        is_lead=bool(members and members[0]["id"] == user["id"]),
    )


@bp.route("/teams/<int:team_id>/invite/rotate", methods=["POST"])
def rotate_invite(team_id: int):
    user = current_user()
    team = db.query_one("SELECT * FROM teams WHERE id = ?", (team_id,))
    if team is None:
        abort(404)
    member = membership(user["id"], team_id) if user else None
    if member is None:
        return deny("You are not a member of this team.", 403)
    if not member["is_lead"]:
        return deny("Only the team lead can rotate the invite link.", 403)
    db.execute("UPDATE teams SET invite_token = ? WHERE id = ?", (new_token(24), team_id))
    flash("Invite link rotated. The previous link no longer works.", "ok")
    return redirect(url_for("teams.detail", team_id=team_id))


@bp.route("/join/<token>", methods=["GET", "POST"])
def join(token: str):
    user = current_user()
    team = db.query_one("SELECT * FROM teams WHERE invite_token = ?", (token,))
    if team is None:
        return render_template("error.html", code=404,
                               message="That invite link is invalid or has been rotated."), 404
    event = events_service.get_event(team["event_id"])

    if user is None:
        return redirect(url_for("auth.login", next=request.path))
    if user["role"] != "participant":
        return deny("Only participant accounts can join a team.", 403)

    if request.method == "GET":
        already = membership(user["id"], team["id"]) is not None
        other = db.query_one(
            "SELECT * FROM team_members WHERE event_id = ? AND user_id = ? AND team_id <> ?",
            (team["event_id"], user["id"], team["id"]),
        )
        return render_template("team_join.html", team=team, event=event, already=already,
                               conflict=other is not None)

    from ..projects.service import enforce_deadline

    closed = enforce_deadline(event, as_json=False)
    if closed is not None:
        return closed

    existing = db.query_one(
        "SELECT * FROM team_members WHERE event_id = ? AND user_id = ?", (team["event_id"], user["id"])
    )
    if existing:
        if existing["team_id"] == team["id"]:
            flash("You are already on this team.", "ok")
        else:
            return deny("You are already on a different team in this event.", 409)
        return redirect(url_for("teams.detail", team_id=team["id"]))

    db.insert(
        """INSERT INTO team_members (event_id, team_id, user_id, is_lead, created_at)
           VALUES (?, ?, ?, 0, ?)""",
        (team["event_id"], team["id"], user["id"], to_iso(now_utc())),
    )
    flash(f"You joined “{team['name']}”.", "ok")
    return redirect(url_for("teams.detail", team_id=team["id"]))


# --------------------------------------------------------------------------
# JSON variants (used by the security test matrix)
# --------------------------------------------------------------------------
@bp.route("/api/teams/join", methods=["POST"])
def api_join():
    user = current_user()
    if user is None:
        return json_error("authentication required", 401)
    if user["role"] != "participant":
        return json_error("forbidden", 403)
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token") or request.form.get("token") or "")
    team = db.query_one("SELECT * FROM teams WHERE invite_token = ?", (token,))
    if team is None:
        return json_error("invalid invite token", 404)
    event = events_service.get_event(team["event_id"])
    from ..projects.service import enforce_deadline

    closed = enforce_deadline(event, as_json=True) if event else None
    if closed is not None:
        return closed
    existing = db.query_one(
        "SELECT * FROM team_members WHERE event_id = ? AND user_id = ?", (team["event_id"], user["id"])
    )
    if existing:
        return json_error("already on a team in this event", 409)
    db.insert(
        """INSERT INTO team_members (event_id, team_id, user_id, is_lead, created_at)
           VALUES (?, ?, ?, 0, ?)""",
        (team["event_id"], team["id"], user["id"], to_iso(now_utc())),
    )
    return json_ok({"joined": True, "team_id": team["id"], "team": team["name"]})


@bp.route("/api/teams/<int:team_id>")
def api_detail(team_id: int):
    user = current_user()
    team = db.query_one("SELECT * FROM teams WHERE id = ?", (team_id,))
    if team is None:
        return json_error("not found", 404)
    if user is None or membership(user["id"], team_id) is None:
        return json_error("forbidden", 403)
    members = db.query_all(
        "SELECT u.id, u.display_name, tm.is_lead FROM team_members tm JOIN users u ON u.id = tm.user_id WHERE tm.team_id = ?",
        (team_id,),
    )
    return json_ok({
        "id": team["id"],
        "name": team["name"],
        "event_id": team["event_id"],
        "members": [dict(m) for m in members],
    })