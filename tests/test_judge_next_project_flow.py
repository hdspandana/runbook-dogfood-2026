"""Judge submit → next assigned project flow, and public home empty states.

These tests exercise the live pilot event (id 2) on the shared seeded test app.
Each test resets that event's judging window and one pilot judge's reviews so
results do not depend on test order. Identity always comes from a server-side
session created for an existing judge account; no client parameter selects the
next project.
"""
from __future__ import annotations

import pytest

from app import db
from app.auth import service as auth_service
from app.judging import service as judging

PILOT_EVENT = 2
PILOT_JUDGE = 4      # wei.lindqvist@example.org — six pilot assignments
OTHER_JUDGE = 3      # tomas.varga@example.org


@pytest.fixture
def pilot(test_app):
    """Open the pilot judging window and restore PILOT_JUDGE's reviews to pending.

    The event window is restored on teardown so later tests keep seeing the
    original live pilot schedule.
    """
    with test_app.app_context():
        original = dict(db.query_one(
            "SELECT starts_at, submission_deadline, judging_starts_at, judging_ends_at "
            "FROM events WHERE id = ?", (PILOT_EVENT,)))
        db.execute(
            "UPDATE events SET starts_at = '2026-08-01T00:00:00Z', "
            "submission_deadline = '2026-08-31T00:00:00Z', "
            "judging_starts_at = '2026-09-01T00:00:00Z', "
            "judging_ends_at = '2027-12-31T00:00:00Z' WHERE id = ?", (PILOT_EVENT,))
        db.execute("DELETE FROM review_scores WHERE review_id IN "
                   "(SELECT id FROM reviews WHERE event_id = ?)", (PILOT_EVENT,))
        db.execute("UPDATE reviews SET status = 'pending', raw_total = NULL, submitted_at = NULL "
                   "WHERE event_id = ? AND judge_id = ?", (PILOT_EVENT, PILOT_JUDGE))
    yield test_app
    with test_app.app_context():
        db.execute(
            "UPDATE events SET starts_at = ?, submission_deadline = ?, judging_starts_at = ?, "
            "judging_ends_at = ? WHERE id = ?",
            (original["starts_at"], original["submission_deadline"],
             original["judging_starts_at"], original["judging_ends_at"], PILOT_EVENT))


def client_for(test_app, user_id):
    with test_app.app_context():
        token = auth_service.create_session(user_id, "pytest")
    client = test_app.test_client()
    client.set_cookie("runbook_session", token)
    return client


def queue(test_app, judge_id=PILOT_JUDGE):
    with test_app.app_context():
        return judging.judge_queue(judge_id, PILOT_EVENT)


def incomplete_rows(test_app, judge_id=PILOT_JUDGE):
    return [row for row in queue(test_app, judge_id) if row["review_status"] != "complete"]


def scores_payload(test_app, value=7.0):
    with test_app.app_context():
        criteria = judging.rubric_for_event(PILOT_EVENT)
    return {f"score_{c['id']}": str(min(value, float(c["max_score"]))) for c in criteria}


def submit(test_app, client, project_id, **extra):
    data = scores_payload(test_app)
    data["intent"] = "submit"
    data.update(extra)
    return client.post(f"/judge/projects/{project_id}", data=data)


def complete_directly(test_app, review_id):
    with test_app.app_context():
        criteria = judging.rubric_for_event(PILOT_EVENT)
        scores = {int(c["id"]): 7.0 for c in criteria}
        for cid, value in scores.items():
            db.execute("INSERT OR REPLACE INTO review_scores (review_id, criterion_id, score) "
                       "VALUES (?, ?, ?)", (review_id, cid, value))
        db.execute("UPDATE reviews SET status = 'complete', raw_total = ? WHERE id = ?",
                   (judging.weighted_raw_total(criteria, scores), review_id))


def test_submit_redirects_to_next_incomplete_assigned_project(pilot):
    rows = incomplete_rows(pilot)
    assert len(rows) >= 2
    client = client_for(pilot, PILOT_JUDGE)
    response = submit(pilot, client, rows[0]["project_id"])
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/judge/projects/{rows[1]['project_id']}")


def test_already_completed_projects_are_skipped(pilot):
    rows = incomplete_rows(pilot)
    assert len(rows) >= 3
    complete_directly(pilot, rows[1]["review_id"])  # the second project is done out-of-band
    client = client_for(pilot, PILOT_JUDGE)
    response = submit(pilot, client, rows[0]["project_id"])
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.endswith(f"/judge/projects/{rows[2]['project_id']}")
    assert f"/judge/projects/{rows[1]['project_id']}" != location


def test_final_submission_returns_to_queue_with_message(pilot):
    rows = incomplete_rows(pilot)
    assert rows
    for row in rows[:-1]:
        complete_directly(pilot, row["review_id"])
    client = client_for(pilot, PILOT_JUDGE)
    response = submit(pilot, client, rows[-1]["project_id"])
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/judge?event=2")
    page = client.get("/judge?event=2")
    assert b"All assigned projects reviewed." in page.data


def test_save_draft_does_not_advance(pilot):
    rows = incomplete_rows(pilot)
    assert rows
    client = client_for(pilot, PILOT_JUDGE)
    data = scores_payload(pilot)
    data["intent"] = "save"
    response = client.post(f"/judge/projects/{rows[0]['project_id']}", data=data)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/judge?event=2")
    still = [row for row in queue(pilot) if row["project_id"] == rows[0]["project_id"]]
    assert still and still[0]["review_status"] != "complete"


def test_bogus_client_supplied_next_project_is_ignored(pilot):
    rows = incomplete_rows(pilot)
    assert len(rows) >= 2
    client = client_for(pilot, PILOT_JUDGE)
    response = submit(pilot, client, rows[0]["project_id"], next_project_id="999999",
                      next="/judge/projects/999999")
    assert response.status_code == 302
    location = response.headers["Location"]
    assert "999999" not in location
    assert location.endswith(f"/judge/projects/{rows[1]['project_id']}")


def test_other_judge_cannot_access_next_target(pilot):
    rows = incomplete_rows(pilot)
    assert rows
    with pilot.app_context():
        shared = {r["project_id"] for r in judging.judge_queue(OTHER_JUDGE, PILOT_EVENT)}
    target = next(row for row in rows if row["project_id"] not in shared)
    other = client_for(pilot, OTHER_JUDGE)
    assert other.get(f"/judge/projects/{target['project_id']}").status_code == 403
    assert submit(pilot, other, target["project_id"]).status_code == 403


def test_public_home_empty_states(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNBOOK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RUNBOOK_SEED_ON_BOOT", "0")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.execute(
            "INSERT INTO events (name, slug, tagline, location, timezone_label, starts_at, "
            "submission_deadline, judging_starts_at, judging_ends_at, created_at) "
            "VALUES ('Empty', 'empty-event', '', '', 'UTC', '2026-01-01T00:00:00Z', "
            "'2026-02-01T00:00:00Z', '2026-02-02T00:00:00Z', '2026-03-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z')")
    html = app.test_client().get("/").data
    assert b"No tracks have been configured yet." in html
    assert b"No public submissions yet." in html
