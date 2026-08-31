import unittest

from slack_prompting import needs_relevance_prefix, relevance_prefix


class RelevancePrefixTests(unittest.TestCase):
    def test_explicit_app_mention_bypasses_model_relevance_filter(self):
        self.assertFalse(needs_relevance_prefix(
            event_type="app_mention",
            is_dm=False,
            has_existing_session=False,
            has_live_process=False,
            show_reminder=False,
        ))

    def test_cold_unmentioned_channel_message_uses_relevance_filter(self):
        self.assertTrue(needs_relevance_prefix(
            event_type="message",
            is_dm=False,
            has_existing_session=False,
            has_live_process=False,
            show_reminder=False,
        ))

    def test_ongoing_channel_thread_only_uses_scheduled_reminder(self):
        common = {
            "event_type": "message",
            "is_dm": False,
            "has_existing_session": True,
            "has_live_process": True,
        }
        self.assertFalse(needs_relevance_prefix(
            **common,
            show_reminder=False,
        ))
        self.assertTrue(needs_relevance_prefix(
            **common,
            show_reminder=True,
        ))

    def test_dm_never_uses_relevance_filter(self):
        self.assertFalse(needs_relevance_prefix(
            event_type="message",
            is_dm=True,
            has_existing_session=False,
            has_live_process=False,
            show_reminder=True,
        ))

    def test_cold_message_uses_each_service_bot_name(self):
        for bot_name in ("Ned", "Cargo Support"):
            with self.subTest(bot_name=bot_name):
                prompt = relevance_prefix("agent-test", bot_name)

                self.assertIn(f"name '{bot_name}'", prompt)
                self.assertNotIn("Andy", prompt)

    def test_reminder_uses_configured_bot_name(self):
        prompt = relevance_prefix("ned-test", "Ned", reminder=True)

        self.assertIn("name 'Ned'", prompt)
        self.assertNotIn("Andy", prompt)


if __name__ == "__main__":
    unittest.main()
