from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))

from turn_reporter import (
    PROGRESS_POST_INTERVAL,
    STATUS_FIRST_AFTER,
    TurnReporter,
    format_elapsed,
    is_for_human,
    step_line,
)


class FakeSlack:
    def __init__(self):
        self.posts = []
        self.status_posts = []
        self.updates = []

    def post(self, text):
        self.posts.append(text)

    def post_status(self, text):
        self.status_posts.append(text)
        return "status-ts"

    def update_status(self, ts, text):
        self.updates.append((ts, text))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def reporter():
    slack, clock = FakeSlack(), Clock()
    turn = TurnReporter(slack.post, slack.post_status, slack.update_status, clock=clock)
    turn.begin()
    return turn, slack, clock


class TurnReporterTest(unittest.TestCase):
    def test_narration_updates_status_and_never_posts(self):
        turn, slack, clock = reporter()
        turn.text("Stage 0: staging the diff")
        turn.tool_used()
        clock.now += 10
        turn.text("Dispatching the full roster")
        turn.tool_used()

        self.assertEqual([], slack.posts)
        self.assertEqual(1, len(slack.status_posts))
        self.assertIn("Stage 0: staging the diff", slack.status_posts[0])
        self.assertIn("Dispatching the full roster", slack.updates[-1][1])

    def test_text_after_the_last_tool_call_is_the_reply(self):
        turn, slack, _ = reporter()
        turn.text("Running the build")
        turn.tool_used()
        turn.text("Green with the fix, red without it.")
        turn.text("Draft PR is up.")
        turn.finish()

        self.assertEqual(["Green with the fix, red without it.\n\nDraft PR is up."], slack.posts)
        self.assertIn("done", slack.updates[-1][1])

    def test_marked_messages_post_immediately_and_are_not_repeated(self):
        turn, slack, _ = reporter()
        turn.text("❓ Merge now or wait for your review? I recommend waiting.")
        turn.text("💬 Answering your question: the build is at test-compile.")
        turn.finish()

        self.assertEqual(2, len(slack.posts))
        self.assertTrue(slack.posts[0].startswith("❓"))
        self.assertEqual([], slack.status_posts)

    def test_no_reply_when_the_turn_ended_on_a_tool_call(self):
        turn, slack, _ = reporter()
        turn.text("Waiting on CodeRabbit")
        turn.tool_used()
        turn.finish()

        self.assertEqual([], slack.posts)

    def test_finish_without_reply_for_escalation_or_stop(self):
        turn, slack, _ = reporter()
        turn.text("Sensitive detail that must not reach the thread")
        turn.finish(post_reply=False, state="stopped")

        self.assertEqual([], slack.posts)
        self.assertIn("stopped", slack.updates[-1][1])

    def test_status_edits_are_throttled(self):
        turn, slack, clock = reporter()
        turn.text("one")
        clock.now += 1
        turn.text("two")
        clock.now += 1
        turn.text("three")

        self.assertEqual([], slack.updates)
        clock.now += 10
        turn.text("four")
        self.assertIn("four", slack.updates[-1][1])

    def test_heartbeat_creates_status_on_quiet_tool_only_turns(self):
        turn, slack, clock = reporter()
        turn.tick()
        self.assertEqual([], slack.status_posts)

        clock.now += STATUS_FIRST_AFTER + 1
        turn.tick()
        self.assertEqual(1, len(slack.status_posts))
        self.assertIn("working · 1m", slack.status_posts[0])

    def test_heartbeat_shows_quiet_time_and_posts_progress_on_long_turns(self):
        turn, slack, clock = reporter()
        turn.text("Waiting on the c4 build")
        clock.now += 90
        turn.tick()
        self.assertIn("last activity 1m ago", slack.updates[-1][1])
        self.assertEqual([], slack.posts)

        clock.now += PROGRESS_POST_INTERVAL
        turn.tick()
        self.assertEqual(1, len(slack.posts))
        self.assertTrue(slack.posts[0].startswith("Still working (21m): Waiting on the c4 build"))

        clock.now += 60
        turn.tick()
        self.assertEqual(1, len(slack.posts))

    def test_marked_message_resets_the_progress_timer(self):
        turn, slack, clock = reporter()
        clock.now += PROGRESS_POST_INTERVAL - 10
        turn.text("💬 Still on the review gate.")
        clock.now += 20
        turn.tick()
        self.assertEqual(1, len(slack.posts))

    def test_notes_join_the_status_line(self):
        turn, slack, clock = reporter()
        turn.text("Reviewing")
        clock.now += 10
        turn.set_note("skill: cc-code-review")
        self.assertIn("skill: cc-code-review", slack.updates[-1][1])

    def test_inactive_reporter_ignores_everything(self):
        turn, slack, _ = reporter()
        turn.finish()
        turn.text("late narration")
        turn.tick()
        turn.finish()
        self.assertEqual([], slack.posts)
        self.assertEqual([], slack.status_posts)

    def test_slack_failures_do_not_raise(self):
        def boom(*_):
            raise RuntimeError("slack down")

        turn = TurnReporter(lambda _: None, boom, boom, clock=Clock())
        turn.begin()
        turn.text("narration")
        turn.finish()

    def test_helpers(self):
        self.assertTrue(is_for_human("  ❓ question"))
        self.assertFalse(is_for_human("plain narration"))
        self.assertEqual("Stage 0:", step_line("\n**Stage 0:**\nmore"))
        self.assertEqual(200, len(step_line("x" * 500)))
        self.assertEqual("45s", format_elapsed(45))
        self.assertEqual("1h05m", format_elapsed(3900))


if __name__ == "__main__":
    unittest.main()
