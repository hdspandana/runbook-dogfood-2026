#!/usr/bin/env python3
"""Deterministic generator for the RUNBOOK fixture set.

DOGFOOD ships a shared fixture dataset. We do not have it in this repository, so
this script *produces* a fixture set of the same shape and scale that the loader
(`app/fixtures/loader.py`) consumes — and the loader also accepts the official
file if you point `DOGFOOD_FIXTURES` at it.

The generated set is deliberately awkward, because happy-path data proves
nothing:

* a closed submission window with teams that never submitted,
* draft projects that must stay private,
* incomplete review batches (pending rows),
* a judge who scores everything identically (population σ = 0),
* a judge with a single completed review (insufficient sample),
* a judge with zero assignments (idle judge),
* strict and generous judges whose raw scores differ by design,
* tracks that a judge is *not* assigned to (cross-track isolation),
* a normalization snapshot that is 6 reviews behind (the STALE demo at boot).

Run:  python3 fixtures/build_fixtures.py > fixtures/fixtures.json
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 20260927
NOW = datetime(2026, 9, 27, 9, 0, 0, tzinfo=timezone.utc)

TRACKS = [
    ("Agentic Workflows", "agentic-workflows", "Autonomous agents that get real work done end to end."),
    ("Developer Tooling", "developer-tooling", "Tools that make engineers faster or happier."),
    ("Applied ML", "applied-ml", "Models in production with measurable outcomes."),
    ("Data & Observability", "data-observability", "Pipelines, telemetry and the truth about what happened."),
    ("Security & Trust", "security-trust", "Hardening, privacy and verifiable systems."),
    ("Edge & Embedded", "edge-embedded", "Software that runs where the network does not."),
    ("Sustainability", "sustainability", "Systems that reduce energy or material waste."),
    ("Open Data", "open-data", "Public-interest datasets made usable."),
]

RUBRIC = [
    ("Innovation", "Does this do something meaningfully new, or measurably better?", 25, 10),
    ("Execution", "Does it work? Is it engineered rather than demonstrated?", 30, 10),
    ("Impact", "Who benefits, and by how much, and how soon?", 25, 10),
    ("Presentation", "Can a stranger understand the project and its evidence?", 20, 10),
]

PROJECT_NOUNS = [
    "Ledger", "Compass", "Beacon", "Harbour", "Relay", "Quarry", "Lantern", "Forge",
    "Signal", "Anchor", "Prism", "Trellis", "Mosaic", "Verdict", "Ridge", "Cascade",
    "Atlas", "Beacon", "Cinder", "Drift", "Ember", "Foundry", "Gauge", "Hollow",
    "Isolde", "Jetty", "Kestrel", "Lattice", "Meridian", "Nimbus", "Orchard",
    "Palisade", "Quill", "Runway", "Slate", "Tidepool", "Umbra", "Vantage",
    "Warden", "Xenon", "Yardarm",
]

PROJECT_ADJ = [
    "Ambient", "Quiet", "Wintermute", "Copper", "Honest", "Low-latency", "Offline",
    "Federated", "Bare-metal", "Midnight", "Open", "Deterministic", "Wet", "Tidal",
    "Frugal", "Signal", "Salt", "Iron", "Paper", "Blackbox", "Daylight", "Rust",
    "Static", "Warm", "Sparse", "Dense", "Cold", "Long", "Short", "Narrow",
    "Wide", "Deep", "Shallow", "First", "Last", "Half", "Double", "Zero",
    "Whole", "Split", "Shared",
]

FIRST_NAMES = [
    "Priya", "Arun", "Meera", "Karthik", "Ananya", "Rohit", "Sneha", "Vikram", "Divya", "Nikhil",
    "Ishita", "Rahul", "Tanvi", "Aditya", "Ritu", "Sameer", "Neha", "Varun", "Pooja", "Manish",
    "Kavya", "Deepak", "Shruti", "Aravind", "Lakshmi", "Gaurav", "Pallavi", "Suresh", "Nandini", "Yash",
    "Bhavna", "Chandan", "Dhruv", "Esha", "Farhan", "Girish", "Hema", "Imran", "Jaya", "Kiran",
    "Leela", "Mohit", "Naina", "Omkar", "Preeti", "Rachit", "Sahana", "Tarun", "Uma", "Vivek",
]
LAST_NAMES = [
    "Rao", "Iyer", "Nair", "Sharma", "Patel", "Reddy", "Menon", "Gupta", "Verma", "Shetty",
    "Kulkarni", "Bose", "Chopra", "Desai", "Joshi", "Kapoor", "Lal", "Mishra", "Pillai", "Rajan",
]


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build() -> dict:
    rng = random.Random(SEED)
    users: list[dict] = []
    events: list[dict] = []
    tracks: list[dict] = []
    prizes: list[dict] = []
    teams: list[dict] = []
    projects: list[dict] = []
    rubric_rows: list[dict] = []
    assignments: list[dict] = []
    tiebreak: dict[str, list[tuple[str, str]]] = {}
    situations: list[dict] = []

    # ---------------------------------------------------------------- staff
    users.append({"email": "organizer@example.org", "display_name": "Nadia Okonjo (organizer)",
                  "role": "organizer", "password": "organizer123"})
    users.append({"email": "admin@example.org", "display_name": "Sam Ferreira (admin)",
                  "role": "admin", "password": "admin123"})

    # ----------------------------------------------------------- event A
    event_a = "dogfood-2026-raptors-open"
    events.append({
        "slug": event_a,
        "name": "DOGFOOD 2026 — Raptors Open",
        "tagline": "Ship something that survives contact with real users.",
        "location": "Bengaluru, IN + remote",
        "timezone_label": "UTC",
        "starts_at": utc(datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)),
        "submission_deadline": utc(datetime(2026, 3, 14, 18, 0, tzinfo=timezone.utc)),
        "judging_starts_at": utc(datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)),
        "judging_ends_at": utc(datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)),
        "results_published_at": None,
        "created_by_email": "organizer@example.org",
    })
    for order, (name, slug, description) in enumerate(TRACKS):
        tracks.append({"event_slug": event_a, "name": name, "slug": slug,
                       "description": description, "sort_order": order})
    prizes += [
        {"event_slug": event_a, "track_slug": None, "name": "Grand Prize", "rank": 1,
         "description": "Best overall project, by normalized score across all tracks."},
        {"event_slug": event_a, "track_slug": None, "name": "Runner-up", "rank": 2,
         "description": "Second place by normalized score."},
        {"event_slug": event_a, "track_slug": None, "name": "Most Useful Failure", "rank": None,
         "description": "For the team whose post-mortem taught the room the most."},
    ]
    for name, slug, description in TRACKS:
        prizes.append({"event_slug": event_a, "track_slug": slug, "name": f"{name} Track Winner",
                       "rank": 1, "description": f"Best project in {name}."})
    for name, description, weight, maximum in RUBRIC:
        rubric_rows.append({"event_slug": event_a, "name": name, "description": description,
                            "weight_pct": weight, "max_score": maximum,
                            "sort_order": len(rubric_rows)})

    # ------------------------------------------------- 30 judge accounts
    judge_personas: list[tuple[str, str, float, float, str | None]] = []
    # (email, persona, bias, spread, track preference slug)
    personas = (
        ["neutral"] * 18
        + ["generous"] * 4
        + ["strict"] * 4
        + ["flat"] * 1          # identical scores everywhere -> sigma == 0
        + ["sparse"] * 1        # a single completed review -> insufficient sample
        + ["idle"] * 1          # never assigned anything
        + ["neutral"] * 1
    )
    for index, persona in enumerate(personas, start=1):
        bias = {"neutral": 0.0, "generous": 1.4, "strict": -1.6, "flat": 0.0,
                "sparse": 0.0, "idle": 0.0}[persona]
        spread = {"neutral": 1.1, "generous": 1.3, "strict": 0.9, "flat": 0.0,
                  "sparse": 1.0, "idle": 1.0}[persona]
        preference = None
        if index % 4 == 0:
            preference = TRACKS[(index // 4) % len(TRACKS)][1]
        email = f"judge{index:02d}@example.org"
        users.append({
            "email": email,
            "display_name": f"{FIRST_NAMES[(index * 7) % len(FIRST_NAMES)]} "
                            f"{LAST_NAMES[(index * 3) % len(LAST_NAMES)]} (judge {index:02d})",
            "role": "judge",
            "password": "judge123",
        })
        judge_personas.append((email, persona, bias, spread, preference))

    # ------------------------------------------- 41 teams, ~123 participants
    for index in range(1, 42):
        track_slug = TRACKS[(index - 1) % len(TRACKS)][1]
        size = rng.choice([2, 3, 3, 4])
        members = []
        for slot in range(size):
            email = f"p{index:02d}{slot + 1}@example.org"
            users.append({
                "email": email,
                "display_name": f"{FIRST_NAMES[(index * 5 + slot * 11) % len(FIRST_NAMES)]} "
                                f"{LAST_NAMES[(index * 7 + slot * 3) % len(LAST_NAMES)]}",
                "role": "participant",
                "password": "participant123",
            })
            members.append(email)
        team_name = f"{PROJECT_ADJ[index - 1]} {PROJECT_NOUNS[index - 1]}"
        teams.append({"event_slug": event_a, "name": team_name,
                      "lead_email": members[0], "members": members})

        # 3 teams never finished: they keep drafts that must stay private.
        draft_only = index in {7, 19, 33}
        submitted_at = utc(datetime(2026, 3, 12, 9, 0, tzinfo=timezone.utc) + timedelta(hours=index * 3))
        projects.append({
            "event_slug": event_a,
            "team_name": team_name,
            "track_slug": track_slug,
            "name": f"{PROJECT_ADJ[index - 1]} {PROJECT_NOUNS[index - 1]}",
            "tagline": _tagline(index, track_slug),
            "description": _description(index, track_slug),
            "repo_url": f"https://git.example.org/raptors/{PROJECT_NOUNS[index - 1].lower()}-{index}",
            "demo_url": f"https://{PROJECT_NOUNS[index - 1].lower()}{index}.demo.example.org",
            "web_url": "",
            "status": "draft" if draft_only else "submitted",
            "submitted_at": None if draft_only else submitted_at,
        })

    # ------------------------------------------------- judging personas etc
    tiebreak[event_a] = [(p["name"], p["team_name"]) for p in projects]

    # --------------------------------------------------- event B (live demo)
    event_b = "raptors-pilot-open-hack"
    events.append({
        "slug": event_b,
        "name": "Raptors Pilot Open Hack",
        "tagline": "A smaller open event used to exercise the live submission path.",
        "location": "Remote",
        "timezone_label": "UTC",
        "starts_at": utc(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)),
        "submission_deadline": utc(datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)),
        "judging_starts_at": utc(datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)),
        "judging_ends_at": utc(datetime(2026, 11, 30, 18, 0, tzinfo=timezone.utc)),
        "results_published_at": None,
        "created_by_email": "organizer@example.org",
    })
    pilot_tracks = TRACKS[:4]
    for order, (name, slug, description) in enumerate(pilot_tracks):
        tracks.append({"event_slug": event_b, "name": name, "slug": slug,
                       "description": description, "sort_order": order})
    prizes += [
        {"event_slug": event_b, "track_slug": None, "name": "Pilot Prize", "rank": 1,
         "description": "Best project in the pilot event."},
        {"event_slug": event_b, "track_slug": pilot_tracks[0][1], "name": "Agentic Track Winner",
         "rank": 1, "description": "Best autonomous-agent project."},
    ]
    for name, description, weight, maximum in RUBRIC:
        rubric_rows.append({"event_slug": event_b, "name": name, "description": description,
                            "weight_pct": weight, "max_score": maximum,
                            "sort_order": len(rubric_rows)})
    for index in range(1, 13):
        email = f"q{index:02d}@example.org"
        users.append({"email": email, "display_name": f"Pilot Participant {index}", "role": "participant",
                      "password": "participant123"})
        team_name = f"Pilot Team {index:02d}"
        teams.append({"event_slug": event_b, "name": team_name, "lead_email": email, "members": [email]})
        track_slug = pilot_tracks[(index - 1) % len(pilot_tracks)][1]
        submitted = index <= 8
        projects.append({
            "event_slug": event_b,
            "team_name": team_name,
            "track_slug": track_slug,
            "name": f"Pilot Project {index:02d}",
            "tagline": "A pilot submission used to exercise the live participant flow.",
            "description": "Short pilot description. This project exists so the organizer can watch "
                           "submissions, assignments and judging move while the portal runs.",
            "repo_url": f"https://git.example.org/pilot/{index}",
            "demo_url": "",
            "web_url": "",
            "status": "submitted" if submitted else "draft",
            "submitted_at": utc(datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)) if submitted else None,
        })

    # -------------------------------------------------------- assignments
    # Event A: round-robin within track, 3 judges per project, then injuries.
    pool_a = [p for p in judge_personas if p[1] != "idle"]
    projects_a = [p for p in projects if p["event_slug"] == event_a and p["status"] == "submitted"]
    by_track: dict[str, list[dict]] = {}
    for project in projects_a:
        by_track.setdefault(project["track_slug"], []).append(project)

    complete_flags: list[tuple[str, str, bool]] = []  # (project, judge, complete)
    for track_slug, group in sorted(by_track.items(), key=lambda item: item[0]):
        eligible = [p for p in pool_a if p[4] is None or p[4] == track_slug]
        eligible.sort(key=lambda p: p[0])
        for position, project in enumerate(group):
            for k in range(3):
                judge = eligible[(position + k) % len(eligible)]
                complete = rng.random() > 0.16
                complete_flags.append((project["name"], judge[0], complete))

    # judge 29 ("sparse") keeps exactly one assignment; judge 30 ("idle") keeps none.
    sparse_email = [p[0] for p in judge_personas if p[1] == "sparse"][0]
    idle_email = [p[0] for p in judge_personas if p[1] == "idle"][0]
    sparse_kept = False
    trimmed: list[tuple[str, str, bool]] = []
    for project_name, judge_email, complete in complete_flags:
        if judge_email == idle_email:
            continue
        if judge_email == sparse_email:
            if sparse_kept:
                continue
            sparse_kept = True
            complete = True
        trimmed.append((project_name, judge_email, complete))
    complete_flags = trimmed

    persona_map = {p[0]: p for p in judge_personas}
    quality = {}
    for index, project in enumerate(projects_a):
        quality[project["name"]] = rng.gauss(0.0, 1.25) + (1.2 if index % 11 == 0 else 0.0)

    criteria_names = [row[0] for row in RUBRIC]
    for project_name, judge_email, complete in complete_flags:
        _, persona, bias, spread, _pref = persona_map[judge_email]
        base = 5.6 + quality[project_name] + bias
        scores = {}
        for criterion in criteria_names:
            value = base + rng.gauss(0.0, spread)
            scores[criterion] = round(max(0.0, min(10.0, value)), 2)
        if persona == "flat":
            scores = {criterion: 7.0 for criterion in criteria_names}
        assignments.append({
            "event_slug": event_a,
            "project_name": project_name,
            "judge_email": judge_email,
            "complete": complete,
            "comment": "" if not complete else _comment(quality[project_name], rng),
            "submitted_at": utc(datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)) if complete else None,
            "scores": scores if complete else {},
        })

    # Event B: a light judging setup that is *not yet open* by design.
    pool_b = sorted({p[0] for p in judge_personas if p[1] == "neutral"})[:6]
    projects_b = [p for p in projects if p["event_slug"] == event_b and p["status"] == "submitted"]
    for position, project in enumerate(projects_b):
        for k in range(2):
            judge_email = pool_b[(position + k) % len(pool_b)]
            base = 5.4 + quality.get(project["name"], rng.gauss(0, 1.0))
            assignments.append({
                "event_slug": event_b,
                "project_name": project["name"],
                "judge_email": judge_email,
                "complete": False,
                "comment": "",
                "submitted_at": None,
                "scores": {},
            })

    situations.extend([
        {"situation": "closed submission window", "where": "event A submission_deadline",
         "why": "acceptance check 3 — a post-deadline submit must be rejected with 4xx"},
        {"situation": "3 teams with drafts only", "where": "event A projects at index 7, 19, 33",
         "why": "drafts must never appear in the public gallery or in judging"},
        {"situation": "incomplete review batches", "where": "event A reviews with status=pending",
         "why": "pending reviews must be exported as pending, never as zero"},
        {"situation": "judge with zero variance", "where": "the 'flat' persona judge",
         "why": "population sigma == 0 must yield z == 0 rather than a divide-by-zero"},
        {"situation": "judge with one completed review", "where": "the 'sparse' persona judge",
         "why": "insufficient sample must fall back to neutral normalization"},
        {"situation": "idle judge", "where": "the 'idle' persona judge",
         "why": "assignment coverage must report a judge with zero assignments"},
        {"situation": "cross-track isolation", "where": "track-scoped judge preferences",
         "why": "a judge must not read another track's protected judging data"},
        {"situation": "stale normalization snapshot", "where": "normalization_holdback",
         "why": "the STALE Runbook state is demonstrable at boot without interaction"},
    ])

    return {
        "meta": {
            "name": "RUNBOOK DOGFOOD 2026 fixture set",
            "seed": SEED,
            "generated_for": "DOGFOOD 2026 acceptance",
            "counts": {
                "users": len(users),
                "events": len(events),
                "tracks": len(tracks),
                "prizes": len(prizes),
                "teams": len(teams),
                "projects": len(projects),
                "rubric_criteria": len(rubric_rows),
                "assignments": len(assignments),
            },
            "situations": situations,
        },
        "users": users,
        "events": events,
        "tracks": tracks,
        "prizes": prizes,
        "teams": teams,
        "projects": projects,
        "rubric_criteria": rubric_rows,
        "assignments": assignments,
        "normalization_holdback": [
            {"event_slug": event_a, "exclude_latest_complete": 6},
        ],
    }


def _tagline(index: int, track_slug: str) -> str:
    track = dict((slug, name) for name, slug, _ in TRACKS)[track_slug]
    return f"{track} with a measurable claim attached."


def _description(index: int, track_slug: str) -> str:
    track = dict((slug, name) for name, slug, _ in TRACKS)[track_slug]
    return (
        f"Fixture project #{index} in the {track} track. "
        "It exists so that the gallery, assignment coverage, judging progress and cross-judge "
        "normalization all run against a realistic volume of data rather than a single happy path. "
        "The description is deliberately long enough to exercise truncation, search matching and the "
        "public project detail page."
    )


def _comment(quality: float, rng: random.Random) -> str:
    if quality > 1.0:
        return "Strong, well-evidenced build with a clear deployment story."
    if quality > -0.5:
        return "Solid execution; the impact claim needs a sharper baseline to be convincing."
    return rng.choice([
        "Interesting idea, but the current build is closer to a demo than a product.",
        "The engineering is real; the framing is thin. Show the failure modes you handled.",
        "Scope is too wide for the evidence provided.",
    ])


if __name__ == "__main__":
    payload = build()
    text = json.dumps(payload, indent=1, sort_keys=False)
    target = Path(__file__).with_name("fixtures.json")
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    target.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {target} ({len(text)} bytes)")
    print(json.dumps(payload["meta"]["counts"], indent=2))