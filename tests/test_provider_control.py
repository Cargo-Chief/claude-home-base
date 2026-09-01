import unittest

from provider_control import parse_provider_command, use_openai_provider


class ProviderControlTest(unittest.TestCase):
    def test_parses_only_exact_provider_commands(self):
        self.assertEqual("openai", parse_provider_command("provider openai"))
        self.assertEqual("claude", parse_provider_command("<@U123> provider claude"))
        self.assertEqual("auto", parse_provider_command("provider auto"))
        self.assertEqual("status", parse_provider_command("provider status"))
        self.assertIsNone(parse_provider_command("please use provider openai"))

    def test_explicit_choice_wins_over_automatic_state(self):
        self.assertTrue(use_openai_provider(
            "openai", limit_paused=False, has_openai_session=False,
        ))
        self.assertFalse(use_openai_provider(
            "claude", limit_paused=True, has_openai_session=True,
        ))

    def test_auto_uses_limit_pause_or_pinned_openai_session(self):
        self.assertTrue(use_openai_provider(
            None, limit_paused=True, has_openai_session=False,
        ))
        self.assertTrue(use_openai_provider(
            None, limit_paused=False, has_openai_session=True,
        ))
        self.assertFalse(use_openai_provider(
            None, limit_paused=False, has_openai_session=False,
        ))


if __name__ == "__main__":
    unittest.main()
