"""Security test matrix: IDOR, cross-role, track isolation, deadline gates."""

from __future__ import annotations

import pytest


def test_visitor_cannot_access_runbook(roles):
    assert roles.anon.get("/organizer/runbook").status_code == 401
    assert roles.anon.get("/api/events/1/runbook").status_code == 401


def test_participant_cannot_access_runbook(roles):
    assert roles.participant.get("/organizer/runbook").status_code == 403
    assert roles.participant.get("/api/events/1/runbook").status_code == 403


def test_judge_cannot_access_runbook(roles):
    assert roles.judge_a.get("/organizer/runbook").status_code == 403
    assert roles.judge_a.get("/api/events/1/runbook").status_code == 403


def test_participant_cannot_export_csv(roles):
    assert roles.participant.get("/organizer/events/1/export.csv").status_code == 403
    assert roles.participant.get("/api/events/1/export.csv").status_code == 403


def test_judge_cannot_export_csv(roles):
    assert roles.judge_a.get("/organizer/events/1/export.csv").status_code == 403
    assert roles.judge_a.get("/api/events/1/export.csv").status_code == 403


def test_admin_has_organizer_capabilities(roles):
    # Admin is an explicit supported role with organizer-superset capability.
    assert roles.admin.get("/organizer/runbook").status_code == 200
    assert roles.admin.get("/api/events/1/runbook").status_code == 200
    assert roles.admin.get("/api/events/1/export.csv").status_code == 200


def test_judge_unassigned_project_rejected(roles, test_app):
    # Find a project judge A is NOT assigned to.
    with test_app.app_context():
        from app import db
        case = test_app.config["DEMO_SESSIONS"]["case"]
        judge_id = test_app.config["DEMO_SESSIONS"]["tokens"]["judge_a"]
        # Look up judge_a user id:
        user = db.query_one("SELECT u.id FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
                            (judge_id,))
        unassigned = db.query_one(
            """SELECT p.id FROM projects p
                WHERE p.event_id = 1 AND p.status = 'submitted'
                  AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.project_id = p.id AND a.judge_id = ?)
                LIMIT 1""",
            (user["id"],),
        )
        assert unassigned is not None
        target_id = unassigned["id"]

    response = roles.judge_a.get(f"/judge/projects/{target_id}")
    assert response.status_code == 403


def test_judge_cannot_spoof_identity_via_param(roles, test_app):
    case = test_app.config["DEMO_SESSIONS"]["case"]
    review_a = case["judge_a_review_id"]
    with test_app.app_context():
        from app import db
        user_b = db.query_one(
            "SELECT u.id FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
            (test_app.config["DEMO_SESSIONS"]["tokens"]["judge_b"],),
        )
    # Judge A tries to pass ?judge_id=<judge B's id>
    response = roles.judge_a.get(f"/api/judging/reviews/{review_a}?judge_id={user_b['id']}")
    assert response.status_code == 403


def test_judge_cross_track_progress_rejected(roles, test_app):
    with test_app.app_context():
        from app import db
        user_a = db.query_one(
            "SELECT u.id FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
            (test_app.config["DEMO_SESSIONS"]["tokens"]["judge_a"],),
        )
        # Find a track where judge A has NO assignments:
        other_track = db.query_one(
            """SELECT t.id FROM tracks t
                WHERE t.event_id = 1
                  AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.track_id = t.id AND a.judge_id = ?)
                LIMIT 1""",
            (user_a["id"],),
        )
        assert other_track is not None
        track_id = other_track["id"]

    response = roles.judge_a.get(f"/api/judging/tracks/{track_id}/progress")
    assert response.status_code == 403


def test_participant_cannot_edit_other_teams_project(roles, test_app):
    with test_app.app_context():
        from app import db
        user = db.query_one(
            "SELECT u.id FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
            (test_app.config["DEMO_SESSIONS"]["tokens"]["participant"],),
        )
        other_project = db.query_one(
            """SELECT p.id FROM projects p
                WHERE p.event_id = 1
                  AND NOT EXISTS (SELECT 1 FROM team_members tm WHERE tm.team_id = p.team_id AND tm.user_id = ?)
                LIMIT 1""",
            (user["id"],),
        )
        target = other_project["id"]

    response = roles.participant.post(f"/api/projects/{target}", json={"name": "defaced"})
    assert response.status_code == 403


def test_post_deadline_participant_edit_rejected(roles, test_app):
    case = test_app.config["DEMO_SESSIONS"]["case"]
    project_id = case["participant_project_id"]
    response = roles.participant.post(f"/api/projects/{project_id}", json={"name": "late-edit"})
    assert response.status_code == 403
    assert "submission window closed" in response.get_json()["error"]


def test_participant_team_uniqueness_per_event(roles, test_app):
    # Priya is already on tm_01 in event 1. Attempt to create a second team in event 1:
    response = roles.participant.post(
        "/teams/create",
        data={"event_id": 1, "name": "Second Team Attempt"},
    )
    assert response.status_code in (403, 409)


def test_session_cookie_flags_and_open_redirect_guard(roles, test_app):
    c = test_app.test_client()
    response = c.post("/register", data={"email": "cookie-check@example.org",
                                         "password": "long-enough-1", "display_name": "Cookie Check"})
    assert response.status_code == 302
    cookie = response.headers.get("Set-Cookie", "")
    assert "runbook_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=" in cookie

    evil = c.post("/login", data={"email": "cookie-check@example.org",
                                  "password": "long-enough-1", "next": "//evil.example/x"})
    assert evil.status_code == 302
    assert "evil.example" not in evil.headers["Location"]

    out = c.post("/logout")
    assert out.status_code == 302
    assert "runbook_session=" in out.headers.get("Set-Cookie", "")


def test_team_join_boundaries(roles, test_app):
    from app import db
    a = test_app.test_client()
    b = test_app.test_client()
    assert a.post("/register", data={"email": "join-a@example.org", "password": "long-enough-1",
                                     "display_name": "Join A"}).status_code == 302
    assert b.post("/register", data={"email": "join-b@example.org", "password": "long-enough-1",
                                     "display_name": "Join B"}).status_code == 302
    assert a.post("/teams/create", data={"event_id": 2, "name": "Alpha Join"}).status_code == 302
    with test_app.app_context():
        token = db.query_scalar("SELECT invite_token FROM teams WHERE event_id = 2 AND name = 'Alpha Join'")
        a_user = db.query_scalar("SELECT id FROM users WHERE email = 'join-a@example.org'")
    assert token

    assert b.post("/api/teams/join", json={"token": token}).status_code == 200
    assert b.post("/api/teams/join", json={"token": "not-a-real-token"}).status_code == 404
    with test_app.app_context():
        db.execute("INSERT INTO teams (event_id, name, invite_token, created_by, created_at) "
                   "VALUES (2, 'Beta Join', 'beta-token-xyz', ?, '2026-01-01T00:00:00Z')", (a_user,))
    # B already belongs to Alpha Join in event 2: a second team in the same event is refused.
    assert b.post("/api/teams/join", json={"token": "beta-token-xyz"}).status_code == 409
    assert roles.judge_a.post("/api/teams/join", json={"token": token}).status_code == 403
