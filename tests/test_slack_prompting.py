import unittest

from slack_prompting import relevance_prefix


class RelevancePrefixTests(unittest.TestCase):
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
