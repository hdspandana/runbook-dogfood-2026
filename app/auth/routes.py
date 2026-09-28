"""Login / logout / registration.

Registration is intentionally limited to the participant role: judges arrive via
an organizer-issued invite token, organizers and admins are created by seeding or
by an existing organizer. Nobody can self-assign a privileged role.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .. import db
from ..util import json_ok, now_utc, to_iso
from . import service as auth_service
from ..security import current_user, deny

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", next_url=request.args.get("next", ""))

    email = request.form.get("email", "")
    password = request.form.get("password", "")
    user = auth_service.authenticate(email, password)
    if user is None:
        # One generic message: no user enumeration.
        return render_template("login.html", error="Email or password is incorrect.",
                               next_url=request.form.get("next", "")), 401

    token = auth_service.create_session(user["id"], request.headers.get("User-Agent"))
    target = request.form.get("next") or url_for("auth.dashboard")
    # Only local absolute paths are safe redirect targets. A URL like
    # //attacker.example would leak the session through an open redirect.
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        target = url_for("auth.dashboard")
    response = redirect(target)
    return auth_service.set_session_cookie(response, token)


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    auth_service.destroy_session(request.cookies.get("runbook_session"))
    response = redirect(url_for("gallery.home"))
    return auth_service.clear_session_cookie(response)


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    display_name = (request.form.get("display_name") or "").strip()

    error = None
    if "@" not in email or len(email) < 5:
        error = "Enter a valid email address."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif db.query_one("SELECT id FROM users WHERE email = ?", (email,)):
        error = "That email is already registered."
    if error:
        return render_template("register.html", error=error, email=email, display_name=display_name), 400

    user_id = auth_service.create_user(email, password, display_name or email, "participant")
    token = auth_service.create_session(user_id, request.headers.get("User-Agent"))
    response = redirect(url_for("auth.dashboard"))
    flash("Account created. You are signed in as a participant.", "ok")
    return auth_service.set_session_cookie(response, token)


@bp.route("/dashboard")
def dashboard():
    user = current_user()
    if user is None:
        return redirect(url_for("auth.login", next=request.path))
    if user["role"] in ("organizer", "admin"):
        return redirect(url_for("runbook.dashboard"))
    if user["role"] == "judge":
        return redirect(url_for("judge.index"))
    return redirect(url_for("projects.index"))


@bp.route("/api/whoami")
def whoami():
    """Tiny identity endpoint. Handy for demos and for the security test matrix."""
    user = current_user()
    if user is None:
        return json_ok(
            {
                "authenticated": False,
                "role": "visitor",
                "permissions": ["gallery:read", "event:read"],
            }
        )
    return json_ok(
        {
            "authenticated": True,
            "role": user["role"],
            "email": user["email"],
            "display_name": user["display_name"],
            "permissions": _permissions(user),
        }
    )


def _permissions(user) -> list[str]:
    from ..security import roles_of

    capabilities = set(roles_of(user))
    granted: list[str] = ["gallery:read", "event:read"]
    if "participant" in capabilities:
        granted += ["team:write", "project:write", "project:submit"]
    if "judge" in capabilities and user["role"] == "judge":
        granted += ["review:read:own", "review:write:own", "assignment:read:own"]
    if "organizer" in capabilities:
        granted += [
            "event:write", "track:write", "prize:write", "rubric:write",
            "assignment:write", "review:read:all", "progress:read",
            "normalization:run", "export:csv", "results:publish", "judge:invite",
        ]
    if user["role"] == "admin":
        granted += ["system:manage"]
    return sorted(set(granted))
