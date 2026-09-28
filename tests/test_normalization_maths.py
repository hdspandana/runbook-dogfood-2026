"""Unit tests for cross-judge normalization maths and edge cases."""

from __future__ import annotations

import statistics

from app.judging import normalization


def test_population_sigma_deterministic():
    values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    # Population sigma = sqrt(32 / 8) = 2.0
    sigma = normalization._population_sigma(values)
    assert abs(sigma - 2.0) < 1e-9


def test_zero_variance_judge_yields_zero_z_score():
    # If a judge gives the identical raw score to every project, sigma == 0.
    values = [4.0, 4.0, 4.0, 4.0]
    sigma = normalization._population_sigma(values)
    assert sigma == 0.0


def test_normalization_on_seeded_event(test_app):
    with test_app.app_context():
        result = normalization.compute(1)
        assert result["event_id"] == 1
        assert result["review_count"] > 0
        assert len(result["judge_stats"]) > 0
        assert len(result["projects"]) > 0
        # Check ranking ordering:
        scores = [p["score"] for p in result["projects"] if p["score"] is not None]
        assert scores == sorted(scores, reverse=True)
        # Ranks must be sequential 1..N:
        ranks = [p["rank"] for p in result["projects"] if p["rank"] is not None]
        assert ranks == list(range(1, len(ranks) + 1))


def test_deterministic_tie_breaking(test_app):
    # Two synthetic entries with identical normalized scores tiebreak by raw_mean DESC, then project_id ASC.
    p1 = {"score": 1.25, "raw_mean": 8.0, "project_id": 10}
    p2 = {"score": 1.25, "raw_mean": 8.5, "project_id": 5}
    p3 = {"score": 1.25, "raw_mean": 8.0, "project_id": 2}
    ranked = sorted(
        [p1, p2, p3],
        key=lambda p: (-p["score"], -p["raw_mean"], p["project_id"]),
    )
    # p2 has higher raw mean -> rank 1
    # p3 and p1 have same raw mean, but p3 has lower project_id -> rank 2
    # p1 -> rank 3
    assert ranked == [p2, p3, p1]
