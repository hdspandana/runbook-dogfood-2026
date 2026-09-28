"""CSV export.

Product rules implemented here (see README §Export semantics):

* Export is an **operational** capability. It is available to organizers/admins
  at every judging stage — before judging starts, mid-judging, after
  normalization, and after later review changes.
* It is never gated on Runbook status. A BLOCKED or STALE card does not, and
  must not, turn this into a 403.
* It tells the truth about what it contains: pending reviews appear as rows with
  ``review_status=pending`` and empty score fields, never as zeros, and the file
  states whether the attached normalized result is current or stale.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from .. import db
from ..judging import normalization, service as judging
from ..util import now_utc, to_iso

BASE_COLUMNS = [
    "event",
    "event_id",
    "track",
    "project_id",
    "project",
    "team",
    "judge",
    "judge_email",
    "review_status",
]


def _criterion_columns(event_id: int) -> list[tuple[int, str]]:
    return [(int(c["id"]), f"criterion::{c['name']} ({c['weight_pct']:g}%)")
            for c in judging.rubric_for_event(event_id)]


def build_rows(event_id: int) -> tuple[list[str], list[list[Any]], dict[str, Any]]:
    event = db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
    if event is None:
        raise ValueError("unknown event")

    facts = normalization.review_facts(event_id)
    freshness = normalization.freshness(event_id)
    live = normalization.compute(event_id)
    stored = normalization.stored_result(event_id) or {}

    # Score columns represent the *current database*, even if a previously
    # persisted snapshot is STALE. The snapshot timestamp/status are also
    # exported so the organizer can distinguish a live provisional calculation
    # from an intentionally published final ranking.
    live_reviews = {(r["project_id"], r["judge_id"]): r for r in live.get("reviews", [])}
    live_projects = {p["project_id"]: p for p in live.get("projects", [])}

    rubric_cols = _criterion_columns(event_id)
    headers = BASE_COLUMNS + [name for _, name in rubric_cols] + [
        "raw_total",
        "judge_mean",
        "judge_std",
        "z_score",
        "normalized_score",
        "project_final_score",
        "review_count",
        "normalization_status",
        "normalization_generated_at",
        "normalization_timestamp",
        "export_generated_at",
    ]

    exported_at = to_iso(now_utc())
    norm_status = {
        "never_run": "not_run",
        "current": "current",
        "stale": "stale",
    }[freshness["state"]]
    norm_generated = stored.get("generated_at") or ""

    rows: list[list[Any]] = []
    seen_projects: set[int] = set()

    for fact in facts:
        seen_projects.add(fact["project_id"])
        live_review = live_reviews.get((fact["project_id"], fact["judge_id"]), {})
        live_project = live_projects.get(fact["project_id"], {})
        complete = fact["status"] == "complete" and fact["computed_raw"] is not None
        scores = fact["criterion_scores"] or {}
        row = [
            event["name"],
            event["id"],
            fact["track_name"] or "",
            fact["project_id"],
            fact["project_name"],
            fact["team_name"] or "",
            fact["judge_name"],
            fact["judge_email"],
            "complete" if complete else "pending",
        ]
        row += [fmt(scores.get(cid)) if complete else "" for cid, _ in rubric_cols]
        row += [
            fmt(fact["computed_raw"]) if complete else "",
            # Live computed values; `normalization_status` / generated_at
            # indicate whether the last persisted snapshot matches them.
            fmt(live_review.get("judge_mean")) if complete else "",
            fmt(live_review.get("judge_sigma")) if complete else "",
            fmt(live_review.get("z_score")) if complete else "",
            fmt(live_review.get("raw_total") if live["mode"] == "raw-fallback" else live_review.get("z_score")) if complete else "",
            fmt(live_project.get("score")) if complete else "",
            fmt(live_project.get("review_count")) if complete else "", 
            norm_status,
            norm_generated,
            norm_generated,
            exported_at,
        ]
        rows.append(row)

    # Submitted projects with no judge assignment still get a row, so the CSV can
    # never hide a coverage gap.
    for project in db.query_all(
        """SELECT p.*, t.name AS track_name, tm.name AS team_name
             FROM projects p
             LEFT JOIN tracks t ON t.id = p.track_id
             LEFT JOIN teams tm ON tm.id = p.team_id
            WHERE p.event_id = ? AND p.status = 'submitted' AND p.id NOT IN (
                SELECT project_id FROM assignments WHERE event_id = ?
            )
            ORDER BY p.id ASC""",
        (event_id, event_id),
    ):
        row = [
            event["name"], event["id"], project["track_name"] or "", project["id"], project["name"],
            project["team_name"] or "", "", "", "unassigned",
        ]
        row += [""] * len(rubric_cols)
        row += ["", "", "", "", "", "", "0", norm_status, norm_generated, norm_generated, exported_at]
        rows.append(row)

    meta = {
        "event_id": event_id,
        "event": event["name"],
        "rows": len(rows),
        "complete_reviews": sum(1 for r in rows if r[8] == "complete"),
        "pending_reviews": sum(1 for r in rows if r[8] == "pending"),
        "unassigned_projects": sum(1 for r in rows if r[8] == "unassigned"),
        "normalization_status": norm_status,
        "normalization_generated_at": norm_generated,
        "normalization_delta": freshness["delta"],
        "exported_at": exported_at,
        "method": normalization.METHOD_LABEL,
    }
    return headers, rows, meta


def fmt(value: Any) -> Any:
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return value


def render_csv(event_id: int) -> tuple[str, dict[str, Any]]:
    headers, rows, meta = build_rows(event_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue(), meta