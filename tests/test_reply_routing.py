import json
from pathlib import Path
import tempfile
import unittest

from reply_routing import ReplyRouteStore


class ReplyRouteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "forwards.json"
        self.store = ReplyRouteStore(self.path, max_age_seconds=100)

    def tearDown(self):
        self.temp.cleanup()

    def register(self, **overrides):
        values = {
            "from_channel": "DPRIVATE",
            "from_thread": "100.1",
            "to_channel": "CSOURCE",
            "to_thread": "90.1",
            "user_id": "UOWNER",
            "session_id": "session-1",
            "approver_only": True,
            "now": 10,
        }
        values.update(overrides)
        self.store.register(**values)

    def test_named_approver_claims_once_and_duplicate_is_terminal(self):
        self.register()
        first = self.store.claim(
            from_channel="DPRIVATE", from_thread="100.1", user_id="UAPPROVER",
            is_approver=True, now=20,
        )
        self.assertEqual(first.status, "routed")
        self.assertEqual(first.route["channel"], "CSOURCE")
        self.assertEqual(first.route["thread"], "90.1")

        duplicate = self.store.claim(
            from_channel="DPRIVATE", from_thread="100.1", user_id="UAPPROVER",
            is_approver=True, now=21,
        )
        self.assertEqual(duplicate.status, "consumed")

    def test_non_approver_does_not_consume_escalation(self):
        self.register()
        refused = self.store.claim(
            from_channel="DPRIVATE", from_thread="100.1", user_id="UOPERATOR",
            is_approver=False, now=20,
        )
        self.assertEqual(refused.status, "approver_required")
        accepted = self.store.claim(
            from_channel="DPRIVATE", from_thread="100.1", user_id="UAPPROVER",
            is_approver=True, now=21,
        )
        self.assertEqual(accepted.status, "routed")

    def test_expired_reply_is_terminal_and_persists_across_store_instances(self):
        self.register()
        restarted = ReplyRouteStore(self.path, max_age_seconds=100)
        expired = restarted.claim(
            from_channel="DPRIVATE", from_thread="100.1", user_id="UAPPROVER",
            is_approver=True, now=110,
        )
        self.assertEqual(expired.status, "expired")
        self.assertEqual(restarted.claim(
            from_channel="DPRIVATE", from_thread="100.1", user_id="UAPPROVER",
            is_approver=True, now=111,
        ).status, "expired")

    def test_ordinary_forward_accepts_authorized_non_approver(self):
        self.register(approver_only=False)
        result = self.store.claim(
            from_channel="DPRIVATE", from_thread="100.1", user_id="UOPERATOR",
            is_approver=False, now=20,
        )
        self.assertEqual(result.status, "routed")

    def test_pending_legacy_forward_is_adopted_without_approver_gate(self):
        self.path.write_text(json.dumps({
            "100.1": {
                "thread": "90.1",
                "channel": "CSOURCE",
                "session_id": "session-1",
                "user_id": "UOWNER",
                "registered_at": 10,
            }
        }))
        result = self.store.claim(
            from_channel="DPRIVATE", from_thread="100.1", user_id="UOPERATOR",
            is_approver=False, now=20,
        )
        self.assertEqual(result.status, "routed")
        self.assertEqual(result.route["channel"], "CSOURCE")

    def test_malformed_store_fails_closed(self):
        self.path.write_text("not json")
        result = self.store.claim(
            from_channel="DPRIVATE", from_thread="100.1", user_id="UAPPROVER",
            is_approver=True, now=20,
        )
        self.assertEqual(result.status, "invalid")

    def test_gc_keeps_recent_terminal_tombstone(self):
        self.register()
        self.store.claim(
            from_channel="DPRIVATE", from_thread="100.1", user_id="UAPPROVER",
            is_approver=True, now=20,
        )
        self.assertEqual(self.store.gc(now=50), 0)
        self.assertEqual(self.store.gc(now=121), 1)
        self.assertEqual(json.loads(self.path.read_text()), {})

    def test_gc_expires_old_active_route_before_later_deletion(self):
        self.register(now=10)
        self.assertEqual(self.store.gc(now=111), 0)
        restarted = ReplyRouteStore(self.path, max_age_seconds=100)
        self.assertEqual(restarted.claim(
            from_channel="DPRIVATE", from_thread="100.1", user_id="UAPPROVER",
            is_approver=True, now=112,
        ).status, "expired")
        self.assertEqual(restarted.gc(now=212), 1)


if __name__ == "__main__":
    unittest.main()
