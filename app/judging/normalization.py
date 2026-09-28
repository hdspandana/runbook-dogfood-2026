"""Cross-judge normalization.

The implementation below is the single source of truth for JUDGING.md.
If you change the maths here, change the doc in the same commit.

Method (per completed review, then aggregated per project):

    scope      = judge (within one event; the rubric is common across tracks)
    raw        = Σ criterion_score × criterion_weight / 100
    mean_j     = mean(raw) over judge j's *completed* reviews
    sigma_j    = population standard deviation of those same values
    z          = (raw - mean_j) / sigma_j          if sigma_j > 0
               = 0                                 if sigma_j == 0
    project    = mean(z) over the project's completed reviews
    fallbacks  = see below

Everything is deterministic: no randomness, no timestamps inside the maths,
4-decimal rounding on every reported number, and ties broken by raw score then
project id.
"""

from __future__ import annotations

import json
import math
import statistics
from typing import Any, Optional

from .. import db
from ..util import fingerprint, now_utc, to_iso
from . import service as judging

METHOD_LABEL = "per-judge z-score (population sigma)"


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------
def review_facts(event_id: int) -> list[dict[str, Any]]:
    """Every (project, judge) pair that counts towards judging for this event.

    Rows are returned for *pending* reviews too, so CSV export and progress can
    stay honest; normalization itself only consumes ``status == 'complete'``.
    """
    rows = db.query_all(
        """SELECT r.id AS review_id, a.event_id, r.status, r.raw_total, r.submitted_at, r.updated_at,
                  a.judge_id, u.display_name AS judge_name, u.email AS judge_email,
                  p.id AS project_id, p.name AS project_name, p.track_id, p.status AS project_status,
                  t.name AS track_name, tm.name AS team_name, tm.id AS team_id
             FROM assignments a
             JOIN projects p ON p.id = a.project_id
             JOIN users u ON u.id = a.judge_id
             LEFT JOIN reviews r ON r.project_id = a.project_id AND r.judge_id = a.judge_id
             LEFT JOIN tracks t ON t.id = p.track_id
             LEFT JOIN teams tm ON tm.id = p.team_id
            WHERE a.event_id = ? AND p.status = 'submitted'
            ORDER BY p.id ASC, u.id ASC""", 
        (event_id,),
    )
    facts: list[dict[str, Any]] = []
    for row in rows:
        fact = dict(row)
        scores = judging.scores_for_review(row["review_id"]) if row["review_id"] is not None else {}
        fact["status"] = row["status"] or "pending"
        fact["criterion_scores"] = scores
        fact["computed_raw"] = judging.weighted_raw_total(judging.rubric_for_event(event_id), scores) if scores else None
        facts.append(fact)
    return facts


def review_fingerprint(facts: list[dict[str, Any]], rubric_fp: str, event_id: Optional[int] = None) -> str:
    """Identity of the *completed* review set a normalization run was computed from.

    Includes the rubric fingerprint, so editing the rubric invalidates a
    persisted snapshot exactly like adding a review does.
    """
    completed_parts = [
        (f["review_id"], f["project_id"], f["judge_id"], f["computed_raw"],
         sorted(f["criterion_scores"].items()))
        for f in facts
        if f["status"] == "complete" and f["computed_raw"] is not None
    ]
    # A change to any entered criterion changes the fingerprint even if the
    # weighted total happens to remain identical after the edit. Include the
    # submitted-project roster too: a newly submitted but unassigned project
    # changes the result's unranked/coverage section, not its review_count.
    if event_id is not None:
        roster = [tuple(row) for row in db.query_all(
            "SELECT id, team_id, track_id, name FROM projects WHERE event_id = ? AND status = 'submitted' ORDER BY id",
            (event_id,),
        )]
    else:
        roster = []
    if event_id is not None:
        # The stored payload includes event/project/track/team labels and the
        # event schedule. An organizer editing any of these after normalization
        # must not be able to publish a snapshot computed against old facts.
        event = db.query_one(
            """SELECT name, starts_at, submission_deadline, judging_starts_at, judging_ends_at
                 FROM events WHERE id = ?""", (event_id,))
        track_labels = [tuple(row) for row in db.query_all(
            "SELECT id, name FROM tracks WHERE event_id = ? ORDER BY id", (event_id,))]
        team_labels = [tuple(row) for row in db.query_all(
            "SELECT id, name FROM teams WHERE event_id = ? ORDER BY id", (event_id,))]
        metadata = [tuple(event) if event else (), track_labels, team_labels]
    else:
        metadata = []
    return fingerprint([rubric_fp, roster, completed_parts, metadata])


# --------------------------------------------------------------------------
# the maths
# --------------------------------------------------------------------------
def _population_sigma(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)  # population, not sample
    return math.sqrt(variance)


def compute(event_id: int, min_reviews: Optional[int] = None) -> dict[str, Any]:
    """Compute the full normalization result for an event. Pure function of the DB."""
    from flask import current_app

    min_reviews = min_reviews if min_reviews is not None else int(current_app.config["MIN_REVIEWS_FOR_NORMALIZATION"])
    rubric = judging.rubric_for_event(event_id)
    rubric_fp = judging.rubric_fingerprint(event_id)
    facts = review_facts(event_id)
    complete = [f for f in facts if f["status"] == "complete" and f["computed_raw"] is not None]
    incomplete = [f for f in facts if f["status"] != "complete" or f["computed_raw"] is None]

    # --- per-judge statistics -------------------------------------------------
    by_judge: dict[int, list[dict[str, Any]]] = {}
    for fact in complete:
        by_judge.setdefault(fact["judge_id"], []).append(fact)

    judge_stats: dict[int, dict[str, Any]] = {}
    for judge_id, rows in by_judge.items():
        values = [float(r["computed_raw"]) for r in rows]
        mean = statistics.fmean(values)
        sigma = _population_sigma(values)
        insufficient = len(values) < min_reviews
        judge_stats[judge_id] = {
            "judge_id": judge_id,
            "judge": rows[0]["judge_name"],
            "reviews": len(values),
            "mean": round(mean, 4),
            "sigma": round(sigma, 4),
            "_mean_exact": mean,
            "_sigma_exact": sigma,
            # Edge case 1: sigma == 0 → z = 0 (the judge supplies no relative signal).
            "zero_variance": sigma == 0.0,
            # Edge case 2: too few reviews → neutral (z = 0) fallback.
            "insufficient_reviews": insufficient,
            "trusted": (not insufficient) and sigma > 0.0,
        }

    # --- z-scores -------------------------------------------------------------
    per_review: list[dict[str, Any]] = []
    any_variation = False
    for fact in complete:
        stats = judge_stats[fact["judge_id"]]
        raw = float(fact["computed_raw"])
        if stats["_sigma_exact"] > 0.0 and not stats["insufficient_reviews"]:
            z = (raw - stats["_mean_exact"]) / stats["_sigma_exact"]
            any_variation = True
            fallback = ""
        elif stats["insufficient_reviews"]:
            z = 0.0
            fallback = "insufficient_reviews"
        else:
            z = 0.0
            fallback = "zero_judge_variance"
        per_review.append(
            {
                "review_id": fact["review_id"],
                "project_id": fact["project_id"],
                "project": fact["project_name"],
                "project_status": fact["project_status"],
                "track_id": fact["track_id"],
                "track": fact["track_name"],
                "team_id": fact["team_id"],
                "team": fact["team_name"],
                "judge_id": fact["judge_id"],
                "judge": fact["judge_name"],
                "judge_email": fact["judge_email"],
                "review_status": fact["status"],
                "raw_total": round(raw, 4),
                "judge_mean": stats["mean"],
                "judge_sigma": stats["sigma"],
                "z_score": round(z, 4),
                "fallback": fallback,
            }
        )

    # Edge case 3: no meaningful normalized variation anywhere in the event.
    # normalized = raw (documented fallback), so results stay usable.
    mode = "z-score"
    if complete and not any_variation:
        mode = "raw-fallback"

    # --- project aggregation --------------------------------------------------
    by_project: dict[int, list[dict[str, Any]]] = {}
    for row in per_review:
        by_project.setdefault(row["project_id"], []).append(row)

    projects: list[dict[str, Any]] = []
    for project in db.query_all(
        "SELECT * FROM projects WHERE event_id = ? AND status = 'submitted' ORDER BY id ASC", (event_id,)
    ):
        rows = by_project.get(project["id"], [])
        if not rows:
            # Edge case 4: no completed reviews → unranked / provisional.
            projects.append(
                {
                    "project_id": project["id"],
                    "project": project["name"],
                    "track_id": project["track_id"],
                    "track": db.query_scalar("SELECT name FROM tracks WHERE id = ?", (project["track_id"],), "—"),
                    "team": db.query_scalar("SELECT name FROM teams WHERE id = ?", (project["team_id"],), "—"),
                    "review_count": 0,
                    "required_reviews": db.query_scalar(
                        "SELECT COUNT(*) FROM assignments WHERE project_id = ?", (project["id"],), 0
                    ),
                    "raw_mean": None,
                    "normalized_score": None,
                    "score": None,
                    "provisional": True,
                    "rank": None,
                }
            )
            continue
        raw_values = [r["raw_total"] for r in rows]
        normalized_values = [r["z_score"] for r in rows]
        if mode == "raw-fallback":
            score = statistics.fmean(raw_values)
        else:
            score = statistics.fmean(normalized_values)
        projects.append(
            {
                "project_id": project["id"],
                "project": project["name"],
                "track_id": project["track_id"],
                "track": db.query_scalar("SELECT name FROM tracks WHERE id = ?", (project["track_id"],), "—"),
                "team": db.query_scalar("SELECT name FROM teams WHERE id = ?", (project["team_id"],), "—"),
                "review_count": len(rows),
                "required_reviews": db.query_scalar(
                    "SELECT COUNT(*) FROM assignments WHERE project_id = ?", (project["id"],), 0
                ),
                "raw_mean": round(statistics.fmean(raw_values), 4),
                "normalized_score": round(statistics.fmean(normalized_values), 4),
                "score": round(float(score), 4),
                "provisional": False,
                "rank": None,
            }
        )

    # --- deterministic ranking & tie-breaking ---------------------------------
    ranked = sorted(
        [p for p in projects if p["score"] is not None],
        key=lambda p: (-p["score"], -(p["raw_mean"] if p["raw_mean"] is not None else 0.0), p["project_id"]),
    )
    for position, entry in enumerate(ranked, start=1):
        entry["rank"] = position

    stats_values = [r["raw_total"] for r in per_review]
    result = {
        "event_id": event_id,
        "generated_at": to_iso(now_utc()),
        "method": METHOD_LABEL,
        "mode": mode,
        "min_reviews_for_normalization": min_reviews,
        "rubric_fingerprint": rubric_fp,
        "rubric_criteria": [
            {"id": c["id"], "name": c["name"], "weight_pct": c["weight_pct"], "max_score": c["max_score"]}
            for c in rubric
        ],
        "review_count": len(per_review),
        "pending_review_count": len(incomplete),
        "judge_count": len(judge_stats),
        "project_count": len([p for p in projects if not p["provisional"]]),
        "provisional_project_count": len([p for p in projects if p["provisional"]]),
        "raw_mean_overall": round(statistics.fmean(stats_values), 4) if stats_values else None,
        "judge_stats": sorted(
            [{k: v for k, v in stats.items() if not k.startswith("_")}
             for stats in judge_stats.values()],
            key=lambda s: s["judge_id"],
        ),
        "reviews": sorted(per_review, key=lambda r: (r["project_id"], r["judge_id"])),
        "projects": ranked,
        "unranked_projects": [p for p in projects if p["provisional"]],
        "fingerprint": review_fingerprint(facts, rubric_fp, event_id),
        "limitations": [
            "Z-scores are relative: they say how a project ranked against that judge's own "
            "distribution of scores, not how good the project is in absolute terms.",
            "Judges with fewer than "
            f"{min_reviews} completed reviews contribute z = 0 (neutral) instead of a noisy estimate.",
            "A judge who scores every project identically (sigma = 0) contributes no relative "
            "signal; their reviews are treated as neutral.",
            "Projects with no completed reviews stay provisional and are excluded from ranking.",
            "Pending reviews are never counted as zero.",
        ],
    }
    return result


# --------------------------------------------------------------------------
# persistence & freshness (STALE detection)
# --------------------------------------------------------------------------
def latest_run(event_id: int):
    return db.query_one(
        "SELECT * FROM normalization_runs WHERE event_id = ? ORDER BY id DESC LIMIT 1", (event_id,)
    )


def freshness(event_id: int) -> dict[str, Any]:
    """Is the last persisted normalized result still current?

    This is the only persisted workflow history in RUNBOOK, and it is what
    makes the STALE state possible.
    """
    rubric_fp = judging.rubric_fingerprint(event_id)
    facts = review_facts(event_id)
    current_fp = review_fingerprint(facts, rubric_fp, event_id)
    current_complete = len([f for f in facts if f["status"] == "complete" and f["computed_raw"] is not None])
    pending = len([f for f in facts if f["status"] != "complete" or f["computed_raw"] is None])

    run = latest_run(event_id)
    if run is None:
        return {
            "state": "never_run",
            "current_review_count": current_complete,
            "current_pending_count": pending,
            "stored_review_count": 0,
            "delta": current_complete,
            "stored_at": None,
            "rubric_changed": False,
            "current_fingerprint": current_fp,
        }

    stored_fp = run["fingerprint"]
    rubric_changed = run["rubric_fingerprint"] != rubric_fp
    stored_count = int(run["review_count"])
    if stored_fp == current_fp:
        delta = 0
        rub = False
    else:
        delta = current_complete - stored_count
        rub = rubric_changed
    return {
        "state": "current" if stored_fp == current_fp else "stale",
        "current_review_count": current_complete,
        "current_pending_count": pending,
        "stored_review_count": stored_count,
        "delta": delta,
        "stored_at": run["created_at"],
        "rubric_changed": rub,
        "current_fingerprint": current_fp,
        "stored_fingerprint": stored_fp,
    }


def run_and_store(event_id: int, user_id: Optional[int]) -> dict[str, Any]:
    """Compute normalization and persist a snapshot (organizer action)."""
    result = compute(event_id)
    with db.transaction(db.get_db()):
        db.insert(
            """INSERT INTO normalization_runs
                (event_id, created_at, created_by, method, review_count, judge_count,
                 project_count, rubric_fingerprint, fingerprint, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                result["generated_at"],
                user_id,
                result["method"],
                result["review_count"],
                result["judge_count"],
                result["project_count"],
                result["rubric_fingerprint"],
                result["fingerprint"],
                json.dumps(result, sort_keys=True),
            ),
        )
    return result


def stored_result(event_id: int) -> Optional[dict[str, Any]]:
    run = latest_run(event_id)
    if run is None:
        return None
    return json.loads(run["payload"])