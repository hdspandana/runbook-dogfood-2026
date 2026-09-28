"""The Runbook: a live operational view computed from database facts.

DOGFOOD's differentiator for this entry. A Runbook card is NEVER persisted and
NEVER set by a checkbox. Every card is recomputed from rows on every read:

    database → workflow facts → derived status → explanation → next action

States are exactly the five required ones:

    UPCOMING  the stage has not become active yet (time or dependency)
    ACTIVE    the stage is in progress / actionable now
    COMPLETE  the stage's requirements are currently satisfied
    BLOCKED   a real dependency or data problem needs attention
    STALE     a previously completed result is no longer current

Runbook is informational: it never authorizes or denies an action. In
particular a BLOCKED or STALE card does not close any route — CSV export stays
available to organizers at every judging stage (see exports/routes.py, and the
regression test tests/test_runbook_engine.py).
"""

from __future__ import annotations

from typing import Any, Optional

from .. import db
from ..events import service as events_service
from ..judging import normalization, service as judging
from ..util import countdown, now_utc, parse_iso, pretty_utc

STEPS = [
    ("event_setup", "Event setup"),
    ("tracks_prizes", "Tracks & prizes"),
    ("rubric", "Judging rubric"),
    ("assignments", "Judge assignment"),
    ("submissions", "Submissions"),
    ("judging", "Judging"),
    ("normalization", "Normalization"),
    ("results", "Results / export"),
]

PRIORITY = {key: index for index, (key, _) in enumerate(STEPS)}


def _card(step: str, title: str, status: str, metric: str, reason: str,
          next_action: str, action_url: str = "", evidence: Optional[list[str]] = None,
          observation: bool = False) -> dict[str, Any]:
    return {
        "step": step,
        "title": title,
        "status": status,
        "metric": metric,
        "reason": reason,
        "next_action": next_action,
        "action_url": action_url,
        "evidence": evidence or [],
        "observation": observation,
    }


def build(event_id: Optional[int] = None) -> dict[str, Any]:
    """Return the full Runbook for an event (or the absence of one)."""
    event = events_service.get_event(event_id) if event_id else events_service.primary_event()
    if event is None:
        cards = [_card("event_setup", "Event setup", "BLOCKED", "No event configured",
                       "There is no event in the database, so nothing downstream can be evaluated.",
                       "Create an event", "/organizer/events/new")]
        for key, title in STEPS[1:]:
            cards.append(_card(key, title, "UPCOMING", "—",
                               "Waiting for an event to be configured.", "Configure the event first",
                               "/organizer/events/new"))
        return {
            "event": None,
            "generated_at": now_utc().isoformat(),
            "steps": cards,
            "next_action": _next_action(cards),
            "summary": {"total": len(cards), "complete": 0, "active": 0, "upcoming": len(cards) - 1,
                        "blocked": 1, "stale": 0},
        }

    now = now_utc()
    summary = events_service.counts(event["id"])
    timing = events_service.timing_summary(event, now)
    progress = judging.judging_progress(event["id"])
    freshness = normalization.freshness(event["id"])
    from ..judging import publication
    published_state = publication.state(event, freshness, progress)

    cards = [
        _event_setup_card(event, timing),
        _tracks_prizes_card(event, summary),
        _rubric_card(event),
        _assignments_card(event, progress, summary, timing),
        _submissions_card(event, summary, timing, now),
        _judging_card(event, progress, timing, now),
        _normalization_card(event, freshness, progress, timing),
        _results_card(event, freshness, progress, timing, published_state),
    ]

    return {
        "event": {"id": event["id"], "name": event["name"], "slug": event["slug"],
                  "phase": timing["phase_label"], "deadline": pretty_utc(event["submission_deadline"]),
                  "judging_ends_at": pretty_utc(event["judging_ends_at"])},
        "generated_at": timing["now"].isoformat(),
        "steps": cards,
        "next_action": _next_action(cards),
        "summary": {
            "total": len(cards),
            "complete": sum(1 for c in cards if c["status"] == "COMPLETE"),
            "active": sum(1 for c in cards if c["status"] == "ACTIVE"),
            "upcoming": sum(1 for c in cards if c["status"] == "UPCOMING"),
            "blocked": sum(1 for c in cards if c["status"] == "BLOCKED"),
            "stale": sum(1 for c in cards if c["status"] == "STALE"),
        },
        "progress": progress,
        "normalization": freshness,
        "publication": {"state": published_state, "published_at": event["results_published_at"]},
    }


# --------------------------------------------------------------------------
# 1. event setup
# --------------------------------------------------------------------------
def _event_setup_card(event, timing) -> dict[str, Any]:
    problems: list[str] = []
    if not (event["name"] or "").strip():
        problems.append("the event has no name")
    deadline = parse_iso(event["submission_deadline"])
    starts = parse_iso(event["starts_at"])
    if deadline is None:
        problems.append("the submission deadline is not a valid UTC timestamp")
    elif starts and deadline <= starts:
        problems.append("the submission deadline is not after the event start")

    if problems:
        return _card(
            "event_setup", "Event setup", "BLOCKED", "Invalid event configuration",
            "Cannot evaluate the event because " + "; ".join(problems) + ".",
            "Fix the event configuration", f"/organizer/events/{event['id']}/edit",
            evidence=problems,
        )

    if timing["deadline_passed"]:
        metric = f"Submission deadline passed {timing['deadline_countdown']} ago"
        reason = f"Submissions closed automatically at {timing['deadline_display']}."
    else:
        metric = f"Submissions close in {timing['deadline_countdown']}"
        reason = f"Event is in phase “{timing['phase_label']}” and the deadline is enforced server-side in UTC."
    return _card(
        "event_setup", "Event setup", "COMPLETE", metric, reason,
        "No action required",
        f"/events/{event['id']}",
        evidence=[f"Phase: {timing['phase_label']}",
                  f"Submission deadline: {timing['deadline_display']}",
                  f"Judging window ends: {pretty_utc(event['judging_ends_at'])}"],
    )


# --------------------------------------------------------------------------
# 2. tracks & prizes
# --------------------------------------------------------------------------
def _tracks_prizes_card(event, summary) -> dict[str, Any]:
    tracks, prizes = summary["tracks"], summary["prizes"]
    if tracks == 0:
        return _card(
            "tracks_prizes", "Tracks & prizes", "BLOCKED", "0 tracks defined",
            "Projects must belong to a track and judges are scoped by track, so nothing can be "
            "submitted or assigned until at least one track exists.",
            "Define tracks", f"/organizer/events/{event['id']}/tracks",
            evidence=["Tracks are required for submission, gallery filtering and track-scoped judging."],
        )
    if prizes == 0:
        return _card(
            "tracks_prizes", "Tracks & prizes", "ACTIVE", f"{tracks} tracks · 0 prizes",
            "Tracks exist, but no prizes are configured, so proposed projects cannot be told what "
            "they are competing for.",
            "Add at least one prize", f"/organizer/events/{event['id']}/tracks",
            evidence=[f"{tracks} tracks defined."],
        )
    return _card(
        "tracks_prizes", "Tracks & prizes", "COMPLETE", f"{tracks} tracks · {prizes} prizes",
        "Tracks and prizes are configured and visible on the public event page.",
        "No action required", f"/events/{event['id']}",
        evidence=[f"{tracks} tracks, {prizes} prizes (event-wide and per-track)."],
    )


# --------------------------------------------------------------------------
# 3. rubric
# --------------------------------------------------------------------------
def _rubric_card(event) -> dict[str, Any]:
    criteria = judging.rubric_for_event(event["id"])
    valid, message = judging.validate_rubric(criteria)
    if not criteria:
        return _card(
            "rubric", "Judging rubric", "BLOCKED", "No rubric criteria",
            "Judges have nothing to score against, so no review can be completed.",
            "Configure the rubric", f"/organizer/events/{event['id']}/rubric",
            evidence=["Raw totals are Σ score × weight / 100; without criteria that sum is undefined."],
        )
    if not valid:
        return _card(
            "rubric", "Judging rubric", "BLOCKED", message,
            "The weighted scoring model requires valid weights before judging can produce comparable results.",
            "Fix the rubric weights", f"/organizer/events/{event['id']}/rubric",
            evidence=[f"{len(criteria)} criteria defined; {message}"],
        )
    return _card(
        "rubric", "Judging rubric", "COMPLETE", f"{len(criteria)} criteria · weights 100%",
        f"Weighted rubric is valid (max raw total {judging.rubric_max_total(criteria):g}). "
        "Edits to the rubric will mark a persisted normalized result as STALE.",
        "No action required", f"/organizer/events/{event['id']}/rubric",
        evidence=[f"{c['name']}: {c['weight_pct']:g}% of {c['max_score']:g}" for c in criteria],
    )


# --------------------------------------------------------------------------
# 4. assignments
# --------------------------------------------------------------------------
def _assignments_card(event, progress, summary, timing) -> dict[str, Any]:
    if summary["submitted"] == 0:
        status = "UPCOMING" if not timing["deadline_passed"] else "BLOCKED"
        reason = (
            "Nothing to assign yet — no project has been submitted."
            if status == "UPCOMING"
            else "The submission window closed with zero submitted projects, so judging cannot start."
        )
        return _card(
            "assignments", "Judge assignment", status, "0 submitted projects",
            reason, "Wait for submissions" if status == "UPCOMING" else "Investigate submissions",
            f"/organizer/events/{event['id']}/assignments",
        )

    judges = db.query_scalar(
        """SELECT COUNT(*) FROM judge_invites i JOIN users u ON u.id = i.accepted_by
            WHERE i.event_id = ? AND u.role = 'judge'""", (event["id"],), 0
    )
    problems = []
    if judges == 0:
        problems.append("no judge accounts exist")
    if progress["unassigned_projects"]:
        problems.append(
            f"{len(progress['unassigned_projects'])} submitted project(s) have no judge assignment"
        )
    if progress["invalid_track_assignments"]:
        problems.append(f"{progress['invalid_track_assignments']} assignment(s) point at the wrong track")
    if progress["duplicate_assignments"]:
        problems.append(f"{progress['duplicate_assignments']} duplicate assignment(s)")

    if problems:
        return _card(
            "assignments", "Judge assignment", "BLOCKED",
            f"{progress['assigned_projects']} / {progress['submitted_projects']} projects assigned",
            "Coverage is incomplete: " + "; ".join(problems) + ".",
            "Review assignments", f"/organizer/events/{event['id']}/assignments",
            evidence=problems + [f"{judges} judge account(s) in the system."],
        )

    if progress["judges_without_assignments"]:
        idle = len(progress["judges_without_assignments"])
        return _card(
            "assignments", "Judge assignment", "ACTIVE",
            f"{progress['assigned_projects']} / {progress['submitted_projects']} projects assigned",
            f"Every submitted project has judges, but {idle} judge account(s) hold no assignments in this event.",
            "No required action — judge load is uneven",
            f"/organizer/events/{event['id']}/assignments",
            evidence=[f"{j['display_name']} has no assignment" for j in progress["judges_without_assignments"][:5]],
        )

    return _card(
        "assignments", "Judge assignment", "COMPLETE",
        f"{progress['assigned_projects']} / {progress['submitted_projects']} projects assigned",
        f"Round-robin-within-track coverage is complete: {progress['required_reviews']} required reviews across "
        f"{progress['submitted_projects']} submitted projects.",
        "No action required", f"/organizer/events/{event['id']}/assignments",
        evidence=["Duplicate assignments are impossible by schema (UNIQUE(project_id, judge_id))."],
    )


# --------------------------------------------------------------------------
# 5. submissions (observation stage — the system does not ask the organizer to
#    do something the deadline already did)
# --------------------------------------------------------------------------
def _submissions_card(event, summary, timing, now) -> dict[str, Any]:
    teams = summary["teams"]
    submitted = summary["submitted"]
    drafts = summary["drafts"]

    if timing["phase"] == "UPCOMING":
        return _card(
            "submissions", "Submissions", "UPCOMING", f"Window opens {pretty_utc(event['starts_at'])}",
            f"Submission opens in {countdown(parse_iso(event['starts_at']), now)}; "
            f"{teams} team(s) already registered.",
            "No action required", f"/events/{event['id']}",
            observation=True,
        )

    if not timing["deadline_passed"]:
        missing = max(0, teams - submitted)
        return _card(
            "submissions", "Submissions", "ACTIVE", f"{submitted} / {teams} teams submitted",
            f"{missing} registered team(s) have not submitted yet; the window closes in "
            f"{timing['deadline_countdown']} ({timing['deadline_display']}) and will close automatically.",
            f"No organizer action required — the deadline closes submissions at {timing['deadline_display']}",
            f"/organizer/events/{event['id']}/submissions",
            evidence=[f"{drafts} project(s) still in draft and therefore not public."],
            observation=True,
        )

    missing = max(0, teams - submitted)
    return _card(
        "submissions", "Submissions", "COMPLETE",
        f"{submitted} / {teams} teams submitted",
        f"Submissions closed automatically at {timing['deadline_display']} "
        f"({timing['deadline_countdown']} ago) — {submitted}/{teams} teams submitted, "
        f"{missing} team(s) did not. {drafts} draft project(s) stay private and are not judgeable or public.",
        "No action required — the deadline closed submissions automatically",
        f"/organizer/events/{event['id']}/submissions",
        evidence=["Deadline enforcement is server-side UTC on every participant mutation."],
        observation=True,
    )


# --------------------------------------------------------------------------
# 6. judging
# --------------------------------------------------------------------------
def _judging_card(event, progress, timing, now) -> dict[str, Any]:
    required, complete, pending = progress["required_reviews"], progress["complete_reviews"], progress["pending_reviews"]

    if progress["invalid_track_assignments"] or progress["unassigned_projects"]:
        gaps = len(progress["unassigned_projects"])
        invalid = progress["invalid_track_assignments"]
        reasons = []
        if gaps:
            reasons.append(f"{gaps} submitted project(s) have no judge assignment")
        if invalid:
            reasons.append(f"{invalid} assignment(s) are invalid for their event/track/judge scope")
        return _card(
            "judging", "Judging", "BLOCKED", f"{complete} / {required or '—'} reviews complete",
            "; ".join(reasons) + ". Required review coverage cannot be reached until these are fixed.",
            "Fix assignments first", f"/organizer/events/{event['id']}/assignments",
            evidence=[f"{p['name']} has no judge assignment" for p in progress["unassigned_projects"][:5]]
                     + ([f"{invalid} invalid assignment(s)"] if invalid else []),
        )

    if required == 0:
        return _card("judging", "Judging", "UPCOMING", "No assignments yet",
                     "Judging cannot start because no reviews are required yet.",
                     "Assign judges", f"/organizer/events/{event['id']}/assignments")

    j_start = parse_iso(event["judging_starts_at"])
    if j_start and now < j_start:
        return _card(
            "judging", "Judging", "UPCOMING", f"0 / {required} reviews complete",
            f"Judging opens in {countdown(j_start, now)} ({pretty_utc(j_start)}).",
            "No action required", f"/organizer/events/{event['id']}/progress",
        )

    judging_end = parse_iso(event["judging_ends_at"])
    if judging_end is not None and now >= judging_end:
        if progress["all_reviews_complete"]:
            return _card(
                "judging", "Judging", "COMPLETE", f"{complete} / {required} reviews complete",
                f"Judging closed at {pretty_utc(event['judging_ends_at'])} with all required reviews complete; "
                "judge scores are now locked.",
                "Run normalization", f"/organizer/events/{event['id']}/progress",
            )
        return _card(
            "judging", "Judging", "BLOCKED", f"{complete} / {required} reviews complete · {pending} missing",
            f"The judging window closed at {pretty_utc(event['judging_ends_at'])} with {pending} review(s) never "
            "completed. Pending reviews are not zero scores — they are simply absent from normalization.",
            "Extend the judging window and complete the missing reviews",
            f"/organizer/events/{event['id']}/progress",
            evidence=[t["track"] + f": {t['remaining']} missing" for t in progress["by_track"] if t["remaining"]],
        )

    if progress["all_reviews_complete"]:
        return _card(
            "judging", "Judging", "COMPLETE", f"{complete} / {required} reviews complete",
            "All required reviews are complete. Further edits by judges will change the required-review "
            "fingerprint and mark the normalized result STALE.",
            "Run normalization" if normalization.latest_run(event["id"]) is None else "Re-run normalization if reviews change",
            f"/organizer/events/{event['id']}/progress",
        )

    return _card(
        "judging", "Judging", "ACTIVE",
        f"{complete} / {required} required reviews complete",
        f"{pending} review(s) remaining ({progress['percent']}% complete) with "
        f"{timing['judging_countdown']} left in the judging window. Coverage is healthy across "
        f"{len(progress['by_track'])} track(s).",
        "Continue judging", f"/organizer/events/{event['id']}/progress",
        evidence=[f"{t['track']}: {t['complete']} / {t['required']}" for t in progress["by_track"]],
    )


# --------------------------------------------------------------------------
# 7. normalization — the signature card
# --------------------------------------------------------------------------
def _normalization_card(event, freshness, progress, timing) -> dict[str, Any]:
    if progress["complete_reviews"] == 0 and freshness["state"] != "stale":
        return _card(
            "normalization", "Normalization", "UPCOMING", "0 completed reviews",
            "There is nothing to normalize until at least one review is complete. "
            + ("The stored empty snapshot matches current inputs but is not a final result."
               if freshness["state"] == "current" else "No snapshot exists yet."),
            "Continue judging first", f"/organizer/events/{event['id']}/progress",
        )

    judging_done = progress["coverage_ok"] and progress["all_reviews_complete"]
    if freshness["state"] == "never_run":
        return _card(
            "normalization", "Normalization", "ACTIVE" if judging_done else "UPCOMING",
            f"{freshness['current_review_count']} completed reviews not yet normalized",
            "No normalized result has ever been generated. "
            + ("Required judging coverage is complete." if judging_done else
               "Finish all required judging coverage before generating final results."),
            "Run normalization" if judging_done else "Continue judging first",
            f"/organizer/events/{event['id']}/results" if judging_done else
            f"/organizer/events/{event['id']}/progress",
            evidence=["Method: per-judge z-score with population σ."],
        )

    if freshness["state"] == "current":
        return _card(
            "normalization", "Normalization", "COMPLETE",
            f"Normalized {freshness['current_review_count']} reviews",
            f"Generated at {pretty_utc(freshness['stored_at'])}. The completed-review fingerprint matches, "
            f"so this result is current ({freshness['current_pending_count']} pending review(s) excluded).",
            "Export results" if judging_done else "Continue judging first",
            f"/organizer/events/{event['id']}/export.csv" if judging_done else
            f"/organizer/events/{event['id']}/progress",
            evidence=[f"Fingerprint {freshness['current_fingerprint'][:12]}…"],
        )

    # STALE
    delta = freshness["delta"]
    if delta > 0:
        delta_text = f"{delta} new review(s) detected (completed)."
    elif delta < 0:
        delta_text = f"{abs(delta)} completed review(s) are no longer part of the result (removed or re-opened)."
    else:
        delta_text = "Scores, assignments, submitted projects, rubric, or result metadata changed since the snapshot."
    reasons = []
    if freshness["rubric_changed"]:
        reasons.append("the rubric was edited after this result was generated")

    return _card(
        "normalization", "Normalization", "STALE",
        f"Last normalization: {freshness['stored_review_count']} reviews · Current: {freshness['current_review_count']}",
        f"Produced at {pretty_utc(freshness['stored_at'])} from different result inputs. "
        f"{delta_text}" + (" Also, " + " and ".join(reasons) + "." if reasons else "")
        + (" Re-run normalization after required judging coverage is complete."
           if not judging_done else ""),
        "Re-run normalization" if judging_done else "Continue judging first",
        f"/organizer/events/{event['id']}/results" if judging_done else
        f"/organizer/events/{event['id']}/progress",
        evidence=[
            f"Stored fingerprint {str(freshness.get('stored_fingerprint'))[:12]}…",
            f"Current fingerprint {freshness['current_fingerprint'][:12]}…",
            f"{freshness['current_pending_count']} pending review(s) excluded from both.",
        ],
    )


# --------------------------------------------------------------------------
# 8. results / export
# --------------------------------------------------------------------------
def _results_card(event, freshness, progress, timing, published_state) -> dict[str, Any]:
    export_url = f"/organizer/events/{event['id']}/export.csv"
    published = parse_iso(event["results_published_at"])

    if not progress["all_reviews_complete"]:
        return _card(
            "results", "Results / export", "BLOCKED",
            f"{progress['complete_reviews']} / {progress['required_reviews']} required reviews complete",
            "Final results cannot be published until every submitted project is assigned and all "
            "required reviews are complete. Operational CSV export is available now, even in BLOCKED state."
            + (" Previously published results are now STALE." if published_state == "stale" else ""),
            "Download operational CSV (continue judging before publication)", export_url,
            evidence=["Runbook state never controls export authorization."],
        )

    if freshness["state"] == "never_run":
        return _card(
            "results", "Results / export", "UPCOMING", "No normalized result yet",
            "Final ranked results do not exist yet. Operational CSV export is available now regardless of "
            "Runbook state — it simply reports pending reviews as pending.",
            "Download operational CSV", export_url,
            evidence=["Export is never gated on Runbook status."],
        )

    if freshness["state"] == "stale":
        return _card(
            "results", "Results / export", "STALE",
            f"Stored ranking is {abs(freshness['delta'])} review(s) behind"
            if freshness["delta"] else "Stored ranking predates the latest review changes",
            ("Previously published results are STALE. " if published_state == "stale" else "")
            + "The CSV will state that the last persisted normalized result is stale and will stamp the export "
            "accordingly; it still contains the current live scores for completed reviews. "
            "Re-running normalization refreshes the stored ranking.",
            "Re-run normalization", f"/organizer/events/{event['id']}/results",
            evidence=[f"Last normalization {pretty_utc(freshness['stored_at'])}."],
        )

    if published_state == "current":
        return _card(
            "results", "Results / export", "COMPLETE", f"Results marked published {pretty_utc(published)}",
            "Normalized results are current and publication was recorded. The public gallery remains "
            "project-only; it never exposes per-judge data or a leaderboard. Organizers can download the ranking.",
            "Download CSV", export_url,
        )

    return _card(
        "results", "Results / export", "ACTIVE",
        f"{progress['complete_reviews']} reviews normalized, "
        + ("previous publication stale" if published_state == "stale" else "not yet published"),
        ("A previous publication is STALE and must be renewed. " if published_state == "stale" else "")
        + "All required judging reviews are complete and the stored normalized result is current. "
        "Publication records the final result; per-judge data remains private.",
        "Publish results or download the CSV", export_url,
        evidence=["Public pages never expose judge scores, comments or identity."],
    )


# --------------------------------------------------------------------------
# next action engine — deterministic, dependency-ordered
# --------------------------------------------------------------------------
def _next_action(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the earliest unmet *prerequisite*, never a downstream stale card.

    Submissions is time-driven rather than an organizer checkbox: while open,
    assignments and judging may proceed; after closure, coverage is final.
    Results BLOCKED by judging is informative, not a higher-priority action.
    """
    by_key = {card["step"]: card for card in cards}
    blocked = [c["step"] for c in cards if c["status"] == "BLOCKED"]
    stale = [c["step"] for c in cards if c["status"] == "STALE"]
    chosen = None
    for key in ("event_setup", "tracks_prizes", "rubric", "assignments"):
        card = by_key[key]
        # An invited judge with zero assignments is a load observation, not
        # missing project coverage. It cannot hold the entire event hostage.
        if key == "assignments" and card["status"] == "ACTIVE" and \
                card["next_action"].startswith("No required action"):
            continue
        if card["status"] != "COMPLETE":
            chosen = card
            break
    if chosen is None:
        for key in ("submissions", "judging", "normalization", "results"):
            card = by_key[key]
            if key == "submissions" and card["status"] in ("UPCOMING", "ACTIVE"):
                # Automatic deadline; no organizer action. Continue to the
                # earliest actionable judging prerequisite instead.
                continue
            if card["status"] != "COMPLETE":
                chosen = card
                break
    if chosen is None:
        return {"step": None, "status": "COMPLETE", "headline": "No action required",
                "reason": "All required workflow stages are complete.", "action_url": "",
                "blocked_steps": blocked, "stale_steps": stale}
    return {"step": chosen["step"], "status": chosen["status"],
            "headline": chosen["next_action"], "reason": chosen["reason"],
            "action_url": chosen["action_url"],
            "blocked_steps": blocked, "stale_steps": stale}
