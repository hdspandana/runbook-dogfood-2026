"""Lifecycle gates: Runbook recommendations, publication and independent CSV."""
import os

import pytest
from app import create_app, db
from app.bootstrap import DEMO_TOKENS
from app.judging import normalization, service as judging
from app.runbook import service as runbook


@pytest.fixture
def portal(tmp_path):
    previous = os.environ.get("RUNBOOK_DATA_DIR")
    os.environ["RUNBOOK_DATA_DIR"] = str(tmp_path)
    try:
        app = create_app()
        app.config["TESTING"] = True
        yield app
    finally:
        if previous is None:
            os.environ.pop("RUNBOOK_DATA_DIR", None)
        else:
            os.environ["RUNBOOK_DATA_DIR"] = previous


@pytest.fixture
def scenario(portal):
    """Restore the pilot to its original 16 assigned, pending reviews."""
    with portal.app_context():
        db.execute("DELETE FROM normalization_runs WHERE event_id = 2")
        db.execute("DELETE FROM review_scores WHERE review_id IN (SELECT id FROM reviews WHERE event_id = 2)")
        db.execute("UPDATE reviews SET status = 'pending', raw_total = NULL, submitted_at = NULL WHERE event_id = 2")
        db.execute("""UPDATE events SET results_published_at = NULL,
                   starts_at = '2026-08-01T00:00:00Z',
                   submission_deadline = '2026-08-31T00:00:00Z',
                   judging_starts_at = '2026-09-01T00:00:00Z',
                   judging_ends_at = '2027-12-31T00:00:00Z' WHERE id = 2""")
        # Keep the pilot judging window open for this isolated policy test.
    yield portal


def client(portal, role="organizer"):
    result = portal.test_client()
    result.set_cookie("runbook_session", DEMO_TOKENS[role])
    return result


def complete_all(portal):
    with portal.app_context():
        criteria = judging.rubric_for_event(2)
        for review in db.query_all("SELECT * FROM reviews WHERE event_id = 2 ORDER BY id"):
            scores = {c["id"]: float((review["id"] % 6) + 2) for c in criteria}
            for cid, value in scores.items():
                db.execute("INSERT INTO review_scores (review_id, criterion_id, score) VALUES (?, ?, ?)",
                           (review["id"], cid, value))
            db.execute("UPDATE reviews SET status = 'complete', raw_total = ?, submitted_at = '2026-11-20T00:00:00Z' WHERE id = ?",
                       (judging.weighted_raw_total(criteria, scores), review["id"]))


def snapshot(portal):
    with portal.app_context():
        normalization.run_and_store(2, None)


def state(portal):
    with portal.app_context():
        return runbook.build(2)


def card(payload, step):
    return next(x for x in payload["steps"] if x["step"] == step)


def test_1_incomplete_and_stale_recommends_judging(scenario):
    complete_all(scenario)
    snapshot(scenario)
    with scenario.app_context():
        db.execute("UPDATE reviews SET status = 'pending', raw_total = NULL WHERE id = (SELECT MIN(id) FROM reviews WHERE event_id = 2)")
    result = state(scenario)
    assert card(result, "normalization")["status"] == "STALE"
    assert card(result, "normalization")["next_action"] == "Continue judging first"
    assert result["next_action"]["step"] == "judging"
    assert card(result, "results")["status"] == "BLOCKED"


def test_2_incomplete_and_current_recommends_judging(scenario):
    snapshot(scenario)  # snapshot of partial/pending state is allowed, not publishable
    result = state(scenario)
    assert card(result, "normalization")["status"] == "UPCOMING"  # no reviews yet
    assert result["next_action"]["step"] == "judging"
    complete_all(scenario)
    with scenario.app_context():
        db.execute("UPDATE reviews SET status = 'pending', raw_total = NULL WHERE id = (SELECT MIN(id) FROM reviews WHERE event_id = 2)")
    snapshot(scenario)
    result = state(scenario)
    assert card(result, "normalization")["status"] == "COMPLETE"
    assert result["next_action"]["step"] == "judging"
    assert card(result, "results")["status"] == "BLOCKED"


def test_3_complete_and_never_run_recommends_normalization(scenario):
    complete_all(scenario)
    result = state(scenario)
    assert card(result, "judging")["status"] == "COMPLETE"
    assert result["next_action"]["step"] == "normalization"
    assert result["next_action"]["headline"] == "Run normalization"


def test_4_complete_and_stale_recommends_renormalization(scenario):
    complete_all(scenario)
    snapshot(scenario)
    with scenario.app_context():
        db.execute("UPDATE review_scores SET score = score + 0.5 WHERE id = (SELECT MIN(rs.id) FROM review_scores rs JOIN reviews r ON r.id=rs.review_id WHERE r.event_id=2)")
    result = state(scenario)
    assert card(result, "normalization")["status"] == "STALE"
    assert result["next_action"]["step"] == "normalization"
    assert result["next_action"]["headline"] == "Re-run normalization"


def test_5_complete_current_recommends_results(scenario):
    complete_all(scenario)
    snapshot(scenario)
    result = state(scenario)
    assert result["next_action"]["step"] == "results"
    assert card(result, "results")["status"] == "ACTIVE"


def test_6_incomplete_publish_rejected_on_both_routes(scenario):
    organizer = client(scenario)
    snapshot(scenario)
    response = organizer.post("/api/events/2/publish")
    assert response.status_code == 409
    assert response.get_json()["error"] == "results_not_ready"
    assert response.get_json()["completed_reviews"] < response.get_json()["required_reviews"]
    assert organizer.post("/organizer/events/2/publish").status_code == 409
    with scenario.app_context():
        assert db.query_scalar("SELECT results_published_at FROM events WHERE id = 2") is None


def test_7_stale_publish_rejected_on_both_routes(scenario):
    complete_all(scenario)
    snapshot(scenario)
    with scenario.app_context():
        db.execute("UPDATE review_scores SET score = score + 0.5 WHERE id = (SELECT MIN(rs.id) FROM review_scores rs JOIN reviews r ON r.id=rs.review_id WHERE r.event_id=2)")
    organizer = client(scenario)
    response = organizer.post("/api/events/2/publish")
    assert response.status_code == 409
    assert response.get_json()["reason"] == "normalized result is stale"
    assert organizer.post("/organizer/events/2/publish").status_code == 409
    with scenario.app_context():
        assert db.query_scalar("SELECT results_published_at FROM events WHERE id = 2") is None


def test_8_complete_current_publish_succeeds(scenario):
    complete_all(scenario)
    snapshot(scenario)
    response = client(scenario).post("/api/events/2/publish")
    assert response.status_code == 200
    assert response.get_json()["published"] is True
    with scenario.app_context():
        assert db.query_scalar("SELECT results_published_at FROM events WHERE id = 2")


def test_9_export_works_without_current_normalization(scenario):
    organizer = client(scenario)
    assert organizer.get("/api/events/2/export.csv").status_code == 200
    complete_all(scenario)
    snapshot(scenario)
    assert organizer.get("/api/events/2/export.csv").status_code == 200
    with scenario.app_context():
        db.execute("UPDATE reviews SET status = 'pending', raw_total = NULL WHERE id = (SELECT MIN(id) FROM reviews WHERE event_id = 2)")
    csv = organizer.get("/api/events/2/export.csv")
    assert csv.status_code == 200
    assert csv.headers["X-Runbook-Normalization-Status"] == "stale"
    assert ",pending," in csv.text


def test_unassigned_project_does_not_count_as_complete(scenario):
    complete_all(scenario)
    with scenario.app_context():
        db.execute("DELETE FROM assignments WHERE project_id = (SELECT MIN(id) FROM projects WHERE event_id = 2 AND status = 'submitted')")
        result = runbook.build(2)
        assert result["progress"]["unassigned_projects"]
        assert not result["progress"]["all_reviews_complete"]
        assert result["next_action"]["step"] == "assignments"
    assert client(scenario).post("/api/events/2/publish").status_code == 409


def test_event_metadata_change_invalidates_snapshot(scenario):
    complete_all(scenario)
    snapshot(scenario)
    with scenario.app_context():
        db.execute("UPDATE events SET name = name || ' revised' WHERE id = 2")
        assert normalization.freshness(2)["state"] == "stale"
    assert client(scenario).post("/api/events/2/publish").status_code == 409


def test_admin_is_not_a_judge_or_participant(scenario):
    from app.security import roles_of, has_role, is_judge
    with scenario.app_context():
        admin = db.query_one("SELECT * FROM users WHERE role = 'admin' LIMIT 1")
        assert roles_of(admin) == {"admin", "organizer"}
        assert not has_role(admin, "judge", "participant")
        assert not is_judge(admin)
    session = client(scenario, "admin")
    assert session.get("/api/events/2/export.csv").status_code == 200
    assert session.post("/api/projects", json={"event_id": 2, "name": "admin project"}).status_code == 403
    assert session.post("/api/judging/projects/41/review", json={"scores": {}}).status_code == 403


def test_event_schedule_validation_and_published_lock(scenario):
    from app.organizer.routes import _validate_event
    with scenario.app_context():
        event = db.query_one("SELECT * FROM events WHERE id = 2")
        values = dict(event)
        values["judging_starts_at"] = "2026-08-30T00:00:00Z"
        assert any("before the submission deadline" in x for x in _validate_event(values, event))
        values = dict(event)
        values["judging_ends_at"] = "2026-08-30T00:00:00Z"
        assert any("before it starts" in x for x in _validate_event(values, event))
        db.execute("UPDATE events SET results_published_at = '2026-09-25T00:00:00Z' WHERE id = 2")
        published = db.query_one("SELECT * FROM events WHERE id = 2")
        values = dict(published)
        values["submission_deadline"] = "2026-09-02T00:00:00Z"
        assert any("published event" in x for x in _validate_event(values, published))


def test_zero_review_stale_snapshot_is_not_hidden(scenario):
    complete_all(scenario)
    snapshot(scenario)
    with scenario.app_context():
        db.execute("UPDATE reviews SET status = 'pending', raw_total = NULL WHERE event_id = 2")
    result = state(scenario)
    assert card(result, "normalization")["status"] == "STALE"
    assert result["next_action"]["step"] == "judging"
    assert client(scenario).post("/api/events/2/publish").status_code == 409


def test_assignment_selection_cannot_bypass_track_or_acceptance(scenario):
    with scenario.app_context():
        project = db.query_one("SELECT id, track_id FROM projects WHERE event_id = 2 AND status = 'submitted' ORDER BY id LIMIT 1")
        db.execute("DELETE FROM assignments WHERE project_id = ?", (project["id"],))
        outsider = db.query_one("SELECT id FROM users WHERE role = 'judge' AND id NOT IN (SELECT accepted_by FROM judge_invites WHERE event_id = 2)")
        assert outsider is not None
        created = judging.assign_round_robin(2, judges_per_project=1, judge_ids=[outsider["id"]])
        assert created["created"] == 0
        assert project["id"] in created["tracks_skipped"]
        assert db.query_scalar("SELECT COUNT(*) FROM assignments WHERE project_id = ?", (project["id"],)) == 0


def test_browser_publish_requires_snapshot_then_allows_current(scenario):
    complete_all(scenario)
    organizer = client(scenario)
    assert organizer.post('/organizer/events/2/publish').status_code == 409
    snapshot(scenario)
    response = organizer.post('/organizer/events/2/publish')
    assert response.status_code == 302
    with scenario.app_context():
        assert db.query_scalar('SELECT results_published_at FROM events WHERE id = 2')


def test_published_snapshot_stales_and_requires_republication(scenario):
    complete_all(scenario)
    snapshot(scenario)
    organizer = client(scenario)
    assert organizer.post('/api/events/2/publish').status_code == 200
    initial = organizer.get('/api/events/2/results').get_json()
    assert initial['published_result_current'] is True
    assert initial['publication_state'] == 'current'
    published_at = initial['published_at']
    with scenario.app_context():
        db.execute("UPDATE review_scores SET score = score + 0.5 WHERE id = (SELECT MIN(rs.id) FROM review_scores rs JOIN reviews r ON r.id = rs.review_id WHERE r.event_id = 2)")
    changed = organizer.get('/api/events/2/results').get_json()
    assert changed['published_at'] == published_at
    assert changed['publication_state'] == 'stale'
    assert changed['published_result_current'] is False
    assert state(scenario)['publication']['state'] == 'stale'
    assert 'STALE' in client(scenario).get('/organizer/events/2/results').text
    assert organizer.post('/api/events/2/publish').status_code == 409
    snapshot(scenario)
    # A new normalization does not retroactively make an old publication current.
    assert organizer.get('/api/events/2/results').get_json()['publication_state'] == 'stale'
    assert card(state(scenario), 'results')['status'] == 'ACTIVE'
    assert organizer.post('/api/events/2/publish').status_code == 200
    assert organizer.get('/api/events/2/results').get_json()['published_result_current'] is True


def test_legacy_timestamp_is_unverified_until_republished(scenario):
    complete_all(scenario)
    snapshot(scenario)
    with scenario.app_context():
        db.execute("UPDATE events SET results_published_at = '2026-09-25T00:00:00Z', results_published_run_id = NULL WHERE id = 2")
    result = client(scenario).get('/api/events/2/results').get_json()
    assert result['publication_state'] == 'stale'
    assert result['published_result_current'] is False
    assert client(scenario).post('/api/events/2/publish').status_code == 200


def test_results_endpoints_expose_run_linkage(scenario):
    complete_all(scenario)
    snapshot(scenario)
    organizer = client(scenario)
    assert organizer.post('/api/events/2/publish').status_code == 200
    data = organizer.get('/api/events/2/results').get_json()
    assert data['latest_run_id'] is not None
    assert data['published_run_id'] == data['latest_run_id']
    page = organizer.get('/organizer/events/2/results').text
    assert f"#{data['latest_run_id']}" in page
