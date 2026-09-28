#!/usr/bin/env python3
"""Production-ish single-process entrypoint for self-hosted local use.

For DOGFOOD's one-container offline deployment, Flask's built-in server is
sufficient: it is bound to 0.0.0.0:8080 and the database is in a persistent
volume. In a public Internet deployment use a WSGI server and TLS reverse proxy
instead; see README.md §Production notes.
"""

from __future__ import annotations

import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"[runbook] listening on http://{host}:{port}", flush=True)
    app.run(host=host, port=port, debug=False, use_reloader=False)
