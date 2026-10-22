import ast
import json
import logging
import subprocess
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from session_lifecycle import owner_stream_event_is_activity

BOT = Path(__file__).parents[1] / "bot.py"
FUNCTIONS = (
    "_reader_loop", "_close_turn_after_error", "_post_context_notice", "_track_context",
)


def load_functions(**overrides):
    """Compile the named bot.py functions against stub globals; bot itself needs Slack to import."""
    tree = ast.parse(BOT.read_text(encoding="utf-8"))
    wanted = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    constants = [
        node for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.startswith("CTX_")
    ]
    module = ast.Module(body=constants + wanted, type_ignores=[])
    namespace = {
        "LiveSession": object,
        "json": json,
        "time": __import__("time"),
        "subprocess": subprocess,
        "logger": logging.getLogger("test-reader"),
        "audit_logger": logging.getLogger("test-reader-audit"),
        "owner_stream_event_is_activity": owner_stream_event_is_activity,
        "_track_model": lambda session, data: None,
        "_notice_output_after_turn": lambda session: None,
        "_post_skill_notice": lambda *args: None,
        "is_claude_limit_notice": lambda text: False,
        "_ATTACH_PATTERN": __import__("re").compile(r"\A\Z"),
        "format_delegation_audit": lambda *args, **kwargs: "",
        "_live_sessions_lock": threading.Lock(),
        "_live_sessions": {},
        "slack_client": None,
    }
    namespace.update(overrides)
    exec(compile(module, str(BOT), "exec"), namespace)
    return namespace


class FakeTracker:
    def observe(self, data):
        pass

    def finish_turn(self):
        return []


class FakeReporter:
    def __init__(self, active=False):
        self.active = active
        self.notes = []
        self.finished = []

    def set_note(self, note):
        self.notes.append(note)

    def finish(self, **kwargs):
        self.finished.append(kwargs)

    def text(self, text):
        pass

    def tool_used(self):
        pass

    def activity(self):
        pass


class FakeProc:
    def __init__(self, events, alive=False):
        self.stdout = [json.dumps(event) + "\n" for event in events]
        self.pid = 4242
        self.alive = alive
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


class FakeSession:
    def __init__(self, events, *, on_text=None, alive=False, reporter=None):
        self.proc = FakeProc(events, alive=alive)
        self.thread_ts = "1.0"
        self.channel = "C1"
        self.user_id = "U1"
        self.session_id = None
        self.workspace = None
        self.reporter = reporter
        self.delegation_tracker = FakeTracker()
        self.escalation_message_file = None
        self.private_escalation_pending = False
        self.first_tool_seen = False
        self.pre_tool_text = []
        self.pending_reactions = []
        self.turn_lock = threading.Lock()
        self.after_turn_noticed = True
        self._turn_done = threading.Event()
        self._on_text = on_text
        self.last_activity = 0
        self.turn_activity = 0
        self.ctx_notified_level = 0
        self.ctx_warned = False


def assistant_text(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


class ReaderResilienceTest(unittest.TestCase):
    def test_a_failed_slack_post_does_not_end_the_reader(self):
        posted = []

        def on_text(text):
            if text == "first":
                raise TimeoutError("The read operation timed out")
            posted.append(text)

        session = FakeSession(
            [assistant_text("first"), {"type": "result"}, assistant_text("second"), {"type": "result"}],
            on_text=on_text,
        )
        load_functions()["_reader_loop"](session)
        self.assertEqual(["second"], posted)
        self.assertTrue(session._turn_done.is_set())

    def test_a_failure_inside_the_result_event_still_closes_the_turn(self):
        def on_text(text):
            raise TimeoutError("The read operation timed out")

        reporter = FakeReporter()
        session = FakeSession([assistant_text("reply"), {"type": "result"}], on_text=on_text, reporter=reporter)
        session.pre_tool_text = []
        observed = []
        functions = load_functions()
        original = session._turn_done.set

        def record_set():
            observed.append(list(reporter.finished))
            original()

        session._turn_done.set = record_set
        functions["_reader_loop"](session)
        self.assertTrue(observed, "turn was never marked done")
        self.assertEqual({"post_reply": True}, observed[0][0])
        self.assertEqual([], session.pre_tool_text)

    def test_non_object_json_is_skipped(self):
        session = FakeSession([[1, 2], {"type": "result"}])
        load_functions()["_reader_loop"](session)
        self.assertTrue(session._turn_done.is_set())

    def test_an_unread_process_is_stopped_when_the_reader_ends(self):
        session = FakeSession([], alive=True)
        load_functions()["_reader_loop"](session)
        self.assertTrue(session.proc.terminated)

    def test_an_exited_process_is_left_alone(self):
        session = FakeSession([], alive=False)
        load_functions()["_reader_loop"](session)
        self.assertFalse(session.proc.terminated)


class FakeSlack:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs["text"])


def usage_event(tokens):
    return {"type": "assistant", "message": {"usage": {"cache_read_input_tokens": tokens}, "content": []}}


class ContextNoticeTest(unittest.TestCase):
    def test_thread_gets_one_message_at_eighty_percent_and_status_line_gets_every_step(self):
        slack = FakeSlack()
        track = load_functions(slack_client=slack)["_track_context"]
        reporter = FakeReporter(active=True)
        session = FakeSession([], reporter=reporter)
        for tokens in (150_000, 450_000, 750_000, 810_000, 910_000, 960_000):
            track(session, usage_event(tokens))
        self.assertEqual(1, len(slack.posts))
        self.assertIn("81%", slack.posts[0])
        self.assertEqual(["context 15%", "context 45%", "context 75%", "context 81%", "context 91%"], reporter.notes)

    def test_no_message_below_the_warning_line_outside_a_turn(self):
        slack = FakeSlack()
        track = load_functions(slack_client=slack)["_track_context"]
        session = FakeSession([])
        for tokens in (150_000, 450_000, 790_000):
            track(session, usage_event(tokens))
        self.assertEqual([], slack.posts)

    def test_compaction_re_arms_the_warning(self):
        slack = FakeSlack()
        track = load_functions(slack_client=slack)["_track_context"]
        session = FakeSession([])
        for tokens in (850_000, 120_000, 820_000):
            track(session, usage_event(tokens))
        self.assertEqual(2, len(slack.posts))


if __name__ == "__main__":
    unittest.main()
