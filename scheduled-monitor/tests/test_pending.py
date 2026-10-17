"""Alert durability: a failed Slack post is never lost.

This is the exact failure Michael flagged. The old design saved the reading
(advancing the edge-trigger baseline) and then posted to Slack; if the post
failed, the alert was gone, because the next pass compared against the new
baseline and would not re-fire. The outbox fixes that: the alert persists
until delivery is confirmed, independent of the baseline.
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

import monitor  # noqa: E402
import notify  # noqa: E402
import storage  # noqa: E402

DEFAULTS = {"retries": 1, "retry_backoff_seconds": 0}
PAYLOAD = {
    "target": "widget price",
    "url": "https://example.com/widget",
    "rule": "below",
    "headline": "Value 19 is at or below 20",
    "previous": "25",
    "current": "19",
    "timestamp": "2026-10-17T00:00:00Z",
}


class TestPendingOutbox(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._real_send = notify.send

    def tearDown(self):
        notify.send = self._real_send
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def test_failed_delivery_stays_pending_then_delivers(self):
        # First flush: Slack is down. Second flush: Slack is back.
        state = {"up": False, "calls": 0}

        def fake_send(webhook, payload, **kwargs):
            state["calls"] += 1
            return state["up"]

        notify.send = fake_send

        with storage.connect(self.path) as conn:
            key = monitor.alert_key(PAYLOAD)
            self.assertTrue(storage.enqueue_alert(conn, key, __import__("json").dumps(PAYLOAD)))

            # Slack down: alert must remain pending, not be dropped.
            delivered, pending = monitor.flush_pending(conn, "http://x", DEFAULTS, False)
            self.assertEqual(delivered, 0)
            self.assertEqual(pending, 1)
            self.assertEqual(storage.count_pending(conn), 1)

            # Slack recovers: the same alert now goes out and leaves the outbox.
            state["up"] = True
            delivered, pending = monitor.flush_pending(conn, "http://x", DEFAULTS, False)
            self.assertEqual(delivered, 1)
            self.assertEqual(storage.count_pending(conn), 0)

    def test_enqueue_is_idempotent(self):
        with storage.connect(self.path) as conn:
            key = monitor.alert_key(PAYLOAD)
            body = __import__("json").dumps(PAYLOAD)
            self.assertTrue(storage.enqueue_alert(conn, key, body))
            # Same event again (overlapping pass) must not create a duplicate.
            self.assertFalse(storage.enqueue_alert(conn, key, body))
            self.assertEqual(storage.count_pending(conn), 1)

    def test_alert_key_is_stable_and_distinct(self):
        other = dict(PAYLOAD, timestamp="2026-10-17T01:00:00Z")
        self.assertEqual(monitor.alert_key(PAYLOAD), monitor.alert_key(dict(PAYLOAD)))
        self.assertNotEqual(monitor.alert_key(PAYLOAD), monitor.alert_key(other))


if __name__ == "__main__":
    unittest.main()
