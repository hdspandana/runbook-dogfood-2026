"""Test Runbook computed states, transitions, and STALE detection."""

from __future__ import annotations

from app.runbook import service as runbook_service
from app.judging import normalization


def test_runbook_contains_all_eight_steps(roles):
    response = roles.organizer.get("/api/events/1/runbook")
    assert response.status_code == 200
    payload = response.get_json()
    steps = [c["step"] for c in payload["steps"]]
    assert steps == [
        "event_setup",
        "tracks_prizes",
        "rubric",
        "assignments",
        "submissions",
        "judging",
        "normalization",
        "results",
    ]


def test_stale_detection_and_normalization_refresh(roles, test_app):
    # At boot, normalization is STALE because 6 completed reviews were held back
    # when the snapshot was recorded.
    response = roles.organizer.get("/api/events/1/runbook")
    payload = response.get_json()
    norm_card = next(c for c in payload["steps"] if c["step"] == "normalization")
    assert norm_card["status"] == "STALE"
    assert "new review(s) detected" in norm_card["reason"] or "difference" in norm_card["reason"]
    assert payload["next_action"]["step"] in ("normalization", "judging")

    # Organizer re-runs normalization:
    normalize_resp = roles.organizer.post("/api/events/1/normalize")
    assert normalize_resp.status_code == 200
    norm_result = normalize_resp.get_json()
    assert norm_result["freshness"]["state"] == "current"

    # Refreshed Runbook must now show normalization as COMPLETE:
    refreshed = roles.organizer.get("/api/events/1/runbook").get_json()
    norm_card_refreshed = next(c for c in refreshed["steps"] if c["step"] == "normalization")
    assert norm_card_refreshed["status"] == "COMPLETE"


def test_runbook_never_gates_csv_export(roles):
    # CSV export must succeed regardless of whether Runbook is STALE or COMPLETE
    response = roles.organizer.get("/api/events/1/export.csv")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/csv")
