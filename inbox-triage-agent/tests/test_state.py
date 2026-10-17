"""Durable, atomic processing state.

Proves that a message is claimed exactly once, that a finished message never
re-claims, that a failed claim is released for retry, and that a claim left
behind by a crashed run becomes reclaimable after it goes stale.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state  # noqa: E402


class TestState(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def test_claim_is_won_once(self):
        with state.connect(self.path) as a, state.connect(self.path) as b:
            self.assertTrue(state.claim(a, "m1"))
            # A second, independent connection must lose the race for m1.
            self.assertFalse(state.claim(b, "m1"))

    def test_done_message_never_reclaims(self):
        with state.connect(self.path) as conn:
            self.assertTrue(state.claim(conn, "m2"))
            state.mark_done(conn, "m2", "draft_created", draft_id="d1")
            self.assertTrue(state.is_done(conn, "m2"))
            self.assertFalse(state.claim(conn, "m2"))

    def test_released_claim_can_be_retaken(self):
        with state.connect(self.path) as conn:
            self.assertTrue(state.claim(conn, "m3"))
            state.release(conn, "m3")  # work failed; hand it back
            self.assertTrue(state.claim(conn, "m3"))  # next run retries it

    def test_stale_claim_is_reclaimable(self):
        with state.connect(self.path) as conn:
            old = datetime.now(timezone.utc) - timedelta(
                seconds=state.STALE_CLAIM_SECONDS + 60
            )
            # Simulate a crashed run that claimed long ago and never finished.
            self.assertTrue(state.claim(conn, "m4", now=old))
            # A fresh run takes it over because the old claim is stale.
            self.assertTrue(state.claim(conn, "m4"))

    def test_fresh_claim_is_not_reclaimable(self):
        with state.connect(self.path) as conn:
            self.assertTrue(state.claim(conn, "m5"))
            # Still fresh: a concurrent run must not steal it.
            self.assertFalse(state.claim(conn, "m5"))


if __name__ == "__main__":
    unittest.main()
