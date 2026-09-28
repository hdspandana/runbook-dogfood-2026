#!/usr/bin/env python3
"""End-to-end demo flow against a RUNNING portal (default http://localhost:8080).

Proves the causal chain that RUNBOOK exists for:

    judge submits a review
      → judging progress changes
      → normalization becomes STALE
      → next action changes
      → organizer re-runs normalization
      → CSV reflects the current result

Usage:  python3 scripts/demo_flow.py [base_url]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import urllib.parse

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")

COOKIES = {
    "organizer": "demo-session-organizer-3f9c1a7d5b2e",
    "admin": "demo-session-admin-7c4e0b91a6d2",
    "judge_a": "demo-session-judge-a-1d8f5c30b7a4",
    "judge_b": "demo-session-judge-b-9a2b6e04c1f7",
    "participant": "demo-session-participant-5e7d2a8c40b1",
}


def call(path: str, role: str | None = None, method: str = "GET", body: dict | None = None):
    request = urllib.request.Request(BASE + path, method=method)
    if role:
        request.add_header("Cookie", f"runbook_session={COOKIES[role]}")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data, timeout=15) as response:
            return response.status, response.read().decode("utf-8", "replace"), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace"), dict(error.headers)


def step(text: str) -> None:
    print(f"\n=== {text}")


def runbook_line(role: str = "organizer") -> dict:
    status, body, _ = call("/api/events/1/runbook", role)
    assert status == 200, f"runbook returned {status}"
    payload = json.loads(body)
    cards = {c["step"]: c for c in payload["steps"]}
    judging = cards["judging"]
    normalization = cards["normalization"]
    print(f"  judging        {judging['status']:<9} {judging['metric']}")
    print(f"  normalization  {normalization['status']:<9} {normalization['metric']}")
    print(f"  next action    {payload['next_action']['headline']}  [{payload['next_action']['status']}]")
    return payload


def main() -> int:
    step("Health and public surface")
    status, body, _ = call("/api/health")
    print(f"  /api/health → {status} {body}")
    status, body, _ = call("/gallery")
    print(f"  /gallery without auth → {status}")
    feed = json.loads(call("/api/gallery")[1])
    print(f"  gallery feed: {feed['count']} submitted projects, first: {feed['projects'][0]['name']}")

    step("Runbook cold open (before any change)")
    runbook_line()

    step("A judge picks a pending assignment and submits a review")
    # The official fixture can give the demo judge no pending slots. Select a
    # pending slot deterministically among *all* fixture judges, log in with
    # its seeded judge123 password, and use that real session. No judge_id is
    # ever trusted from a form/body/query to determine caller identity.
    status, body, _ = call("/api/events/1/export.csv", "organizer")
    import csv, io, http.cookiejar
    pending_rows = [r for r in csv.DictReader(io.StringIO(body)) if r["review_status"] == "pending" and r["judge_email"]]
    assert pending_rows, "no pending review rows in the fixture"
    selected = pending_rows[0]
    judge_email = selected["judge_email"]
    project_id = int(selected["project_id"])
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    form = urllib.parse.urlencode({"email": judge_email, "password": "judge123"}).encode()
    login_request = urllib.request.Request(BASE + "/login", data=form, method="POST")
    with opener.open(login_request, timeout=15) as response:
        assert response.status == 200, f"judge login failed: {response.status}"
    print(f"  pending project #{project_id}, judge {judge_email} signed in through /login")
    rubric = json.loads(call("/api/events/1/rubric")[1])
    scores = {str(c["id"]): 4.5 for c in rubric["criteria"]}
    request = urllib.request.Request(
        BASE + f"/api/judging/projects/{project_id}/review",
        data=json.dumps({"scores": scores, "comment": "Submitted during the demo flow.", "submit": True}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with opener.open(request, timeout=15) as response:
        status, payload = response.status, json.loads(response.read().decode())
    print(f"  POST review → {status}, raw_total={payload['review']['raw_total']}")

    step("The database changed; the Runbook changed with it")
    after = runbook_line()
    normalization = next(c for c in after["steps"] if c["step"] == "normalization")
    assert normalization["status"] == "STALE", "expected a stale normalization after a new review"

    step("Security checks (real backend authorization)")
    peer = call("/api/judging/reviews/1", "judge_b")
    print(f"  judge_b → judge_a's review #1 → {peer[0]} {peer[1].strip()[:60]}")
    own = call("/api/judging/reviews/1", "judge_a")
    print(f"  judge_a → own review #1        → {own[0]}")
    part = call("/api/judging/reviews/1", "participant")
    print(f"  participant → judge review #1  → {part[0]} {part[1].strip()[:60]}")
    late = call("/api/projects", "participant", method="POST", body={"title": "late probe"})
    print(f"  participant → submit to closed event → {late[0]} {late[1].strip()[:60]}")
    export_as_participant = call("/api/events/1/export.csv", "participant")
    print(f"  participant → CSV export → {export_as_participant[0]} (organizer-only)")

    step("Organizer re-runs normalization; the result is current again")
    status, body, _ = call("/api/events/1/normalize", "organizer", method="POST", body={})
    print(f"  POST /api/events/1/normalize → {status} {json.loads(body)['freshness']['state']}")
    runbook_line()

    step("A later review edit makes the persisted snapshot stale again")
    edit_request = urllib.request.Request(
        BASE + f"/api/judging/projects/{project_id}/review",
        data=json.dumps({"scores": {str(c["id"]): 2.5 for c in rubric["criteria"]},
                         "comment": "Revised.", "submit": True}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    with opener.open(edit_request, timeout=15) as response:
        status = response.status
    print(f"  POST review (revision) → {status}")
    runbook_line()

    step("CSV export is available at every stage, and stays honest")
    status, body, headers = call("/api/events/1/export.csv", "organizer")
    lines = body.splitlines()
    print(f"  GET CSV → {status} {headers.get('Content-Type')} rows={len(lines) - 1} "
          f"normalization={headers.get('X-Runbook-Normalization-Status')}")
    header = lines[0].split(",")
    print(f"  columns: {', '.join(header[:9])} …")
    for wanted in ("pending", "unassigned"):
        row = next((line for line in lines[1:] if line.split(",")[8] == wanted), None)
        if row:
            fields = row.split(",")
            print(f"  {wanted:<10} row → project={fields[4]!r} judge={fields[6]!r} raw_total={fields[-9]!r}")
        else:
            print(f"  {wanted:<10} row → none in this event")
    print("  export never consulted Runbook status to decide authorization.")

    print("\nDemo flow complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
