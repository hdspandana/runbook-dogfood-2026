"""RUNBOOK — self-hosted hackathon submission and judging portal with an
operational Runbook computed from live event data.

Application factory. Boot order is deliberately boring and observable:

    1. config (env-driven, offline defaults)
    2. SQLite schema (idempotent CREATE TABLE IF NOT EXISTS)
    3. fixture seed (only when the database has no events)
    4. fixed demo sessions for the acceptance harness
    5. blueprints
"""

from __future__ import annotations

import os
from typing import Any, Optional

from flask import Flask, g, render_template, request, session

from . import db
from .config import Config
from .security import current_user
from .util import json_error, now_utc, pretty_utc

__version__ = "1.0.0"


def build_config(overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    cfg = Config()
    values: dict[str, Any] = {
        "DATA_DIR": str(cfg.DATA_DIR),
        "DATABASE": cfg.DATABASE,
        "SECRET_KEY": cfg.SECRET_KEY,
        "SEED_ON_BOOT": cfg.SEED_ON_BOOT,
        "SESSION_COOKIE_NAME": cfg.SESSION_COOKIE_NAME,
        "SESSION_TTL_HOURS": cfg.SESSION_TTL_HOURS,
        # Flask writes the flash cookie itself, so it cannot consume "auto";
        # the after_request hook in _register_context rewrites that header to
        # match whatever the auth cookie resolved to.
        "SESSION_COOKIE_SECURE": False,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "AUTH_COOKIE_SECURE": cfg.SESSION_COOKIE_SECURE,
        "AUTH_COOKIE_SAMESITE": cfg.SESSION_COOKIE_SAMESITE,
        "SESSION_COOKIE_HTTPONLY": cfg.SESSION_COOKIE_HTTPONLY,
        "MIN_REVIEWS_FOR_NORMALIZATION": cfg.MIN_REVIEWS_FOR_NORMALIZATION,
        "MAX_CONTENT_LENGTH": cfg.MAX_CONTENT_LENGTH,
        "SEND_FILE_MAX_AGE_DEFAULT": 0,
        "TEMPLATES_AUTO_RELOAD": True,
    }
    values.update(overrides or {})
    return values


def create_app(overrides: Optional[dict[str, Any]] = None, boot: bool = True) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(build_config(overrides))
    app.secret_key = app.config["SECRET_KEY"]

    from pathlib import Path

    Path(app.config["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    app.teardown_appcontext(db.close_db)

    with app.app_context():
        _initialise_database(app, boot=boot)

    _register_blueprints(app)
    _register_errors(app)
    _register_context(app)
    return app


def _initialise_database(app: Flask, boot: bool = True) -> None:
    """Create the schema, then seed fixtures and demo sessions exactly once."""
    from . import db as db_module

    conn = db_module.connect(app.config["DATABASE"])
    try:
        db_module.init_db(conn)
    finally:
        conn.close()

    if not boot:
        return

    if app.config.get("SEED_ON_BOOT"):
        from .fixtures import loader

        summary = loader.seed_if_empty()
        if summary.get("seeded"):
            print(f"[runbook] fixtures seeded: {summary}")
        else:
            print(f"[runbook] fixtures not reseeded ({summary.get('reason')})")

        if os.environ.get("RUNBOOK_SEED_LIVE_PILOT", "on").strip().lower() not in {"0", "false", "off", "no"}:
            from .fixtures.demo import seed_live_pilot

            pilot_id = seed_live_pilot()
            print(f"[runbook] live pilot event id: {pilot_id}")

    from . import bootstrap

    app.config["DEMO_SESSIONS"] = bootstrap.ensure_demo_sessions(verbose=True)


def _register_blueprints(app: Flask) -> None:
    from .api.routes import bp as api_bp
    from .auth.routes import bp as auth_bp
    from .exports.routes import bp as exports_bp
    from .gallery.routes import bp as gallery_bp
    from .judging.routes import bp as judge_bp
    from .organizer.routes import bp as organizer_bp
    from .projects.routes import bp as projects_bp
    from .runbook.routes import bp as runbook_bp
    from .teams.routes import bp as teams_bp

    for blueprint in (auth_bp, gallery_bp, teams_bp, projects_bp,
                      judge_bp, organizer_bp, runbook_bp, exports_bp, api_bp):
        app.register_blueprint(blueprint)


def _register_errors(app: Flask) -> None:
    @app.errorhandler(404)
    def not_found(error):  # pragma: no cover - trivial
        if request.path.startswith("/api/"):
            return json_error("not found", 404)
        return render_template("error.html", code=404, message="That page does not exist."), 404

    @app.errorhandler(413)
    def too_large(error):  # pragma: no cover
        return json_error("payload too large", 413)

    @app.errorhandler(500)
    def server_error(error):  # pragma: no cover
        if request.path.startswith("/api/"):
            return json_error("internal error", 500)
        return render_template("error.html", code=500, message="Something went wrong on the server."), 500


def _register_context(app: Flask) -> None:
    @app.before_request
    def _touch():  # pragma: no cover - trivial
        g.request_started = now_utc()

    @app.after_request
    def _cookie_policy(response):
        """Apply the same Secure/SameSite policy to Flask's own flash cookie.

        Flask writes the flash cookie itself, so its flags cannot be set from
        our helper; rewrite the header to match the auth cookie's resolved
        policy. Needed because the demo runs inside an HTTPS cross-site frame.
        """
        from .auth.service import _cookie_flags

        secure, samesite = _cookie_flags()
        name = app.config["SESSION_COOKIE_NAME"]
        cookies = response.headers.getlist("Set-Cookie")
        if not cookies:
            return response
        rewritten = []
        for header in cookies:
            if header.startswith(f"{name}="):
                for existing in ("Lax", "Strict", "None"):
                    header = header.replace(f"SameSite={existing}", f"SameSite={samesite}")
                if "SameSite=" not in header:
                    header += f"; SameSite={samesite}"
                if secure and "Secure" not in header:
                    header += "; Secure"
            rewritten.append(header)
        response.headers.pop("Set-Cookie", None)
        for header in rewritten:
            response.headers.add("Set-Cookie", header)
        return response

    @app.context_processor
    def inject() -> dict[str, Any]:
        from .events import service as events_service

        user = current_user()
        return {
            "current_user": user,
            "viewer_role": user["role"] if user else "visitor",
            "nav_event": events_service.primary_event(),
            "app_version": __version__,
            "now_utc": now_utc(),
        }

    app.jinja_env.filters["utc"] = pretty_utc
    app.jinja_env.globals["pretty_utc"] = pretty_utc


def create_offline_app(**kwargs) -> Flask:  # pragma: no cover - convenience alias
    return create_app(kwargs)


__all__ = ["create_app", "build_config", "__version__"]
