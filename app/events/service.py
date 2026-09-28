"""Event phase maths. One place decides 'what phase is this event in right now'."""

from __future__ import annotations

from typing import Any

from .. import db
from ..util import countdown, now_utc, parse_iso, pretty_utc

PHASE_LABELS = {
    "UPCOMING": "Upcoming",
    "SUBMISSIONS_OPEN": "Submissions open",
    "SUBMISSIONS_CLOSED": "Submissions closed",
    "JUDGING_PENDING": "Judging opens soon",
    "JUDGING_OPEN": "Judging in progress",
    "JUDGING_CLOSED": "Judging closed",
    "RESULTS_PUBLISHED": "Results published",
}


def get_event(event_id: int | None):
    if event_id is None:
        return None
    return db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))


def all_events() -> list[Any]:
    return db.query_all("SELECT * FROM events ORDER BY starts_at DESC, id DESC")


def primary_event():
    """The event the public gallery and the Runbook open by default.

    Deterministic rule: the event that is currently in its submission or judging
    window; otherwise the most recent one.
    """
    events = all_events()
    if not events:
        return None
    # The official DOGFOOD acceptance fixture is the canonical event, even
    # when the optional live pilot is open. Gallery, default dashboard and the
    # acceptance probe therefore agree on which event they are looking at.
    official = next((e for e in events if e["slug"] == "dogfood-2026-official-fixture"), None)
    if official is not None:
        return official
    return events[0]


def phase(event, at=None) -> str:
    now = at or now_utc()
    starts = parse_iso(event["starts_at"])
    deadline = parse_iso(event["submission_deadline"])
    j_start = parse_iso(event["judging_starts_at"])
    j_end = parse_iso(event["judging_ends_at"])
    published = parse_iso(event["results_published_at"])
    if published and now >= published:
        from ..judging import publication
        if publication.state(event) == "current":
            return "RESULTS_PUBLISHED"
    if j_end and now >= j_end:
        return "JUDGING_CLOSED"
    if j_start and now >= j_start:
        return "JUDGING_OPEN"
    if deadline and now >= deadline:
        return "JUDGING_PENDING"
    if starts and now < starts:
        return "UPCOMING"
    return "SUBMISSIONS_OPEN"


def phase_label(event, at=None) -> str:
    return PHASE_LABELS[phase(event, at)]


def timing_summary(event, at=None) -> dict[str, Any]:
    now = at or now_utc()
    deadline = parse_iso(event["submission_deadline"])
    j_end = parse_iso(event["judging_ends_at"])
    return {
        "phase": phase(event, now),
        "phase_label": phase_label(event, now),
        "now": now,
        "starts_at": parse_iso(event["starts_at"]),
        "deadline": deadline,
        "judging_starts_at": parse_iso(event["judging_starts_at"]),
        "judging_ends_at": j_end,
        "deadline_passed": bool(deadline and now >= deadline),
        "deadline_display": pretty_utc(deadline),
        "deadline_countdown": countdown(deadline, now),
        "judging_countdown": countdown(j_end, now),
    }


def tracks_for(event_id: int) -> list[Any]:
    return db.query_all("SELECT * FROM tracks WHERE event_id = ? ORDER BY sort_order, id", (event_id,))


def prizes_for(event_id: int) -> list[Any]:
    return db.query_all(
        """SELECT pz.*, t.name AS track_name
             FROM prizes pz LEFT JOIN tracks t ON t.id = pz.track_id
            WHERE pz.event_id = ?
            ORDER BY (pz.track_id IS NOT NULL), pz.track_id, pz.rank IS NULL, pz.rank, pz.id""",
        (event_id,),
    )


def teams_for(event_id: int) -> list[Any]:
    return db.query_all("SELECT * FROM teams WHERE event_id = ? ORDER BY id", (event_id,))


def counts(event_id: int) -> dict[str, int]:
    return {
        "tracks": db.query_scalar("SELECT COUNT(*) FROM tracks WHERE event_id = ?", (event_id,), 0),
        "prizes": db.query_scalar("SELECT COUNT(*) FROM prizes WHERE event_id = ?", (event_id,), 0),
        "teams": db.query_scalar("SELECT COUNT(*) FROM teams WHERE event_id = ?", (event_id,), 0),
        "participants": db.query_scalar("SELECT COUNT(*) FROM team_members WHERE event_id = ?", (event_id,), 0),
        "projects": db.query_scalar("SELECT COUNT(*) FROM projects WHERE event_id = ?", (event_id,), 0),
        "submitted": db.query_scalar(
            "SELECT COUNT(*) FROM projects WHERE event_id = ? AND status = 'submitted'", (event_id,), 0
        ),
        "drafts": db.query_scalar(
            "SELECT COUNT(*) FROM projects WHERE event_id = ? AND status = 'draft'", (event_id,), 0
        ),
    }


def judged_projects_public(event_id: int) -> list[Any]:
    """Public gallery rows: submitted projects of an event, with no judging data."""
    return db.query_all(
        """SELECT p.id, p.name, p.tagline, p.description, p.repo_url, p.demo_url, p.web_url,
                  p.submitted_at, p.track_id, t.name AS track_name, tm.name AS team_name
             FROM projects p
             LEFT JOIN tracks t ON t.id = p.track_id
             LEFT JOIN teams tm ON tm.id = p.team_id
            WHERE p.event_id = ? AND p.status = 'submitted'
            ORDER BY p.id ASC""",
        (event_id,),
    )