import json
import subprocess
import unittest
from unittest.mock import patch

from openai_fallback import parse_codex_events, run_codex_turn


class OpenAIFallbackTest(unittest.TestCase):
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
