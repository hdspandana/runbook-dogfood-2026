"""UTC boundary, participant lifecycle and duplicate submission tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.security import judging_open, submissions_closed
from app.util import parse_iso


def test_deadline_before_allowed_at_rejected():
    deadline = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
    event = {"submission_deadline": "2026-10-01T18:00:00Z"}
    assert not submissions_closed(event, deadline - timedelta(microseconds=1))
    assert submissions_closed(event, deadline)
    assert submissions_closed(event, deadline + timedelta(seconds=1))


def test_judging_window_before_at_after():
    event = {"judging_starts_at": "2026-10-02T00:00:00Z",
             "judging_ends_at": "2026-10-03T00:00:00Z"}
    start = parse_iso(event["judging_starts_at"])
    end = parse_iso(event["judging_ends_at"])
    assert not judging_open(event, start - timedelta(microseconds=1))
    assert judging_open(event, start)
    assert judging_open(event, end - timedelta(microseconds=1))
    assert not judging_open(event, end)


def test_live_pilot_create_edit_submit_and_duplicate(roles, test_app):
    """Exercise real DB writes through HTTP against the live pilot event.

    This is the complete T1 path — not just a mocked status code.
    """
    client = test_app.test_client()
    response = client.post(
        "/login",
        data={"email": "pilot11@example.org", "password": "participant123"},
    )
    assert response.status_code == 302

    with test_app.app_context():
        from app import db
        pilot = db.query_one("SELECT id FROM events WHERE slug = 'runbook-live-pilot'")
        assert pilot is not None
        event_id = pilot["id"]
        track = db.query_one("SELECT id FROM tracks WHERE event_id = ? ORDER BY id LIMIT 1", (event_id,))

    new = client.post(
        "/api/projects",
        json={"event_id": event_id, "name": "Pilot Participant 11 Project", "tagline": "T1 lifecycle",
              "description": "A real pilot submission for end-to-end tests.", "track_id": track["id"],
              "repo_url": "https://example.org/pilot11"},
    )
    assert new.status_code == 201, new.get_json()
    project = new.get_json()["project"]
    assert project["status"] == "draft"
    project_id = project["id"]

    # Draft is private:
    assert roles.anon.get(f"/api/gallery/{project_id}").status_code == 404

    # Edit before the deadline:
    edit = client.patch(f"/api/projects/{project_id}", json={"tagline": "Edited before deadline"})
    assert edit.status_code == 200
    assert edit.get_json()["project"]["tagline"] == "Edited before deadline"
    assert edit.get_json()["project"]["name"] == "Pilot Participant 11 Project"

    # Submit and verify public gallery visibility:
    submitted = client.post(f"/api/projects/{project_id}/submit")
    assert submitted.status_code == 200
    assert submitted.get_json()["status"] == "submitted"
    assert roles.anon.get(f"/api/gallery/{project_id}").status_code == 200

    # One project per team per event, enforced by database and API:
    duplicate = client.post("/api/projects", json={
        "event_id": event_id, "name": "Duplicate pilot project", "track_id": track["id"],
    })
    assert duplicate.status_code == 409

    with test_app.app_context():
        from app import db
        project_row = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO projects
                   (event_id, team_id, track_id, name, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'schema-level-duplicate', 'draft', 'x', 'x')""",
                (project_row["event_id"], project_row["team_id"], project_row["track_id"]),
            )

    # The submitted project can still be edited while the window is open:
    edit_after_submit = client.patch(f"/api/projects/{project_id}", json={"tagline": "Final edit"})
    assert edit_after_submit.status_code == 200
    assert edit_after_submit.get_json()["project"]["status"] == "submitted"


def test_closed_event_participant_mutations_all_rejected(roles, test_app):
    case = test_app.config["DEMO_SESSIONS"]["case"]
    project_id = case["participant_project_id"]
    part = roles.participant
    assert part.post(f"/api/projects/{project_id}/submit").status_code == 403
    assert part.patch(f"/api/projects/{project_id}", json={"name": "Late edit"}).status_code == 403
    assert part.post("/api/projects", json={"event_id": 1, "name": "Late create"}).status_code == 403
