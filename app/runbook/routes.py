"""Runbook dashboard and its JSON projection.

The dashboard is the organizer's primary screen. The JSON endpoint exists so
that the same computed facts can be tested and diffed without scraping HTML.
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from ..events import service as events_service
from ..security import current_user, deny, is_organizer
from ..util import json_ok
from . import service as runbook

bp = Blueprint("runbook", __name__)


def _authorised():
    user = current_user()
    if user is None:
        return None, deny("Sign in as an organizer to view the Runbook.", 401)
    if not is_organizer(user):
        return None, deny("Organizer access required.", 403)
    return user, None


@bp.route("/organizer/runbook")
def dashboard():
    user, refusal = _authorised()
    if refusal is not None:
        return refusal
    event_id = request.args.get("event", type=int)
    snapshot = runbook.build(event_id)
    return render_template(
        "runbook.html",
        snapshot=snapshot,
        events=events_service.all_events(),
        selected_event_id=(snapshot["event"] or {}).get("id"),
    )


@bp.route("/organizer/events/<int:event_id>/runbook")
def dashboard_for_event(event_id: int):
    return redirect(url_for("runbook.dashboard", event=event_id))


@bp.route("/api/events/<int:event_id>/runbook")
def api_runbook(event_id: int):
    user, refusal = _authorised()
    if refusal is not None:
        return refusal
    event = events_service.get_event(event_id)
    if event is None:
        from ..util import json_error

        return json_error("not found", 404)
    return json_ok(runbook.build(event_id))


@bp.route("/api/runbook")
def api_runbook_default():
    user, refusal = _authorised()
    if refusal is not None:
        return refusal
    return json_ok(runbook.build(request.args.get("event", type=int)))