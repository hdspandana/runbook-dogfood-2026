"""Judging domain logic: rubric validation, weighted scoring, assignment, progress.

Nothing in this module knows about HTTP. Routes translate its return values
into responses; tests exercise it directly.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from .. import db
from ..util import now_utc, to_iso

# --------------------------------------------------------------------------
# rubric
# --------------------------------------------------------------------------
def rubric_for_event(event_id: int) -> list[Any]:
    return db.query_all(
        """SELECT * FROM rubric_criteria
            WHERE event_id = ?
            ORDER BY sort_order ASC, id ASC""",
        (event_id,),
    )


def rubric_fingerprint(event_id: int) -> str:
    """Changes whenever a criterion name/weight/max/order changes.

    Folded into the normalization snapshot so that editing the rubric makes a
    previously persisted result STALE, which is what an organizer expects.
    """
    from ..util import fingerprint

    rows = rubric_for_event(event_id)
    return fingerprint(
        [(r["id"], r["name"], round(float(r["weight_pct"]), 6), round(float(r["max_score"]), 6), r["sort_order"]) for r in rows]
    )


def validate_rubric(criteria: Sequence[Any]) -> tuple[bool, str]:
    """A rubric is usable when it has criteria whose weights total 100%."""
    if not criteria:
        return False, "No rubric criteria configured."
    total = 0.0
    for row in criteria:
        weight = float(row["weight_pct"])
        if weight < 0:
            return False, f"Criterion “{row['name']}” has a negative weight."
        if float(row["max_score"]) <= 0:
            return False, f"Criterion “{row['name']}” must have a positive maximum score."
        total += weight
    if abs(total - 100.0) > 0.01:
        return False, f"Criterion weights must total 100% (currently {total:g}%)."
    return True, f"{len(criteria)} criteria, weights total {total:g}%."


def rubric_max_total(criteria: Sequence[Any]) -> float:
    return sum(float(c["max_score"]) * float(c["weight_pct"]) / 100.0 for c in criteria)


def weighted_raw_total(criteria: Sequence[Any], scores: dict[int, float]) -> float:
    """raw_total = Σ (score_i / max_i is NOT applied) — scores are absolute 0..max.

    raw_total = Σ score_i * weight_i / 100 , rounded to 4 dp for determinism.
    With the recommended model (each criterion 0–10, weights summing to 100%)
    this yields a raw total in 0..10.
    """
    total = 0.0
    for criterion in criteria:
        value = scores.get(int(criterion["id"]))
        if value is None:
            continue
        total += float(value) * float(criterion["weight_pct"]) / 100.0
    return round(total, 4)


# --------------------------------------------------------------------------
# assignment
# --------------------------------------------------------------------------
def eligible_judges(event_id: int, track_id: Optional[int]) -> list[int]:
    """Judges eligible for a track.

    A judge invited for a specific track is eligible for that track only.
    Judges with no track preference are eligible for every track.
    Ordered by id, so the assignment below is fully deterministic.
    """
    rows = db.query_all(
        """SELECT u.id, i.track_id AS preferred_track
             FROM users u
             JOIN judge_invites i ON i.accepted_by = u.id AND i.event_id = ?
            WHERE u.role = 'judge'
            ORDER BY u.id ASC""",
        (event_id,),
    )
    if track_id is None:
        return [r["id"] for r in rows]
    return [r["id"] for r in rows if r["preferred_track"] is None or r["preferred_track"] == track_id]


def assign_round_robin(event_id: int, judges_per_project: int = 2, judge_ids: Optional[Iterable[int]] = None) -> dict[str, Any]:
    """Round-robin assignment **within track**.

    Deterministic algorithm (documented in JUDGING.md):

        for each track (ordered by track id):
            J = eligible judges (ordered by user id)
            P = submitted projects in the track (ordered by project id)
            for i, project in enumerate(P):
                for k in range(judges_per_project):
                    judge = J[(i + k) % len(J)]

    With ``judges_per_project == 1`` this is exactly the example in the
    DOGFOOD spec: P1→J1, P2→J2, P3→J1, P4→J2.

    Existing assignments are kept; only missing (project, judge) pairs are
    inserted, so the operation is idempotent and safe to re-run.
    """
    created: list[int] = []
    skipped_projects: list[int] = []
    selected = set(judge_ids) if judge_ids is not None else None
    with db.transaction(db.get_db()):
        projects = db.query_all(
            """SELECT * FROM projects
                WHERE event_id = ? AND status = 'submitted'
                ORDER BY (track_id IS NULL), track_id ASC, id ASC""",
            (event_id,),
        )
        track_index: dict[Any, int] = {}
        for project in projects:
            eligible = eligible_judges(event_id, project["track_id"])
            # Optional caller selection narrows the eligible pool; it must
            # never grant an uninvited or wrong-track account an assignment.
            pool = [judge_id for judge_id in eligible
                    if selected is None or judge_id in selected]
            if not pool:
                skipped_projects.append(project["id"])
                continue
            # Round-robin position *within the project's track*, matching the
            # DOGFOOD example (P1→J1, P2→J2, P3→J1, P4→J2).
            index = track_index.get(project["track_id"], 0)
            track_index[project["track_id"]] = index + 1
            for k in range(max(1, judges_per_project)):
                judge_id = pool[(index + k) % len(pool)]
                exists = db.query_one(
                    "SELECT id FROM assignments WHERE project_id = ? AND judge_id = ?",
                    (project["id"], judge_id),
                )
                if exists:
                    continue
                assignment_id = db.insert(
                    """INSERT INTO assignments (event_id, project_id, track_id, judge_id, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (event_id, project["id"], project["track_id"], judge_id, to_iso(now_utc())),
                )
                created.append(assignment_id)
                db.execute(
                    """INSERT OR IGNORE INTO reviews
                       (event_id, project_id, judge_id, assignment_id, status, updated_at)
                       VALUES (?, ?, ?, ?, 'pending', ?)""",
                    (event_id, project["id"], judge_id, assignment_id, to_iso(now_utc())),
                )
    _backfill_pending_reviews(event_id)
    return {"created": len(created), "tracks_skipped": skipped_projects}


def _backfill_pending_reviews(event_id: int) -> None:
    """Every assignment must have exactly one review row (pending by default)."""
    db.execute(
        """INSERT OR IGNORE INTO reviews (event_id, project_id, judge_id, assignment_id, status, updated_at)
           SELECT a.event_id, a.project_id, a.judge_id, a.id, 'pending', ?
             FROM assignments a
            WHERE a.event_id = ?""",
        (to_iso(now_utc()), event_id),
    )


# --------------------------------------------------------------------------
# reviews
# --------------------------------------------------------------------------
def get_or_create_review(event_id: int, project_id: int, judge_id: int):
    _backfill_pending_reviews(event_id)
    return db.query_one(
        "SELECT * FROM reviews WHERE project_id = ? AND judge_id = ?", (project_id, judge_id)
    )


def scores_for_review(review_id: int) -> dict[int, float]:
    rows = db.query_all("SELECT criterion_id, score FROM review_scores WHERE review_id = ?", (review_id,))
    return {int(r["criterion_id"]): float(r["score"]) for r in rows}


def save_review(review, criteria: Sequence[Any], scores: dict[int, float], comment: str, submit: bool) -> dict[str, Any]:
    """Write a judge's review. Returns a summary dict (never raises for user error)."""
    errors: list[str] = []
    clean: dict[int, float] = {}
    import math

    for criterion in criteria:
        cid = int(criterion["id"])
        value = scores.get(cid)
        if value is None:
            # Saved draft scores are merged below when this review is submitted.
            continue
        maximum = float(criterion["max_score"])
        if not math.isfinite(float(value)) or value < 0 or value > maximum:
            errors.append(f"“{criterion['name']}” must be a finite number between 0 and {maximum:g}.")
            continue
        clean[cid] = float(value)
    unknown = set(scores) - {int(c["id"]) for c in criteria}
    if unknown:
        errors.append("Unknown criterion in score payload.")
    if errors:
        return {"ok": False, "errors": errors}
    if submit:
        valid, message = validate_rubric(criteria)
        if not valid:
            return {"ok": False, "errors": [message]}
        # A previously saved draft may hold partial criterion scores. A submit
        # can reuse those values; it must NOT silently treat absent values as
        # zero. Merge them and revalidate all required criteria.
        merged = {**scores_for_review(review["id"]), **clean}
        missing = [c["name"] for c in criteria if int(c["id"]) not in merged]
        if missing:
            return {"ok": False, "errors": [f"Missing score for {name}." for name in missing]}
        clean = merged

    raw_total = weighted_raw_total(criteria, clean) if submit else None
    now = to_iso(now_utc())
    with db.transaction(db.get_db()):
        for cid, value in clean.items():
            db.execute(
                """INSERT INTO review_scores (review_id, criterion_id, score) VALUES (?, ?, ?)
                   ON CONFLICT(review_id, criterion_id) DO UPDATE SET score = excluded.score""",
                (review["id"], cid, value),
            )
        db.execute(
            """UPDATE reviews
                  SET status = ?, raw_total = ?, comment = ?, updated_at = ?,
                      submitted_at = CASE WHEN ? THEN ? ELSE submitted_at END
                WHERE id = ?""",
            (
                "complete" if submit else "pending",
                raw_total,
                comment or "",
                now,
                1 if submit else 0,
                now,
                review["id"],
            ),
        )
    return {"ok": True, "errors": errors, "raw_total": raw_total, "status": "complete" if submit else "pending"}


# --------------------------------------------------------------------------
# progress (derived, never stored)
# --------------------------------------------------------------------------
def judging_progress(event_id: int) -> dict[str, Any]:
    """Everything the organizer dashboard needs, computed from current rows."""
    submitted = db.query_scalar(
        "SELECT COUNT(*) FROM projects WHERE event_id = ? AND status = 'submitted'", (event_id,), 0
    )
    assigned_projects = db.query_scalar(
        """SELECT COUNT(DISTINCT a.project_id) FROM assignments a
             JOIN projects p ON p.id = a.project_id
            WHERE a.event_id = ? AND p.status = 'submitted'""",
        (event_id,),
        0,
    )
    required_reviews = db.query_scalar(
        """SELECT COUNT(*) FROM assignments a
             JOIN projects p ON p.id = a.project_id
            WHERE a.event_id = ? AND p.status = 'submitted'""",
        (event_id,),
        0,
    )
    complete_reviews = db.query_scalar(
        """SELECT COUNT(*) FROM assignments a
             JOIN projects p ON p.id = a.project_id
             JOIN reviews r ON r.project_id = a.project_id AND r.judge_id = a.judge_id
            WHERE a.event_id = ? AND r.status = 'complete' AND p.status = 'submitted'""",
        (event_id,),
        0,
    )
    pending_reviews = max(0, required_reviews - complete_reviews)

    unassigned = db.query_all(
        """SELECT p.id, p.name FROM projects p
            WHERE p.event_id = ? AND p.status = 'submitted'
              AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.project_id = p.id)
            ORDER BY p.id ASC""",
        (event_id,),
    )
    invalid_track = db.query_all(
        """SELECT a.id FROM assignments a
             JOIN projects p ON p.id = a.project_id
             LEFT JOIN judge_invites ji ON ji.event_id = a.event_id AND ji.accepted_by = a.judge_id
            WHERE a.event_id = ? AND (
                a.track_id IS NULL OR a.track_id <> p.track_id OR a.event_id <> p.event_id
                OR ji.id IS NULL OR (ji.track_id IS NOT NULL AND ji.track_id <> p.track_id)
            )""",
        (event_id,),
    )
    duplicate = db.query_all(
        """SELECT project_id, judge_id, COUNT(*) c FROM assignments
            WHERE event_id = ? GROUP BY project_id, judge_id HAVING c > 1""",
        (event_id,),
    )
    idle_judges = db.query_all(
        """SELECT u.id, u.display_name FROM users u
             JOIN judge_invites i ON i.accepted_by = u.id AND i.event_id = ?
            WHERE u.role = 'judge'
              AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.judge_id = u.id AND a.event_id = ?)
            ORDER BY u.id ASC""",
        (event_id, event_id),
    )

    by_track = []
    for track in db.query_all("SELECT * FROM tracks WHERE event_id = ? ORDER BY sort_order, id", (event_id,)):
        required = db.query_scalar(
            """SELECT COUNT(*) FROM assignments a
                 JOIN projects p ON p.id = a.project_id
                WHERE a.event_id = ? AND a.track_id = ? AND p.status = 'submitted'""",
            (event_id, track["id"]),
            0,
        )
        done = db.query_scalar(
            """SELECT COUNT(*) FROM assignments a
                 JOIN projects p ON p.id = a.project_id
                 JOIN reviews r ON r.project_id = a.project_id AND r.judge_id = a.judge_id
                WHERE a.event_id = ? AND p.track_id = ? AND r.status = 'complete' AND p.status = 'submitted'""",
            (event_id, track["id"]),
            0,
        )
        by_track.append(
            {
                "track_id": track["id"],
                "track": track["name"],
                "required": required,
                "complete": done,
                "remaining": max(0, required - done),
            }
        )

    percent = round(100.0 * complete_reviews / required_reviews, 1) if required_reviews else 0.0

    # Completion policy (see JUDGING.md §Completion policy):
    # judging is COMPLETE only when *required assignment coverage* is satisfied —
    # i.e. every submitted project has at least one judge AND every assignment
    # has a completed review. "All currently assigned reviews are done" is NOT
    # enough, because it silently treats an unassigned project as finished.
    coverage_ok = bool(submitted) and not unassigned and not invalid_track and assigned_projects == submitted
    return {
        "event_id": event_id,
        "submitted_projects": submitted,
        "assigned_projects": assigned_projects,
        "required_reviews": required_reviews,
        "complete_reviews": complete_reviews,
        "pending_reviews": pending_reviews,
        "percent": percent,
        "by_track": by_track,
        "unassigned_projects": [dict(row) for row in unassigned],
        "invalid_track_assignments": len(invalid_track),
        "duplicate_assignments": len(duplicate),
        "judges_without_assignments": [dict(row) for row in idle_judges],
        "coverage_ok": coverage_ok,
        "all_reviews_complete": coverage_ok and required_reviews > 0 and pending_reviews == 0,
    }


def judge_queue(judge_id: int, event_id: int) -> list[dict[str, Any]]:
    """A judge's own queue. Assignment-scoped by construction."""
    rows = db.query_all(
        """SELECT a.id AS assignment_id, p.id AS project_id, p.name AS project_name,
                  p.tagline, p.status AS project_status, t.name AS track_name, t.id AS track_id,
                  r.id AS review_id, r.status AS review_status, r.raw_total, r.updated_at
             FROM assignments a
             JOIN projects p ON p.id = a.project_id
             JOIN judge_invites i ON i.event_id = a.event_id AND i.accepted_by = a.judge_id
             LEFT JOIN tracks t ON t.id = p.track_id
             LEFT JOIN reviews r ON r.project_id = a.project_id AND r.judge_id = a.judge_id
            WHERE a.judge_id = ? AND a.event_id = ? AND p.status = 'submitted'
              AND a.event_id = p.event_id AND a.track_id = p.track_id
              AND (i.track_id IS NULL OR i.track_id = p.track_id)
            ORDER BY (r.status = 'complete') ASC, p.id ASC""",
        (judge_id, event_id),
    )
    return [dict(row) for row in rows]