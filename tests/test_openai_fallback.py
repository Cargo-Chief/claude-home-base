import json
import subprocess
import unittest
from unittest.mock import patch

from openai_fallback import (
    is_claude_limit_notice,
    model_notice_text,
    parse_codex_events,
    run_codex_turn,
)


class OpenAIFallbackTest(unittest.TestCase):
    def test_recognizes_claude_limit_notices(self):
        for notice in (
            "You've hit your limit · resets 4pm (UTC)",
            "You've hit your usage limit · resets at 4:00 pm",
            "You've hit your weekly limit · resets Sep 2 at 7pm (America/Los_Angeles)",
        ):
            with self.subTest(notice=notice):
                self.assertTrue(is_claude_limit_notice(notice))

    def test_does_not_treat_ordinary_errors_as_limit_notices(self):
        self.assertFalse(is_claude_limit_notice("Claude process exited with status 1"))
        self.assertFalse(is_claude_limit_notice("You've hit your tool limit"))

    def test_model_notice_ignores_synthetic_limit_envelope(self):
        self.assertIsNone(model_notice_text("<synthetic>"))

    def test_model_notice_attributes_and_deduplicates_fallback(self):
        self.assertEqual("model: gpt-5.6-sol", model_notice_text("gpt-5.6-sol"))
        self.assertIsNone(model_notice_text("gpt-5.6-sol", "gpt-5.6-sol"))
        self.assertEqual(
            "model changed: gpt-5.6-sol → gpt-5.6-terra",
            model_notice_text("gpt-5.6-terra", "gpt-5.6-sol"),
        )

    def test_parser_keeps_only_session_text_and_usage(self):
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "T1"}),
            json.dumps({"type": "item.completed", "item": {
                "type": "command_execution", "command": "secret-shaped but ignored",
            }}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "finished",
            }}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12}}),
        ]
        result = parse_codex_events(lines)
        self.assertEqual("T1", result.session_id)
        self.assertEqual(["finished"], result.texts)
        self.assertEqual({"input_tokens": 12}, result.usage)

    def test_parser_tolerates_noise_and_records_failure(self):
        result = parse_codex_events([
            "not json",
            json.dumps({"type": "turn.failed", "error": "out of capacity"}),
        ])
        self.assertEqual("out of capacity", result.error)

    @patch("openai_fallback.subprocess.Popen")
    def test_runner_passes_prompt_via_stdin_and_never_raises_stderr(self, popen):
        process = popen.return_value
        process.returncode = 7
        process.communicate.return_value = ("", "credential-bearing diagnostic")
        observed = []
        result = run_codex_turn(
            ["codex", "exec", "--json", "-"], "authority envelope",
            cwd="/workspace", env={"SAFE": "yes"}, timeout=10,
            on_process=observed.append,
        )
        self.assertEqual("Codex fallback exited with status 7", result.error)
        self.assertNotIn("credential", result.error)
        process.communicate.assert_called_once_with("authority envelope", timeout=10)
        self.assertEqual([process, None], observed)

    @patch("openai_fallback.subprocess.Popen")
    def test_runner_kills_timed_out_process(self, popen):
        process = popen.return_value
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="codex", timeout=10),
            ("", ""),
        ]
        result = run_codex_turn(
            ["codex"], "prompt", cwd="/workspace", env={}, timeout=10,
        )
        self.assertEqual("Codex fallback timed out", result.error)
        process.kill.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
