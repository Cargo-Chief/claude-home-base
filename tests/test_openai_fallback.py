from datetime import datetime, timedelta, timezone
import json
import threading
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from openai_fallback import (
    fallback_error_kind,
    is_claude_limit_notice,
    limit_reset_epoch,
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
            "You've hit your 5-hour limit · resets 12am (America/Los_Angeles)",
            "You've hit your session limit · resets 2:20am (America/Los_Angeles)",
            "You've hit your Opus limit · resets 3pm",
            "You've hit your weekly Opus limit · resets Sep 2 at 7pm (America/Los_Angeles)",
            "You've hit your monthly spend limit · raise it at claude.ai/settings/usage?from=cc_cli_limit_message · your session limit resets 3:50pm (America/Los_Angeles)",
        ):
            with self.subTest(notice=notice):
                self.assertTrue(is_claude_limit_notice(notice))

    def test_does_not_treat_ordinary_errors_as_limit_notices(self):
        self.assertFalse(is_claude_limit_notice("Claude process exited with status 1"))
        self.assertFalse(is_claude_limit_notice("You've hit your tool limit"))

    def test_reset_time_is_read_in_the_zone_the_notice_names(self):
        # 23:44 in Los Angeles is 07:44 UTC; "resets 12am" is 16 minutes away, not 16 hours.
        now = datetime(2026, 11, 11, 7, 44, tzinfo=timezone.utc)
        reset = limit_reset_epoch("You've hit your 5-hour limit · resets 12am (America/Los_Angeles)", now=now)
        self.assertEqual(datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc).timestamp() + 120, reset)

    def test_reset_time_with_minutes_rolls_to_the_next_day_once_past(self):
        now = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)
        reset = limit_reset_epoch("You've hit your limit · resets 3:50pm (UTC)", now=now)
        self.assertEqual(datetime(2026, 11, 12, 15, 50, tzinfo=timezone.utc).timestamp() + 120, reset)

    def test_reset_time_without_a_usable_zone_uses_the_clock_given(self):
        now = datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc)
        for notice in ("resets 4pm", "resets 4pm (Not/AZone)"):
            with self.subTest(notice=notice):
                self.assertEqual(datetime(2026, 11, 11, 16, 0, tzinfo=timezone.utc).timestamp() + 120,
                                 limit_reset_epoch(notice, now=now))

    def test_a_reset_just_past_is_now_not_tomorrow(self):
        now = datetime(2026, 11, 11, 16, 0, 5, tzinfo=timezone.utc)
        self.assertEqual(now.timestamp() + 120, limit_reset_epoch("resets 4pm (UTC)", now=now))

    def test_impossible_times_are_not_parsed(self):
        now = datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc)
        for notice in ("resets 4:99pm (UTC)", "resets 13pm (UTC)", "resets 0am (UTC)"):
            with self.subTest(notice=notice):
                self.assertIsNone(limit_reset_epoch(notice, now=now))

    def test_unloadable_zone_names_fall_back_without_raising(self):
        now = datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc)
        for zone in ("America", "../etc/passwd", "/etc/hosts", "x" * 5000, "PST"):
            with self.subTest(zone=zone[:20]):
                self.assertIsNotNone(limit_reset_epoch(f"resets 4pm ({zone})", now=now))

    def test_zoneless_reset_applies_the_local_zone_dst_rules(self):
        # 23:00 PDT the night before fall-back; 4am the next morning is PST, 12:00 UTC.
        now = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
        reset = limit_reset_epoch("resets 4am", now=now, local_zone=ZoneInfo("America/Los_Angeles"))
        self.assertEqual(datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc).timestamp() + 120, reset)

    def test_zoneless_reset_without_a_given_clock_uses_host_local_time(self):
        reset = limit_reset_epoch("resets 4am")
        expected = datetime.now().replace(hour=4, minute=0, second=0, microsecond=0)
        if expected <= datetime.now() - timedelta(minutes=5):
            expected += timedelta(days=1)
        self.assertAlmostEqual(max(expected.timestamp(), time.time()) + 120, reset, delta=5)

    def test_a_dated_weekly_reset_is_parsed(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        reset = limit_reset_epoch("You've hit your weekly limit · resets Sep 2 at 7pm (America/Los_Angeles)", now=now)
        self.assertEqual(datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc).timestamp() + 120, reset)

    def test_a_dated_reset_already_past_rolls_to_next_year(self):
        now = datetime(2026, 12, 30, 12, 0, tzinfo=timezone.utc)
        reset = limit_reset_epoch("resets Jan 2 at 7am (UTC)", now=now)
        self.assertEqual(datetime(2027, 1, 2, 7, 0, tzinfo=timezone.utc).timestamp() + 120, reset)

    def test_an_impossible_date_is_not_parsed(self):
        self.assertIsNone(limit_reset_epoch("resets Feb 30 at 7am (UTC)", now=datetime(2026, 1, 1, tzinfo=timezone.utc)))

    def test_no_reset_time_returns_none(self):
        self.assertIsNone(limit_reset_epoch("You've hit your weekly limit"))

    def test_model_notice_ignores_synthetic_limit_envelope(self):
        self.assertIsNone(model_notice_text("<synthetic>"))

    def test_model_notice_attributes_and_deduplicates_fallback(self):
        self.assertEqual("model: gpt-6-sol", model_notice_text("gpt-6-sol"))
        self.assertIsNone(model_notice_text("gpt-6-sol", "gpt-6-sol"))
        self.assertEqual(
            "model changed: gpt-6-sol → gpt-5.6-terra",
            model_notice_text("gpt-5.6-terra", "gpt-6-sol"),
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
