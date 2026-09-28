"""Project lifecycle: create → draft → submit → editable until the deadline → locked.

The single deadline gate lives here, and every participant mutation calls it.
It is evaluated on the server against UTC wall-clock time, never against a
client-supplied value, a hidden field, or a rendered "closed" banner.
"""

from __future__ import annotations

from typing import Any, Optional

from .. import db
from ..security import submissions_closed, user_team_in_event
from ..util import json_error, now_utc, pretty_utc, to_iso

URL_FIELDS = ("repo_url", "demo_url", "web_url")
TEXT_FIELDS = ("name", "tagline", "description")


def deadline_error(event) -> dict[str, Any]:
    """The canonical payload used for every deadline rejection."""
    return {
        "error": "submission window closed",
        "detail": (
            f"Submissions for “{event['name']}” closed at "
            f"{pretty_utc(event['submission_deadline'])}. "
            "Participant-side project changes are locked after the deadline."
        ),
        "deadline": event["submission_deadline"],
        "closed_at": pretty_utc(event["submission_deadline"]),
        "server_time_utc": to_iso(now_utc()),
    }


def enforce_deadline(event, as_json: bool = True):
    """Return a 4xx response when the submission window has closed, else None."""
    if not submissions_closed(event):
        return None
    payload = deadline_error(event)
    if as_json:
        return json_error(payload["error"], 403, **{k: v for k, v in payload.items() if k != "error"})
    from flask import render_template

    return render_template("deadline_closed.html", event=event, payload=payload), 403


def validate(project: dict[str, Any], event) -> list[str]:
    errors: list[str] = []
    name = (project.get("name") or "").strip()
    if len(name) < 3:
        errors.append("Project name must be at least 3 characters.")
    if len(name) > 120:
        errors.append("Project name must be at most 120 characters.")
    for field in URL_FIELDS:
        value = (project.get(field) or "").strip()
        if value and not value.lower().startswith(("http://", "https://")):
            errors.append(f"{field.replace('_', ' ').title()} must start with http:// or https://")
    if len((project.get("description") or "")) > 4000:
        errors.append("Description must be at most 4000 characters.")
    track_id = project.get("track_id")
    if track_id:
        owns = db.query_one("SELECT id FROM tracks WHERE id = ? AND event_id = ?", (track_id, event["id"]))
        if owns is None:
            errors.append("That track does not belong to this event.")
    return errors


def clean(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": (project.get("name") or "").strip(),
        "tagline": (project.get("tagline") or "").strip(),
        "description": (project.get("description") or "").strip(),
        "repo_url": (project.get("repo_url") or "").strip(),
        "demo_url": (project.get("demo_url") or "").strip(),
        "web_url": (project.get("web_url") or "").strip(),
        "track_id": project.get("track_id") or None,
    }


def create_project(event, team_id: int, values: dict[str, Any]) -> Any:
    now = to_iso(now_utc())
    project_id = db.insert(
        """INSERT INTO projects
           (event_id, team_id, track_id, name, tagline, description, repo_url, demo_url, web_url,
            status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
        (event["id"], team_id, values["track_id"], values["name"], values["tagline"],
         values["description"], values["repo_url"], values["demo_url"], values["web_url"], now, now),
    )
    return db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))


def update_project(project, values: dict[str, Any]) -> None:
    db.execute(
        """UPDATE projects
              SET name = ?, tagline = ?, description = ?, repo_url = ?, demo_url = ?, web_url = ?,
                  track_id = ?, updated_at = ?
            WHERE id = ?""",
        (values["name"], values["tagline"], values["description"], values["repo_url"],
         values["demo_url"], values["web_url"], values["track_id"], to_iso(now_utc()), project["id"]),
    )


def submit_project(project) -> None:
    db.execute(
        "UPDATE projects SET status = 'submitted', submitted_at = ?, updated_at = ? WHERE id = ?",
        (to_iso(now_utc()), to_iso(now_utc()), project["id"]),
    )


def team_project(event_id: int, team_id: int):
    return db.query_one("SELECT * FROM projects WHERE event_id = ? AND team_id = ?", (event_id, team_id))


def participant_context(user) -> list[dict[str, Any]]:
    """Everything a participant sees on their dashboard, event by event."""
    from ..events import service as events_service

    rows = db.query_all(
        """SELECT tm.event_id, tm.team_id, tm.is_lead FROM team_members tm WHERE tm.user_id = ?""",
        (user["id"],),
    )
    context = []
    for row in rows:
        event = events_service.get_event(row["event_id"])
        if event is None:
            continue
        team = db.query_one("SELECT * FROM teams WHERE id = ?", (row["team_id"],))
        project = team_project(event["id"], row["team_id"])
        context.append(
            {
                "event": event,
                "team": team,
                "project": project,
                "timing": events_service.timing_summary(event),
                "is_lead": bool(row["is_lead"]),
            }
        )
    context.sort(key=lambda item: item["event"]["id"])
    return context
