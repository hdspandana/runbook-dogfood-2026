#!/usr/bin/env python3
"""In-process smoke test: boots the app against a throwaway database and walks
the whole surface. Useful before running the real acceptance checker.

    python3 scripts/smoke.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.bootstrap import DEMO_TOKENS  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" · {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def cookie(role: str) -> dict[str, str]:
    return {"Cookie": f"runbook_session={DEMO_TOKENS[role]}"}


def as_role(client, role: str):
    """Flask's test client intentionally ignores a raw Cookie header. Use its jar."""
    client.set_cookie("runbook_session", DEMO_TOKENS[role])
    return client


def main() -> int:
    directory = tempfile.mkdtemp(prefix="runbook-smoke-")
    os.environ["RUNBOOK_DATA_DIR"] = directory
    try:
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()

        # --- public surface -------------------------------------------------
        check("GET / is 200 without auth", client.get("/").status_code == 200)
        gallery = client.get("/gallery")
        check("GET /gallery is 200 without auth", gallery.status_code == 200)
        feed = client.get("/api/gallery").get_json()
        check("gallery feed lists submitted projects", feed["count"] > 0, f"{feed['count']} projects")
        names = {p["name"] for p in feed["projects"]}
        check("gallery feed contains a known fixture project", "Glass Signal" in names)
        check("gallery feed leaks no score keys",
              not any(k in p for p in feed["projects"]
                      for k in ("score", "judge", "normalization", "review", "assignment")))
        from app import db as database
        with app.app_context():
            draft_ids = {r["id"] for r in database.query_all(
                "SELECT id FROM projects WHERE status = 'draft' AND event_id = ?", (1,))}
        check("drafts are absent from the gallery", not (draft_ids & {p["id"] for p in feed["projects"]}))

        # --- auth -----------------------------------------------------------
        anon = client.get("/api/whoami").get_json()
        check("visitor role is resolved", anon["role"] == "visitor")
        as_role(client, "organizer")
        who = client.get("/api/whoami").get_json()
        check("organizer session resolves to organizer", who["role"] == "organizer")

        # --- runbook --------------------------------------------------------
        as_role(client, "organizer")
        rb = client.get("/api/events/1/runbook")
        anon_client = app.test_client()
        check("runbook requires organizer (anon is 401)", anon_client.get("/api/events/1/runbook").status_code == 401)
        check("runbook JSON is 200 for organizer", rb.status_code == 200)
        payload = rb.get_json()
        check("runbook has 8 steps", len(payload["steps"]) == 8)
        statuses = {c["step"]: c["status"] for c in payload["steps"]}
        print("      runbook statuses:", statuses)
        check("runbook exposes a next action", bool(payload["next_action"]["headline"]))
        check("stale normalization detected at boot", statuses.get("normalization") == "STALE",
              f"delta={payload['normalization']['delta']}")

        # --- csv ------------------------------------------------------------
        csv_response = as_role(client, "organizer").get("/api/events/1/export.csv")
        check("organizer CSV export is 200", csv_response.status_code == 200)
        check("CSV content type", csv_response.headers["Content-Type"].startswith("text/csv"))
        check("CSV has header row", b"review_status" in csv_response.data)
        check("participant CSV export is 403",
              as_role(client, "participant").get("/api/events/1/export.csv").status_code == 403)
        check("anon CSV export is 401/403",
              anon_client.get("/api/events/1/export.csv").status_code in (401, 403))

        case = app.config["DEMO_SESSIONS"]["case"]
        review_a = case["judge_a_review_id"]
        review_b = case["judge_b_review_id"]

        check("judge A reads own review → 200",
              as_role(client, "judge_a").get(f"/api/judging/reviews/{review_a}").status_code == 200)
        check("judge A reads judge B review → 403",
              as_role(client, "judge_a").get(f"/api/judging/reviews/{review_b}").status_code == 403)
        body = as_role(client, "judge_a").get(f"/api/judging/reviews/{review_b}")
        check("denied body leaks nothing", b"raw_total" not in body.data)
        check("participant reads judge review → 403",
              as_role(client, "participant").get(f"/api/judging/reviews/{review_a}").status_code == 403)

        # deadline
        project_id = case["participant_project_id"]
        rejected = as_role(client, "participant").post(f"/api/projects/{project_id}/submit")
        check("post-deadline submit is 4xx", 400 <= rejected.status_code < 500, str(rejected.status_code))

        print()
        if FAILURES:
            print(f"{len(FAILURES)} failure(s): {FAILURES}")
            return 1
        print("all smoke checks passed")
        return 0
    finally:
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
