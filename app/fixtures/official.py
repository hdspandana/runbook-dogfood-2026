"""Adapter for the official DOGFOOD 2026 fixture format.

Input: root ``fixtures.json`` (event, tracks, judges, teams, projects, scores).
Output: the event-scoped rows consumed by the ordinary fixture loader.

The official fixture deliberately contains *duplicate team names* and a 41st
project for a team that already submitted. The app's constraints are stricter:
UNIQUE(event_id, team_id) is essential to enforce one project per team. We
preserve the first canonical project and count the duplicate separately. We do
NOT quietly create an illegal second project. The duplicate score batch is
ignored with the duplicate submission, with the collision reported in the
seed summary and docs.
"""

from __future__ import annotations

from typing import Any


def convert(raw: dict[str, Any]) -> dict[str, Any]:
    event = raw["event"]
    slug = "dogfood-2026-official-fixture"
    teams = raw.get("teams", [])
    track_by_id = {t["id"]: t for t in raw.get("tracks", [])}
    judge_by_id = {j["id"]: j for j in raw.get("judges", [])}
    team_by_id = {t["id"]: t for t in teams}

    # Names are not identifiers; fixture team IDs are. Duplicate display names
    # get their stable suffix rather than collapsing distinct teams.
    seen_names: dict[str, int] = {}
    canonical_names: dict[str, str] = {}
    for team in teams:
        name = str(team["name"])
        seen_names[name] = seen_names.get(name, 0) + 1
        canonical_names[team["id"]] = name if seen_names[name] == 1 else f"{name} · {team['id']}"

    payload: dict[str, Any] = {
        "meta": {
            "source": "official DOGFOOD 2026 fixture",
            "original_event_id": event.get("id"),
            "original_projects": len(raw.get("projects", [])),
            "original_scores": len(raw.get("scores", [])),
            "duplicate_project_ids": [],
            "duplicate_team_display_names": [k for k, v in seen_names.items() if v > 1],
        },
        "users": [
            {"email": "organizer@example.org", "display_name": "Organizer (seeded)",
             "role": "organizer", "password": "organizer123"},
            {"email": "admin@example.org", "display_name": "Admin (seeded)",
             "role": "admin", "password": "admin123"},
        ],
        "events": [{
            "slug": slug,
            "name": event["name"],
            "tagline": "The official DOGFOOD 2026 acceptance fixture — live submissions, private judging, real results.",
            "location": "Self-hosted · offline",
            "timezone_label": "UTC",
            "starts_at": "2026-02-25T09:00:00Z",
            "submission_deadline": event["submissions_close"],
            "judging_starts_at": "2026-03-01T18:00:00Z",
            "judging_ends_at": "2026-12-31T23:59:59Z",
            "results_published_at": None,
            "created_by_email": "organizer@example.org",
        }],
        "tracks": [], "prizes": [], "teams": [], "projects": [], "rubric_criteria": [],
        "assignments": [], "judge_invites": [],
        "normalization_holdback": [{"event_slug": slug, "exclude_latest_complete": 6}],
    }

    for i, track in enumerate(raw.get("tracks", [])):
        payload["tracks"].append({
            "event_slug": slug, "name": track["name"], "slug": track["id"],
            "description": "Official fixture track", "sort_order": i,
        })
        payload["prizes"].append({
            "event_slug": slug, "track_slug": track["id"],
            "name": f"{track['name']} track winner", "description": "Top project in this track.", "rank": 1,
        })
    payload["prizes"].append({
        "event_slug": slug, "track_slug": None, "name": "Overall winner",
        "description": "Top overall project after normalization.", "rank": 1,
    })

    # Shared rubric, same max/weights across all tracks. The official fixture
    # supplies 0–5 scores in three dimensions; weights sum to 100%.
    for i, (name, weight) in enumerate([
        ("functionality", 40), ("quality", 35), ("innovation", 25),
    ]):
        payload["rubric_criteria"].append({
            "event_slug": slug, "name": name,
            "description": f"{name.title()} (official fixture criterion, 0–5)",
            "weight_pct": weight, "max_score": 5, "sort_order": i,
        })

    for judge in raw.get("judges", []):
        payload["users"].append({
            "email": judge["email"].lower(), "display_name": judge["name"],
            "role": "judge", "password": "judge123",
        })
        # A multi-track judge has an all-track invite; assignment rows remain
        # the final authority for resource access.
        payload["judge_invites"].append({
            "event_slug": slug, "email": judge["email"].lower(), "track_slug": None,
        })

    all_member_emails: set[str] = set()
    for team in teams:
        for member in team.get("members", []):
            if member not in all_member_emails:
                payload["users"].append({
                    "email": member.lower(), "display_name": member.split("@")[0].replace(".", " ").title(),
                    "role": "participant", "password": "participant123",
                })
                all_member_emails.add(member)
        members = team.get("members", [])
        if members:
            payload["teams"].append({
                "event_slug": slug, "name": canonical_names[team["id"]],
                "lead_email": members[0].lower(), "members": [m.lower() for m in members],
            })

    # First project of a team is canonical. The fixture's last project
    # duplicates tm_07; letting it in would violate UNIQUE(event_id,team_id).
    canonical_projects: dict[str, dict[str, Any]] = {}
    by_original_id: dict[str, str] = {}
    for project in raw.get("projects", []):
        team_id = project["team"]
        if team_id in canonical_projects:
            payload["meta"]["duplicate_project_ids"].append(project["id"])
            continue
        canonical_projects[team_id] = project
        by_original_id[project["id"]] = project["title"]
        track = track_by_id.get(project["track"])
        payload["projects"].append({
            "event_slug": slug, "team_name": canonical_names[team_id],
            "track_slug": track["id"] if track else None,
            "name": project["title"], "tagline": project.get("summary", ""),
            "description": project.get("summary") or "Official DOGFOOD 2026 fixture submission.",
            "repo_url": project.get("repo_url", ""), "demo_url": project.get("demo_url", ""),
            "web_url": "", "status": "submitted" if project.get("submitted_at") else "draft",
            "submitted_at": project.get("submitted_at"),
        })

    skipped_scores = 0
    for score in raw.get("scores", []):
        project_name = by_original_id.get(score["project"])
        judge = judge_by_id.get(score["judge"])
        if not project_name or not judge:
            skipped_scores += 1
            continue
        criteria = score.get("criteria") or {}
        # Absent/partial criterion scores remain PENDING, never zero.
        complete = all(k in criteria and criteria[k] is not None
                       for k in ("functionality", "quality", "innovation"))
        payload["assignments"].append({
            "event_slug": slug, "project_name": project_name,
            "judge_email": judge["email"].lower(), "complete": complete,
            "comment": score.get("comment", ""),
            "submitted_at": "2026-03-02T12:00:00Z" if complete else None,
            "scores": criteria if complete else {},
        })
    payload["meta"]["skipped_duplicate_project_scores"] = skipped_scores

    # Pending slots for real incomplete batches: a project that has only two
    # completed reviews gets a third (unscored) assignment. The extra rows are
    # plainly identified as generated so the original 126 scores remain
    # attributable to the official fixture.
    eligible = raw.get("judges", [])
    used_pairs = {(a["project_name"], a["judge_email"]) for a in payload["assignments"]}
    added_pending = 0
    for i, project in enumerate(payload["projects"]):
        if i % 3 != 0:
            continue
        for offset in range(len(eligible)):
            candidate = eligible[(i + offset) % len(eligible)]["email"].lower()
            if (project["name"], candidate) in used_pairs:
                continue
            payload["assignments"].append({
                "event_slug": slug, "project_name": project["name"],
                "judge_email": candidate, "complete": False,
                "comment": "", "submitted_at": None, "scores": {},
            })
            used_pairs.add((project["name"], candidate))
            added_pending += 1
            break
    payload["meta"]["generated_pending_slots"] = added_pending
    return payload
