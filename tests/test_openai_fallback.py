import json
import threading
import unittest
from unittest.mock import patch

from openai_fallback import (
    fallback_error_kind,
    is_claude_limit_notice,
    model_notice_text,
    parse_codex_events,
    run_codex_turn,
)


class OpenAIFallbackTest(unittest.TestCase):
    def test_fallback_errors_have_content_free_log_categories(self):
        self.assertEqual("timeout", fallback_error_kind("Codex fallback timed out"))
        self.assertEqual("start", fallback_error_kind("Codex fallback could not start"))
        self.assertEqual("provider", fallback_error_kind("sensitive provider detail"))

    def test_recognizes_claude_limit_notices(self):
        for notice in (
            "You've hit your limit · resets 4pm (UTC)",
            "You've hit your usage limit · resets at 4:00 pm",
            "You've hit your weekly limit · resets Sep 2 at 7pm (America/Los_Angeles)",
            "You've hit your monthly spend limit · raise it at claude.ai/settings/usage?from=cc_cli_limit_message · your session limit resets 3:50pm (America/Los_Angeles)",
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
        process.stdout = iter([])
        process.stderr = iter(["credential-bearing diagnostic"])
        observed = []
        result = run_codex_turn(
            ["codex", "exec", "--json", "-"], "authority envelope",
            cwd="/workspace", env={"SAFE": "yes"}, timeout=10,
            on_process=observed.append,
        )
        self.assertEqual("Codex fallback exited with status 7", result.error)
        self.assertNotIn("credential", result.error)
        process.stdin.write.assert_called_once_with("authority envelope")
        process.stdin.close.assert_called_once_with()
        self.assertEqual([process, None], observed)

    @patch("openai_fallback.subprocess.Popen")
    def test_runner_kills_timed_out_process(self, popen):
        process = popen.return_value
        process.returncode = 0
        process.poll.return_value = None
        released = threading.Event()

        def blocked_stdout():
            released.wait(timeout=1)
            return
            yield

        process.stdout = blocked_stdout()
        process.stderr = iter([])
        process.kill.side_effect = released.set
        result = run_codex_turn(
            ["codex"], "prompt", cwd="/workspace", env={}, timeout=0.01,
        )
        self.assertEqual("Codex fallback timed out", result.error)
        process.kill.assert_called_once_with()

    @patch("openai_fallback.subprocess.Popen")
    def test_runner_times_out_when_prompt_delivery_blocks(self, popen):
        process = popen.return_value
        process.returncode = 0
        process.poll.return_value = None
        released = threading.Event()
        process.stdin.write.side_effect = lambda _prompt: released.wait(timeout=1)

        def blocked_stdout():
            released.wait(timeout=1)
            return
            yield

        process.stdout = blocked_stdout()
        process.stderr = iter([])
        process.kill.side_effect = released.set

        result = run_codex_turn(
            ["codex"], "prompt", cwd="/workspace", env={}, timeout=0.01,
        )

        self.assertEqual("Codex fallback timed out", result.error)
        process.kill.assert_called_once_with()

    @patch("openai_fallback.subprocess.Popen")
    def test_runner_reaps_child_after_prompt_write_failure(self, popen):
        process = popen.return_value
        process.returncode = 1
        process.stdin.write.side_effect = BrokenPipeError
        process.stdout = iter([])
        process.stderr = iter([])

        result = run_codex_turn(
            ["codex"], "prompt", cwd="/workspace", env={}, timeout=1,
        )

        self.assertEqual("Codex fallback could not start", result.error)
        process.wait.assert_called_once_with(timeout=5)

    @patch("openai_fallback.subprocess.Popen")
    def test_runner_keeps_waiting_while_delegate_is_active(self, popen):
        process = popen.return_value
        process.returncode = 0
        release_stdout = threading.Event()

        def delayed_stdout():
            release_stdout.wait(timeout=1)
            yield json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": "finished",
            }})

        process.stdout = delayed_stdout()
        process.stderr = iter([])

        def wait_for_completion(done_event, **kwargs):
            self.assertTrue(kwargs["delegate_active"]())
            self.assertEqual(17, kwargs["max_duration"])
            release_stdout.set()
            self.assertTrue(done_event.wait(timeout=1))
            return True

        result = run_codex_turn(
            ["codex"], "prompt", cwd="/workspace", env={}, timeout=10,
            delegate_active=lambda: True,
            max_duration=17,
            wait_for_completion=wait_for_completion,
        )
        self.assertEqual(["finished"], result.texts)

    @patch("openai_fallback.subprocess.Popen")
    def test_runner_reports_stream_progress_to_lifecycle_waiter(self, popen):
        process = popen.return_value
        process.returncode = 0
        emit_line = threading.Event()
        finish_stream = threading.Event()

        def streamed_stdout():
            emit_line.wait(timeout=1)
            yield json.dumps({"type": "thread.started", "thread_id": "T1"})
            finish_stream.wait(timeout=1)

        process.stdout = streamed_stdout()
        process.stderr = iter([])

        def wait_for_completion(done_event, **kwargs):
            before = kwargs["activity_at"]()
            emit_line.set()
            for _ in range(100):
                if kwargs["activity_at"]() > before:
                    break
                threading.Event().wait(0.01)
            self.assertGreater(kwargs["activity_at"](), before)
            finish_stream.set()
            self.assertTrue(done_event.wait(timeout=1))
            return True

        result = run_codex_turn(
            ["codex"], "prompt", cwd="/workspace", env={}, timeout=10,
            wait_for_completion=wait_for_completion,
        )
        self.assertEqual("T1", result.session_id)


if __name__ == "__main__":
    unittest.main()
