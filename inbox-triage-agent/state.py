"""Durable, concurrency-safe processing state for the triage agent.

Backed by a small SQLite file. It answers two reliability questions that a
plain "loop over unread mail" design gets wrong:

1. Durable processed state. Every message the agent acts on is recorded, so
   running the agent twice over the same unread inbox never creates a second
   draft for a message already handled. The record survives restarts.

2. Atomic duplicate prevention under concurrency. Claiming a message is a
   single conditional write. SQLite serializes writers, so for a given
   message id exactly one process wins the claim; everyone else sees zero
   rows changed and skips it. There is no check-then-act gap for a race to
   slip through, and no lock file to leak if a process dies.

A claim that is left dangling by a crashed run becomes reclaimable after
STALE_CLAIM_SECONDS, so one bad run cannot wedge a message forever. A
message is only marked done after its draft or escalation actually
succeeded; if the work fails, the claim is released and the next run retries
it.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

STALE_CLAIM_SECONDS = 15 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    message_id  TEXT PRIMARY KEY,
    status      TEXT NOT NULL,          -- claimed | done
    action      TEXT,                   -- draft_created | escalated
    draft_id    TEXT,
    claimed_ts  TEXT NOT NULL,
    done_ts     TEXT
);
"""


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def connect(db_path):
    """Yield a SQLite connection with WAL mode and the schema in place.

    WAL plus a busy timeout lets several agent processes hit the same state
    file at once without "database is locked" errors; the claim write is
    still serialized, which is exactly the guarantee we want.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def claim(conn, message_id, now=None):
    """Try to claim a message for processing. Return True if we own it.

    The common path is a single INSERT OR IGNORE: the first caller inserts a
    'claimed' row and wins; a later caller inserts nothing and loses. If a
    previous 'claimed' row is older than STALE_CLAIM_SECONDS we atomically
    take it over with a conditional UPDATE, so a crash cannot strand a
    message. An already 'done' message never claims again.
    """
    now = now or _now()
    now_iso = _iso(now)

    cur = conn.execute(
        "INSERT OR IGNORE INTO processed (message_id, status, claimed_ts) "
        "VALUES (?, 'claimed', ?)",
        (message_id, now_iso),
    )
    conn.commit()
    if cur.rowcount == 1:
        return True

    row = conn.execute(
        "SELECT status, claimed_ts FROM processed WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if row is None or row["status"] == "done":
        return False

    cutoff = _iso(
        datetime.fromtimestamp(now.timestamp() - STALE_CLAIM_SECONDS, timezone.utc)
    )
    cur = conn.execute(
        "UPDATE processed SET claimed_ts = ? "
        "WHERE message_id = ? AND status = 'claimed' AND claimed_ts < ?",
        (now_iso, message_id, cutoff),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_done(conn, message_id, action, draft_id=None, now=None):
    """Record that a message was fully handled. Idempotent."""
    conn.execute(
        "UPDATE processed SET status = 'done', action = ?, draft_id = ?, "
        "done_ts = ? WHERE message_id = ?",
        (action, draft_id, _iso(now or _now()), message_id),
    )
    conn.commit()


def release(conn, message_id):
    """Give up a claim we could not finish, so a later run retries it."""
    conn.execute(
        "DELETE FROM processed WHERE message_id = ? AND status = 'claimed'",
        (message_id,),
    )
    conn.commit()


def is_done(conn, message_id):
    row = conn.execute(
        "SELECT status FROM processed WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    return bool(row) and row["status"] == "done"
