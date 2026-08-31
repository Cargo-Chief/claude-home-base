import unittest

from delegation_observability import DelegationTracker, format_delegation_audit


class DelegationTrackerTest(unittest.TestCase):
    def test_tracks_only_safe_routing_usage_and_verification_metadata(self):
        tracker = DelegationTracker()
        tracker.observe({
            "type": "system",
            "subtype": "task_started",
            "tool_use_id": "tool-1",
            "subagent_type": "cargo-chief-explore",
            "prompt": "must never enter the record",
            "description": "must never enter the record",
        })
        tracker.observe({
            "type": "assistant",
            "parent_tool_use_id": "tool-1",
            "message": {
                "model": "claude-haiku-4-5-20251001",
                "content": [{"type": "text", "text": "sensitive child return"}],
            },
        })
        tracker.observe({
            "type": "system",
            "subtype": "task_notification",
            "tool_use_id": "tool-1",
            "status": "completed",
            "summary": "must never enter the record",
            "usage": {"total_tokens": 1234, "tool_uses": 7, "duration_ms": 2500},
        })
        tracker.observe({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "secret"}},
                {"type": "text", "text": "private verification narrative"},
            ]},
        })
        records = tracker.finish_turn()
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("cargo-chief-explore", record.agent)
        self.assertEqual("claude-haiku-4-5-20251001", record.expected_model)
        self.assertEqual("medium", record.effort)
        self.assertEqual(1, record.owner_verification_tools)
        audit = format_delegation_audit(
            record, user="U1", channel="C1", thread="T1"
        )
        self.assertIn("MODEL_MATCH:true", audit)
        self.assertIn("TOKENS:1234", audit)
        self.assertNotIn("sensitive", audit)
        self.assertNotIn("secret", audit)
        self.assertNotIn("narrative", audit)

    def test_unknown_agent_is_visible_and_malformed_usage_is_safe(self):
        tracker = DelegationTracker()
        tracker.observe({
            "type": "system", "subtype": "task_started",
            "tool_use_id": "tool-2", "subagent_type": "Explore",
        })
        tracker.observe({
            "type": "system", "subtype": "task_notification",
            "tool_use_id": "tool-2", "status": "failed",
            "usage": {"total_tokens": "bad", "duration_ms": -4},
        })
        record = tracker.finish_turn()[0]
        self.assertEqual("unapproved", record.expected_model)
        self.assertEqual(0, record.total_tokens)
        self.assertEqual(0, record.duration_ms)
        audit = format_delegation_audit(record, user="U", channel="C", thread="T")
        self.assertIn("MODEL_MATCH:false", audit)
        self.assertIn("STATUS:failed", audit)

    def test_main_agent_invocation_is_not_counted_as_verification(self):
        tracker = DelegationTracker()
        tracker.observe({
            "type": "system", "subtype": "task_started",
            "tool_use_id": "tool-3", "subagent_type": "cargo-chief-bounded",
        })
        tracker.observe({
            "type": "system", "subtype": "task_notification",
            "tool_use_id": "tool-3", "status": "completed", "usage": {},
        })
        tracker.observe({
            "type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Agent", "input": {}},
            ]},
        })
        self.assertEqual(0, tracker.finish_turn()[0].owner_verification_tools)

    def test_background_delegation_survives_owner_turn_boundary(self):
        tracker = DelegationTracker()
        tracker.observe({
            "type": "system", "subtype": "task_started",
            "tool_use_id": "tool-bg", "subagent_type": "cargo-chief-mechanical",
        })

        # The owner may finish an interim turn while the background delegate
        # is still running. In-flight state must not be audited or discarded.
        self.assertEqual([], tracker.finish_turn())
        self.assertIn("tool-bg", tracker.records)

        tracker.observe({
            "type": "assistant", "parent_tool_use_id": "tool-bg",
            "message": {"model": "claude-sonnet-5", "content": []},
        })
        tracker.observe({
            "type": "system", "subtype": "task_notification",
            "tool_use_id": "tool-bg", "status": "completed",
            "usage": {"total_tokens": 4000, "tool_uses": 2, "duration_ms": 9000},
        })
        tracker.observe({
            "type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "hidden"}},
            ]},
        })

        records = tracker.finish_turn()
        self.assertEqual(1, len(records))
        self.assertEqual("completed", records[0].status)
        self.assertEqual("claude-sonnet-5", records[0].served_model)
        self.assertEqual(1, records[0].owner_verification_tools)
        self.assertEqual({}, tracker.records)


if __name__ == "__main__":
    unittest.main()
