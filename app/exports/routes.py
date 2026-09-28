"""CSV export routes.

Authorization is `organizer_required` and nothing else. In particular:

* There is NO check against Runbook status here.
* There is NO check that normalization has been run.
* There is NO check that judging is complete.

The export works while judging is mid-flight, after normalization, and after
later review edits (in which case the file says the normalized result is stale).
Only role and event scope decide access.
"""

from __future__ import annotations

from flask import Blueprint, Response, abort, request

from ..events import service as events_service
from ..security import organizer_required
from ..util import json_ok
from . import service as export_service

bp = Blueprint("exports", __name__)


def _csv_response(event_id: int) -> Response:
    event = events_service.get_event(event_id)
    if event is None:
        abort(404)
    body, meta = export_service.render_csv(event_id)
    filename = f"runbook-{event['slug']}-judging-export.csv"
    response = Response(body, mimetype="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    # The CSV's meaning depends on the current review set; do not let a proxy
    # or browser serve a stale copy.
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Runbook-Export-Rows"] = str(meta["rows"])
    response.headers["X-Runbook-Normalization-Status"] = meta["normalization_status"]
    return response


@bp.route("/organizer/events/<int:event_id>/export.csv")
@organizer_required
def export_csv(event_id: int):
    return _csv_response(event_id)


@bp.route("/api/events/<int:event_id>/export.csv")
@organizer_required
def api_export_csv(event_id: int):
    return _csv_response(event_id)


@bp.route("/api/events/<int:event_id>/export/summary")
@organizer_required
def export_summary(event_id: int):
    """What the CSV would contain, without downloading it."""
    if events_service.get_event(event_id) is None:
        abort(404)
    _, _, meta = export_service.build_rows(event_id)
    return json_ok(meta)