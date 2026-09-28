"""Fixture loading.

* Idempotent: seeding runs once per database, and every statement is
  `INSERT OR IGNORE`-shaped so a half-finished seed cannot corrupt an existing one.
* Tolerant: the DOGFOOD fixture file may contain entities we do not know about.
  Unknown keys are ignored; missing optional keys get defaults; review rows may
  be incomplete, duplicated in intent, or absurdly numerous.
* Honest: raw totals are not copied from the file — they are recomputed with the
  same function the judges' submissions use (`judging.service.weighted_raw_total`),
  so the fixture can never disagree with the running application.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .. import db
from ..auth import service as auth_service
from ..judging import normalization, service as judging
from ..util import now_utc, to_iso

REPO_ROOT = Path(__file__).resolve().parents[2]
# The supplied competition fixture lives at the repo root. Keep it unchanged;
# the loader adapts its shape in app/fixtures/official.py.
DEFAULT_FIXTURE = REPO_ROOT / "fixtures.json"


def fixture_path() -> Path:
    return Path(os.environ.get("DOGFOOD_FIXTURES", str(DEFAULT_FIXTURE)))


def load_json(path: Optional[Path] = None) -> dict[str, Any]:
    path = path or fixture_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):  # a bare list of entities is tolerated
        data = {"entities": data}
    if "event" in data and "scores" in data:
        from .official import convert
        data = convert(data)
    for key in ("users", "events", "tracks", "prizes", "teams", "projects",
                "rubric_criteria", "assignments", "judge_invites", "normalization_holdback"):
        data.setdefault(key, [])
    return data


def is_seeded() -> bool:
    return db.query_scalar("SELECT COUNT(*) FROM events", (), 0) > 0


def seed(force: bool = False, path: Optional[Path] = None, verbose: bool = False) -> dict[str, Any]:
    """Load the fixture set. Returns a summary of what was written."""
    from flask import current_app

    try:
        data = load_json(path)
    except FileNotFoundError:
        return {"seeded": False, "reason": "fixture file not found"}

    if is_seeded() and not force:
        return {"seeded": False, "reason": "database already contains events"}

    if force:
        for table in ("review_scores", "reviews", "assignments", "normalization_runs", "projects",
                      "team_members", "teams", "prizes", "rubric_criteria", "tracks",
                      "judge_invites", "events", "sessions", "users"):
            db.execute(f"DELETE FROM {table}")

    stamp = to_iso(now_utc())
    user_ids: dict[str, int] = {}
    event_ids: dict[str, int] = {}
    track_ids: dict[tuple[str, str], int] = {}
    team_ids: dict[tuple[str, str], int] = {}
    project_ids: dict[tuple[str, str], int] = {}
    criterion_ids: dict[tuple[str, str], int] = {}

    # ---------------------------------------------------------------- users
    for user in data["users"]:
        email = str(user["email"]).strip().lower()
        db.execute(
            """INSERT OR IGNORE INTO users (email, password_hash, display_name, role, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (email, auth_service.hash_password(str(user.get("password", "changeme"))),
             user.get("display_name") or email, user.get("role", "participant"), stamp),
        )
        user_ids[email] = db.query_scalar("SELECT id FROM users WHERE email = ?", (email,), 0)

    # --------------------------------------------------------------- events
    for event in data["events"]:
        db.execute(
            """INSERT OR IGNORE INTO events
               (name, slug, tagline, location, timezone_label, starts_at, submission_deadline,
                judging_starts_at, judging_ends_at, results_published_at, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["name"], event["slug"], event.get("tagline", ""), event.get("location", ""),
                event.get("timezone_label", "UTC"), event["starts_at"], event["submission_deadline"],
                event["judging_starts_at"], event["judging_ends_at"],
                event.get("results_published_at"),
                user_ids.get(str(event.get("created_by_email", "")).lower()),
                stamp,
            ),
        )
        event_ids[event["slug"]] = db.query_scalar("SELECT id FROM events WHERE slug = ?", (event["slug"],), 0)

    # --------------------------------------------------------------- tracks
    for track in data["tracks"]:
        event_id = event_ids.get(track["event_slug"])
        if not event_id:
            continue
        db.execute(
            """INSERT OR IGNORE INTO tracks (event_id, name, slug, description, sort_order)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, track["name"], track["slug"], track.get("description", ""), int(track.get("sort_order", 0))),
        )
        track_ids[(track["event_slug"], track["slug"])] = db.query_scalar(
            "SELECT id FROM tracks WHERE event_id = ? AND slug = ?", (event_id, track["slug"]), 0
        )

    # --------------------------------------------------------------- prizes
    for prize in data["prizes"]:
        event_id = event_ids.get(prize["event_slug"])
        if not event_id:
            continue
        track_id = track_ids.get((prize["event_slug"], prize.get("track_slug"))) if prize.get("track_slug") else None
        db.execute(
            """INSERT OR IGNORE INTO prizes (event_id, track_id, name, description, rank)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, track_id, prize["name"], prize.get("description", ""), prize.get("rank")),
        )

    # ---------------------------------------------------------------- teams
    for team in data["teams"]:
        event_id = event_ids.get(team["event_slug"])
        if not event_id:
            continue
        lead_email = str(team.get("lead_email", "")).lower()
        from ..util import new_token

        db.execute(
            """INSERT OR IGNORE INTO teams (event_id, name, invite_token, created_by, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, team["name"], new_token(24), user_ids.get(lead_email, 0), stamp),
        )
        team_id = db.query_scalar(
            "SELECT id FROM teams WHERE event_id = ? AND name = ?", (event_id, team["name"]), 0
        )
        team_ids[(team["event_slug"], team["name"])] = team_id
        members = team.get("members") or [lead_email]
        for member_email in members:
            member_email = str(member_email).lower()
            if member_email not in user_ids:
                continue
            db.execute(
                """INSERT OR IGNORE INTO team_members (event_id, team_id, user_id, is_lead, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, team_id, user_ids[member_email], 1 if member_email == lead_email else 0, stamp),
            )

    # ------------------------------------------------------------- projects
    for project in data["projects"]:
        event_id = event_ids.get(project["event_slug"])
        team_id = team_ids.get((project["event_slug"], project["team_name"]))
        if not event_id or not team_id:
            continue
        track_id = track_ids.get((project["event_slug"], project.get("track_slug")))
        db.execute(
            """INSERT OR IGNORE INTO projects
               (event_id, team_id, track_id, name, tagline, description, repo_url, demo_url, web_url,
                status, submitted_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, team_id, track_id, project["name"], project.get("tagline", ""),
                project.get("description", ""), project.get("repo_url", ""), project.get("demo_url", ""),
                project.get("web_url", ""), project.get("status", "draft"), project.get("submitted_at"),
                stamp, stamp,
            ),
        )
        project_ids[(project["event_slug"], project["name"])] = db.query_scalar(
            "SELECT id FROM projects WHERE event_id = ? AND team_id = ?", (event_id, team_id), 0
        )

    # ----------------------------------------------------- rubric criteria
    for criterion in data["rubric_criteria"]:
        event_id = event_ids.get(criterion["event_slug"])
        if not event_id:
            continue
        db.execute(
            """INSERT OR IGNORE INTO rubric_criteria
               (event_id, name, description, weight_pct, max_score, sort_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, criterion["name"], criterion.get("description", ""),
             float(criterion["weight_pct"]), float(criterion.get("max_score", 10)),
             int(criterion.get("sort_order", 0))),
        )
        criterion_ids[(criterion["event_slug"], criterion["name"])] = db.query_scalar(
            "SELECT id FROM rubric_criteria WHERE event_id = ? AND name = ?", (event_id, criterion["name"]), 0
        )

    # ------------------------------------------------------ judge invites
    for invite in data["judge_invites"]:
        event_id = event_ids.get(invite["event_slug"])
        email = str(invite["email"]).lower()
        if not event_id or email not in user_ids:
            continue
        track_id = track_ids.get((invite["event_slug"], invite.get("track_slug"))) if invite.get("track_slug") else None
        organizer_id = user_ids.get("organizer@example.org")
        if organizer_id is None:
            continue
        db.execute(
            """INSERT OR IGNORE INTO judge_invites
               (event_id, email, token, track_id, created_by, created_at, accepted_by, accepted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, email, new_token(24), track_id, organizer_id, stamp, user_ids[email], stamp),
        )

    # ---------------------------------------------- assignments and reviews
    created_assignments = 0
    created_reviews = 0
    for entry in data["assignments"]:
        event_id = event_ids.get(entry["event_slug"])
        project_id = project_ids.get((entry["event_slug"], entry["project_name"]))
        judge_email = str(entry["judge_email"]).lower()
        judge_id = user_ids.get(judge_email)
        if not (event_id and project_id and judge_id):
            continue
        project_track = db.query_scalar("SELECT track_id FROM projects WHERE id = ?", (project_id,), None)
        cursor = db.execute(
            """INSERT OR IGNORE INTO assignments (event_id, project_id, track_id, judge_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, project_id, project_track, judge_id, stamp),
        )
        if cursor.rowcount:
            created_assignments += 1
        assignment_id = db.query_scalar(
            "SELECT id FROM assignments WHERE project_id = ? AND judge_id = ?", (project_id, judge_id), None
        )
        complete = bool(entry.get("complete")) and bool(entry.get("scores"))
        criteria = judging.rubric_for_event(event_id)
        scores = {
            criterion_ids[(entry["event_slug"], name)]: float(value)
            for name, value in (entry.get("scores") or {}).items()
            if (entry["event_slug"], name) in criterion_ids
        }
        raw_total = judging.weighted_raw_total(criteria, scores) if complete else None
        cursor = db.execute(
            """INSERT OR IGNORE INTO reviews
               (event_id, project_id, judge_id, assignment_id, status, raw_total, comment,
                submitted_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, project_id, judge_id, assignment_id, "complete" if complete else "pending",
             raw_total, entry.get("comment", ""), entry.get("submitted_at"), stamp),
        )
        if cursor.rowcount:
            created_reviews += 1
        review_id = db.query_scalar(
            "SELECT id FROM reviews WHERE project_id = ? AND judge_id = ?", (project_id, judge_id), None
        )
        for criterion_id, value in scores.items():
            db.execute(
                "INSERT OR IGNORE INTO review_scores (review_id, criterion_id, score) VALUES (?, ?, ?)",
                (review_id, criterion_id, value),
            )

    # --------------------------------------------- normalization holdback
    held_back = 0
    for holdback in data["normalization_holdback"]:
        event_id = event_ids.get(holdback["event_slug"])
        count = int(holdback.get("exclude_latest_complete", 0))
        if not event_id or count <= 0:
            continue
        victims = db.query_all(
            """SELECT id FROM reviews
                WHERE event_id = ? AND status = 'complete'
                ORDER BY (submitted_at IS NULL), submitted_at DESC, id DESC
                LIMIT ?""",
            (event_id, count),
        )
        if not victims:
            continue
        ids = [int(v["id"]) for v in victims]
        placeholders = ",".join("?" for _ in ids)
        # Generate the snapshot as if those reviews had not happened yet, then
        # restore them: the published result is now provably stale, which is the
        # state RUNBOOK is built to detect.
        db.execute(f"UPDATE reviews SET status = 'pending' WHERE id IN ({placeholders})", ids)
        normalization.run_and_store(event_id, None)
        db.execute(f"UPDATE reviews SET status = 'complete' WHERE id IN ({placeholders})", ids)
        held_back = len(ids)

    summary = {
        "seeded": True,
        "users": len(user_ids),
        "events": len(event_ids),
        "tracks": len(track_ids),
        "teams": len(team_ids),
        "projects": len(project_ids),
        "assignments_created": created_assignments,
        "reviews_created": created_reviews,
        "normalization_holdback": held_back,
        "fixture": str(path or fixture_path()),
    }
    if verbose:
        print(json.dumps(summary, indent=2))
    return summary


def seed_if_empty(verbose: bool = False) -> dict[str, Any]:
    if is_seeded():
        return {"seeded": False, "reason": "already seeded"}
    return seed(verbose=verbose)