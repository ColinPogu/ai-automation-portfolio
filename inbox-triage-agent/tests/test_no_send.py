"""The draft-only boundary, enforced statically.

Michael's question: gmail.compose also permits sending, so how is draft-only
actually guaranteed? Answer: the code has no send path and refuses to run
with a send-capable scope. These tests fail CI if a future edit breaks
either property.
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

import gmail_client  # noqa: E402


class TestNoSend(unittest.TestCase):
    def test_scopes_are_draft_only(self):
        self.assertIn(
            "https://www.googleapis.com/auth/gmail.readonly", gmail_client.SCOPES
        )
        self.assertIn(
            "https://www.googleapis.com/auth/gmail.compose", gmail_client.SCOPES
        )
        # No send/modify/full scope may ever be present.
        self.assertFalse(
            gmail_client.FORBIDDEN_SCOPES.intersection(gmail_client.SCOPES)
        )

    def test_no_send_call_in_source(self):
        # Scan every .py in the package for a Gmail .send( call. drafts().send
        # and messages().send are the only ways this code could send mail, and
        # neither may appear.
        offenders = []
        for name in os.listdir(PKG):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(PKG, name), encoding="utf-8") as handle:
                src = handle.read()
            for pattern in (r"\.send\s*\(", r"messages\(\)\.send", r"drafts\(\)\.send"):
                if re.search(pattern, src):
                    offenders.append((name, pattern))
        self.assertEqual(offenders, [], "unexpected send path: %s" % offenders)

    def test_only_write_is_draft_create(self):
        with open(os.path.join(PKG, "gmail_client.py"), encoding="utf-8") as handle:
            src = handle.read()
        self.assertIn("drafts()", src)
        self.assertIn(".create(", src)


if __name__ == "__main__":
    unittest.main()
