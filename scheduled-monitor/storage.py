"""SQLite storage for the scheduled monitor.

One table, "readings", keeps the full history (one row per fetch) so the
monitor can compare the newest reading against the previous one and so you
can inspect trends later with plain SQL. The schema is created on demand,
so the database file appears the first time the monitor runs. All
timestamps are stored in UTC, ISO 8601, with a trailing Z.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    target    TEXT    NOT NULL,
    url       TEXT    NOT NULL,
    status    TEXT    NOT NULL,   -- ok or error
    raw_text  TEXT,
    value     REAL,
    in_stock  INTEGER,            -- 0, 1, or NULL
    error     TEXT,
    ts        TEXT    NOT NULL    -- UTC ISO 8601
);
CREATE INDEX IF NOT EXISTS idx_readings_target_id ON readings (target, id);
"""


def utc_now_iso():
    """Current time as a sortable UTC ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def connect(db_path):
    """Yield a SQLite connection with the schema in place, then commit.

    Usage:
        with storage.connect("monitor.db") as conn:
            ...
    """
    parent = Path(db_path).resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_reading(conn, reading):
    """Insert one reading. "reading" is a dict with the column names."""
    conn.execute(
        """
        INSERT INTO readings
            (target, url, status, raw_text, value, in_stock, error, ts)
        VALUES
            (:target, :url, :status, :raw_text, :value, :in_stock, :error, :ts)
        """,
        reading,
    )
    conn.commit()


def last_reading(conn, target):
    """Return the most recent successful reading for a target, or None."""
    return conn.execute(
        """
        SELECT * FROM readings
        WHERE target = ? AND status = 'ok'
        ORDER BY id DESC
        LIMIT 1
        """,
        (target,),
    ).fetchone()


def history(conn, target, limit=50):
    """Return the most recent readings for a target, newest first."""
    return conn.execute(
        """
        SELECT * FROM readings
        WHERE target = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (target, limit),
    ).fetchall()
