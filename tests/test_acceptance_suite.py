"""Reproduce the official seven acceptance checks in pytest."""

from __future__ import annotations

import json
from pathlib import Path


def test_check_1_public_gallery_is_200(roles):
    response = roles.anon.get("/gallery")
    assert response.status_code == 200
    assert b"Public gallery" in response.data


def test_check_2_gallery_contains_fixture_project(roles):
    fixture = json.loads(Path("fixtures.json").read_text(encoding="utf-8"))
    titles = [p["title"] for p in fixture["projects"][:3]]
    response = roles.anon.get("/gallery")
    haystack = response.data.decode("utf-8").lower()
    assert any(title.lower() in haystack for title in titles), f"none of {titles} in gallery"


def test_check_3_closed_event_refuses_submission(roles):
    response = roles.participant.post(
        "/api/projects",
        json={"title": "probe", "summary": "probe"},
    )
    assert 400 <= response.status_code < 500
    assert response.status_code == 403
    payload = response.get_json()
    assert "submission window closed" in payload["error"]


def test_check_4_judge_sees_own_scores(roles, test_app):
    case = test_app.config["DEMO_SESSIONS"]["case"]
    review_id = case["judge_a_review_id"]
    response = roles.judge_a.get(f"/api/judging/reviews/{review_id}")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["review_id"] == review_id
    assert "criteria" in payload
    assert payload["status"] == "complete"


def test_check_5_judge_cannot_see_peer_scores(roles, test_app):
    case = test_app.config["DEMO_SESSIONS"]["case"]
    review_id = case["judge_a_review_id"]
    # Judge B visits judge A's review endpoint:
    response = roles.judge_b.get(f"/api/judging/reviews/{review_id}")
    assert response.status_code in (401, 403)
    assert b"raw_total" not in response.data
    assert b"criteria" not in response.data


def test_check_6_participant_cannot_see_judge_scores(roles, test_app):
    case = test_app.config["DEMO_SESSIONS"]["case"]
    review_id = case["judge_a_review_id"]
    response = roles.participant.get(f"/api/judging/reviews/{review_id}")
    assert response.status_code in (401, 403)
    assert response.status_code == 403
    assert b"raw_total" not in response.data


def test_check_7_organizer_csv_export_works(roles):
    response = roles.organizer.get("/api/events/1/export.csv")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/csv")
    first_line = response.data.decode("utf-8").splitlines()[0]
    assert "," in first_line
    assert "review_status" in first_line
    assert "z_score" in first_line
