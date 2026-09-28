"""Single publication policy shared by browser and JSON routes.

CSV is intentionally independent of this policy. A published timestamp is history, not evidence of current publication. The
linked normalization run and current inputs must both agree.
"""
from __future__ import annotations

from .. import db
from ..util import now_utc, to_iso
from . import normalization, service as judging


def readiness(event_id):
    progress = judging.judging_progress(event_id)
    freshness = normalization.freshness(event_id)
    details = {"completed_reviews": progress["complete_reviews"],
               "required_reviews": progress["required_reviews"],
               "normalization_state": freshness["state"]}
    if not progress["coverage_ok"] or not progress["all_reviews_complete"]:
        return {"error": "results_not_ready", "reason": "required judging reviews are incomplete", **details}
    if freshness["state"] == "never_run":
        return {"error": "results_not_ready", "reason": "normalization has not been run", **details}
    if freshness["state"] != "current":
        return {"error": "results_not_ready", "reason": "normalized result is stale", **details}
    return None


def state(event, freshness=None, progress=None):
    """Derived publication state; legacy timestamps without a run link are STALE.

    A later normalization (even of identical inputs) is a new snapshot and
    requires deliberate republication. This prevents an old timestamp from
    appearing to certify a ranking that was produced afterwards.
    """
    if not event["results_published_at"]:
        return "not_published"
    run = normalization.latest_run(event["id"])
    if not run or not event["results_published_run_id"] or run["id"] != event["results_published_run_id"]:
        return "stale"
    if freshness is None:
        freshness = normalization.freshness(event["id"])
    if progress is None:
        progress = judging.judging_progress(event["id"])
    if not progress["all_reviews_complete"] or freshness["state"] != "current":
        return "stale"
    return "current"


def publish(event_id):
    """Check and publish in a single SQLite write transaction (no TOCTOU)."""
    with db.transaction(db.get_db()):
        refusal = readiness(event_id)
        if refusal:
            return None, refusal
        timestamp = to_iso(now_utc())
        run = normalization.latest_run(event_id)
        db.execute("UPDATE events SET results_published_at = ?, results_published_run_id = ? WHERE id = ?",
                   (timestamp, run["id"], event_id))
    return timestamp, None
