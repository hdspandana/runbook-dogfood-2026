"""A small *live* second event for demonstrating the participant lifecycle.

This is supplementary demo data, not a replacement for the official fixture.
It lives alongside the official Sample Hack 2026 event, which is seeded from
`fixtures.json` unchanged. The pilot window spans late September–November
2026, so a participant can create, edit and submit a project during a live demo.
"""

from __future__ import annotations

from .. import db
from ..auth import service as auth_service
from ..util import new_token, now_utc, to_iso

SLUG = "runbook-live-pilot"


def seed_live_pilot() -> int:
    """Idempotently create a second, open event with tracks, prizes, rubric and
    a handful of teams/projects/judge assignments. Returns its event id."""
    existing = db.query_one("SELECT id FROM events WHERE slug = ?", (SLUG,))
    if existing:
        # Upgrade an already-seeded pilot from older versions (idempotent).
        _repair_pilot(existing["id"])
        return existing["id"]

    organizer = db.query_one("SELECT id FROM users WHERE role = 'organizer' ORDER BY id LIMIT 1")
    stamp = to_iso(now_utc())
    event_id = db.insert(
        """INSERT INTO events
           (name, slug, tagline, location, timezone_label, starts_at, submission_deadline,
            judging_starts_at, judging_ends_at, created_by, created_at)
           VALUES (?, ?, ?, ?, 'UTC', ?, ?, ?, ?, ?, ?)""",
        (
            "RUNBOOK Live Pilot", SLUG,
            "A live window to demonstrate team formation, draft, submission, and automatic deadline locking.",
            "Remote", "2026-09-01T09:00:00Z", "2026-11-14T18:00:00Z",
            "2026-11-16T00:00:00Z", "2026-11-30T18:00:00Z",
            organizer["id"] if organizer else None, stamp,
        ),
    )
    tracks = [
        ("Build & Ship", "build-ship", "Working software with a deployable path."),
        ("Open Tools", "open-tools", "Open-source tooling that solves a real problem."),
        ("Social Impact", "social-impact", "Technology that helps people directly."),
    ]
    track_ids = []
    for order, (name, slug, description) in enumerate(tracks):
        track_ids.append(db.insert(
            "INSERT INTO tracks (event_id, name, slug, description, sort_order) VALUES (?, ?, ?, ?, ?)",
            (event_id, name, slug, description, order),
        ))
        db.insert(
            "INSERT INTO prizes (event_id, track_id, name, description, rank) VALUES (?, ?, ?, ?, 1)",
            (event_id, track_ids[-1], f"{name} winner", "Top project in this track."),
        )
    db.insert(
        "INSERT INTO prizes (event_id, track_id, name, description, rank) VALUES (?, NULL, ?, ?, 1)",
        (event_id, "Pilot Grand Prize", "Top overall project."),
    )
    for order, (name, weight) in enumerate([
        ("Innovation", 25), ("Execution", 30), ("Impact", 25), ("Presentation", 20),
    ]):
        db.insert(
            """INSERT INTO rubric_criteria (event_id, name, description, weight_pct, max_score, sort_order)
               VALUES (?, ?, ?, ?, 10, ?)""",
            (event_id, name, "Pilot rubric criterion.", weight, order),
        )

    for i in range(1, 13):
        email = f"pilot{i:02d}@example.org"
        user = db.query_one("SELECT id FROM users WHERE email = ?", (email,))
        if user is None:
            user_id = auth_service.create_user(email, "participant123", f"Pilot Participant {i:02d}")
        else:
            user_id = user["id"]
        team_id = db.insert(
            "INSERT INTO teams (event_id, name, invite_token, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, f"Pilot Team {i:02d}", new_token(24), user_id, stamp),
        )
        db.insert(
            "INSERT INTO team_members (event_id, team_id, user_id, is_lead, created_at) VALUES (?, ?, ?, 1, ?)",
            (event_id, team_id, user_id, stamp),
        )
        if i <= 8:
            db.insert(
                """INSERT INTO projects
                   (event_id, team_id, track_id, name, tagline, description, repo_url, demo_url,
                    status, submitted_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '', 'submitted', ?, ?, ?)""",
                (event_id, team_id, track_ids[(i - 1) % len(track_ids)],
                 f"Pilot Project {i:02d}", "A submitted pilot project.",
                 "A pilot project with a submitted state, available for assignment and judging.",
                 f"https://git.example.org/pilot/{i:02d}", stamp, stamp, stamp),
            )
        elif i <= 10:
            db.insert(
                """INSERT INTO projects
                   (event_id, team_id, track_id, name, tagline, description, repo_url,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
                (event_id, team_id, track_ids[(i - 1) % len(track_ids)],
                 f"Pilot Draft {i:02d}", "Draft: not public.",
                 "A draft pilot project that must not appear in the public gallery.",
                 f"https://git.example.org/pilot/draft-{i:02d}", stamp, stamp),
            )

    # Enroll four fixture judge accounts in the pilot (two per project, all
    # tracks), then assign deterministically. They still cannot score before
    # the pilot's judging window opens.
    judges = db.query_all("SELECT id, email FROM users WHERE role = 'judge' ORDER BY id LIMIT 4")
    for judge in judges:
        db.insert(
            """INSERT INTO judge_invites
               (event_id, email, token, track_id, created_by, created_at, accepted_by, accepted_at)
               VALUES (?, ?, ?, NULL, ?, ?, ?, ?)""",
            (event_id, judge["email"], f"fixture-pilot-judge-{judge['id']}",
             organizer["id"] if organizer else 1, stamp, judge["id"], stamp),
        )
    from ..judging import service as judging

    judging.assign_round_robin(event_id, judges_per_project=2)
    return event_id


def _repair_pilot(event_id: int) -> None:
    """Ensure accepted judge accounts and pending assignment rows exist."""
    invited = db.query_scalar("SELECT COUNT(*) FROM judge_invites WHERE event_id = ?", (event_id,), 0)
    if invited == 0:
        organizer = db.query_one("SELECT id FROM users WHERE role = 'organizer' ORDER BY id LIMIT 1")
        stamp = to_iso(now_utc())
        judges = db.query_all("SELECT id, email FROM users WHERE role = 'judge' ORDER BY id LIMIT 4")
        for judge in judges:
            db.execute(
                """INSERT OR IGNORE INTO judge_invites
                   (event_id, email, token, track_id, created_by, created_at, accepted_by, accepted_at)
                   VALUES (?, ?, ?, NULL, ?, ?, ?, ?)""",
                (event_id, judge["email"], f"fixture-pilot-judge-{judge['id']}",
                 organizer["id"] if organizer else 1, stamp, judge["id"], stamp),
            )
    from ..judging import service as judging

    judging.assign_round_robin(event_id, judges_per_project=2)
