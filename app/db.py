"""SQLite access layer for RUNBOOK.

The whole persistence story of this project is:

* one SQLite file,
* one schema (``schema.sql``),
* small helper functions on top of the stdlib ``sqlite3`` module.

There is no ORM on purpose: the acceptance criteria of DOGFOOD 2026 across
every public submission are about *behaviour* (deadline enforcement, role
isolation, honest CSV), and a thin SQL layer keeps that behaviour obvious and
greppable.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


# --------------------------------------------------------------------------
# connection handling
# --------------------------------------------------------------------------
def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with sane defaults for a single-container deployment."""
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def get_db() -> sqlite3.Connection:
    """Return the connection bound to the current request/application context."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(exc: Optional[BaseException] = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit transaction. ``isolation_level=None`` means we drive BEGIN/COMMIT."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------
# query helpers
# --------------------------------------------------------------------------
def query_all(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return list(get_db().execute(sql, params).fetchall())


def query_one(sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
    return get_db().execute(sql, params).fetchone()


def query_scalar(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    row = query_one(sql, params)
    if row is None:  # pragma: no cover - callers always ask for one column
        return default
    value = row[0]
    return default if value is None else value


def execute(sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    return get_db().execute(sql, params)


def insert(sql: str, params: Sequence[Any] = ()) -> int:
    cur = get_db().execute(sql, params)
    return int(cur.lastrowid)


def insert_many(sql: str, rows: Iterable[Sequence[Any]]) -> None:
    get_db().executemany(sql, list(rows))


# --------------------------------------------------------------------------
# schema / bootstrap
# --------------------------------------------------------------------------
def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # Existing volumes predate snapshot-linked publication. Do not infer which
    # historical run a timestamp referred to: leave it unverified until republished.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    if "results_published_run_id" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN results_published_run_id INTEGER")


def utcnow() -> str:
    """Canonical timestamp format used everywhere in the schema (ISO-8601 UTC)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")