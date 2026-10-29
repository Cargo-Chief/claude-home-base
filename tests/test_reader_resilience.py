import ast
import builtins
import json
import logging
import re
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from session_lifecycle import BackgroundAgents, PendingTools, owner_stream_event_is_activity

BOT = Path(__file__).parents[1] / "bot.py"
FUNCTIONS = (
    "_reader_loop", "_deliver_text", "_stop_unread_process", "_post_context_notice", "_track_context",
    "_owner_turn_busy", "_finish_turn", "_take_review_marker", "_pause_review_turn", "_stop_review",
)


class SafetyError(Exception):
    pass


def _bot_tree():
    return ast.parse(BOT.read_text(encoding="utf-8"))


def _globals_used(functions):
    """Names the functions read from module scope; every one must be stubbed."""
    used = set()
    for function in functions:
        local = {arg.arg for arg in function.args.args}
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                local.add(node.name)
        for node in ast.walk(function):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id not in local and not hasattr(builtins, node.id)):
                used.add(node.id)
    return used


def _fake_stop_process_tree(proc):
    proc.calls.append("stop_tree")
    proc.alive = False
    return True


def load_functions(**overrides):
    """Compile the named bot.py functions against stub globals; bot itself needs Slack to import."""
    tree = _bot_tree()
    wanted = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    constants = [
        node for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and (node.targets[0].id.startswith("CTX_") or node.targets[0].id == "_REVIEW_DONE_LINE")
    ]
    namespace = {
        "LiveSession": object,
        "Path": Path,
        "RUNTIME_POLICY": None,
        "SOURCE_DIR": Path("."),
        "SafetyError": SafetyError,
        "json": json,
        "re": re,
        "threading": threading,
        "time": time,
        "sys": sys,
        "subprocess": subprocess,
        "logger": logging.getLogger("test-reader"),
        "audit_logger": logging.getLogger("test-reader-audit"),
        "owner_stream_event_is_activity": owner_stream_event_is_activity,
        "contains_private_escalation_action": lambda *args: False,
        "private_escalation_status": lambda **kwargs: "escalated",
        "consume_bundle_claim": lambda *args, **kwargs: None,
        "consume_parking_claim": lambda *args, **kwargs: None,
        "prepare_thread_workspace": lambda *args, **kwargs: None,
        "_save_session": lambda thread_ts, sid: None,
        "_track_model": lambda session, data: None,
        "_notice_output_after_turn": lambda session: None,
        "_post_skill_notice": lambda *args: None,
        "is_claude_limit_notice": lambda text: False,
        "_ATTACH_PATTERN": re.compile(r"\A\Z"),
        "format_delegation_audit": lambda *args, **kwargs: "",
        "_live_sessions_lock": threading.Lock(),
        "_live_sessions": {},
        "slack_client": None,
        "stop_process_tree": _fake_stop_process_tree,
        "governed_delegate_active": lambda pid_file: False,
        "REVIEW_DONE_MARKER": "REVIEW COMPLETE",
    }
    namespace.update(overrides)
    exec(compile(ast.Module(body=constants + wanted, type_ignores=[]), str(BOT), "exec"), namespace)
    return namespace


class FakeTracker:
    def observe(self, data):
        pass

    def finish_turn(self):
        return []


class FakeReporter:
    def __init__(self, active=False):
        self.active = active
        self.answered = False
        self.notes = []
        self.finished = []

    def set_note(self, note):
        self.notes.append(note)

    def finish(self, **kwargs):
        if self.active and kwargs.get("post_reply", True):
            self.posted_tail = getattr(self, "posted_tail", []) + getattr(self, "tail", [])
        self.tail = []
        self.finished.append(kwargs)

    def text(self, text):
        self.texts = getattr(self, "texts", []) + [text]
        self.tail = getattr(self, "tail", []) + [text]

    def tool_used(self):
        """Like TurnReporter.tool_used: text before a tool call is narration, not the reply."""
        self.tools = getattr(self, "tools", 0) + 1
        self.tail = []

    def delivered(self):
        self.answered = True

    def activity(self):
        pass


class FakeProc:
    def __init__(self, events, *, alive=False, events_raise=None):
        self.lines = [json.dumps(event) + "\n" for event in events]
        self.events_raise = events_raise
        self.pid = 4242
        self.alive = alive
        self.calls = []

    @property
    def stdout(self):
        yield from self.lines
        if self.events_raise:
            raise self.events_raise

    def poll(self):
        return None if self.alive else 0



class FakeSession:
    def __init__(self, events, *, on_text=None, reporter=None, **proc):
        self.proc = FakeProc(events, **proc)
        self.thread_ts = "1.0"
        self.channel = "C1"
        self.user_id = "U1"
        self.session_id = None
        self.workspace = None
        self.reporter = reporter
        self.delegation_tracker = FakeTracker()
        self.pending_tools = PendingTools()
        self.review = False
        self.review_done = False
        self.background_agents = BackgroundAgents()
        self.stop_done = None
        self.escalation_message_file = None
        self.escalation_receipt_file = None
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


def assistant_text(*texts):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": t} for t in texts]}}


def result(sid="s-2"):
    return {"type": "result", "session_id": sid}


def agent_started(task_id):
    return {"type": "system", "subtype": "task_started", "task_id": task_id, "task_type": "local_agent"}


def task_done(task_id):
    return {"type": "system", "subtype": "task_notification", "task_id": task_id, "status": "completed"}


class ReviewReaderTest(unittest.TestCase):
    def review_session(self, events, posted=None):
        session = FakeSession(events, on_text=(posted.append if posted is not None else lambda text: None),
                              reporter=FakeReporter(active=True))
        session.review = True
        return session

    def finished_by_result(self, session):
        """reporter.finish calls other than the reader's own process-exit one."""
        return [call for call in session.reporter.finished if call != {"state": "stopped"}]

    def test_a_result_without_the_completion_line_pauses_and_folds_narration(self):
        session = self.review_session([assistant_text("launched finders, waiting"), result()])
        load_functions()["_reader_loop"](session)
        self.assertEqual([], self.finished_by_result(session))
        self.assertEqual(["launched finders, waiting"], session.reporter.texts)
        self.assertEqual(1, session.reporter.tools, "narration is folded out of the reply tail")

    def test_the_completion_line_ends_the_review_and_is_not_posted(self):
        posted = []
        session = self.review_session([
            agent_started("a"), result(), task_done("a"),
            assistant_text("Posted 3 findings on the PR.\nREVIEW COMPLETE"), result("s-5"),
        ], posted)
        load_functions()["_reader_loop"](session)
        self.assertTrue(session.review_done)
        self.assertEqual(["Posted 3 findings on the PR."], posted)
        self.assertEqual([{"post_reply": True}], self.finished_by_result(session))

    def test_a_summary_written_while_a_finder_runs_is_posted_not_folded(self):
        posted = []
        session = self.review_session([
            agent_started("a"), assistant_text("Posted 3 findings.\nREVIEW COMPLETE"), result(),
        ], posted)
        load_functions()["_reader_loop"](session)
        self.assertEqual(["Posted 3 findings."], posted)
        self.assertEqual([], self.finished_by_result(session), "still paused while the finder runs")

    def test_common_spellings_of_the_completion_line_count(self):
        for line, left in [("REVIEW COMPLETE.", ""), ("_review complete_", ""), ("> **REVIEW COMPLETE**", ""),
                           ("Findings posted. REVIEW COMPLETE", "Findings posted")]:
            with self.subTest(line=line):
                posted = []
                session = self.review_session([assistant_text(line), result()], posted)
                load_functions()["_reader_loop"](session)
                self.assertTrue(session.review_done)
                self.assertEqual([left] if left else [], posted)

    def test_a_mention_of_the_words_mid_sentence_does_not_count(self):
        session = self.review_session([assistant_text("I will write REVIEW COMPLETE when done, not yet."), result()])
        load_functions()["_reader_loop"](session)
        self.assertFalse(session.review_done)

    def test_a_limit_notice_held_at_a_pause_still_reaches_the_turn(self):
        posted = []
        session = self.review_session([assistant_text("LIMIT"), result()], posted)
        load_functions(is_claude_limit_notice=lambda text: text == "LIMIT")["_reader_loop"](session)
        self.assertEqual(["LIMIT"], posted)

    def test_a_review_never_consumes_bundle_or_parking_claims(self):
        consumed = []
        session = self.review_session([assistant_text("REVIEW COMPLETE"), result()])
        session.workspace = type("W", (), {
            "bundle_claim_file": Path(__file__), "parking_claim_file": Path(__file__), "root": Path("."),
        })()
        load_functions(
            consume_bundle_claim=lambda *args, **kwargs: consumed.append("bundle"),
            consume_parking_claim=lambda *args, **kwargs: consumed.append("parking"),
        )["_reader_loop"](session)
        self.assertEqual([], consumed)

    def test_stopping_a_review_kills_it_and_releases_the_request(self):
        session = self.review_session([])
        session.proc.alive = True
        outcome = load_functions()["_stop_review"](session)
        self.assertEqual("review stopped", outcome)
        self.assertEqual(["stop_tree"], session.proc.calls)
        self.assertTrue(session.stop_done.is_set())
        self.assertTrue(session._turn_done.is_set())


class HarnessTest(unittest.TestCase):
    def test_every_global_the_functions_read_is_stubbed(self):
        tree = _bot_tree()
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
        self.assertEqual(len(FUNCTIONS), len(functions))
        missing = _globals_used(functions) - set(load_functions())
        self.assertEqual(set(), missing)


class ReaderResilienceTest(unittest.TestCase):
    def test_a_failed_post_skips_only_that_post(self):
        posted = []

        def on_text(text):
            if text == "first":
                raise TimeoutError("The read operation timed out")
            posted.append(text)

        session = FakeSession([assistant_text("first", "second"), result()], on_text=on_text)
        session.first_tool_seen = True
        load_functions()["_reader_loop"](session)
        self.assertEqual(["second"], posted)
        self.assertEqual("s-2", session.session_id, "the result event was not processed")

    def test_a_failed_post_in_the_result_event_still_finishes_the_turn(self):
        saved = []
        reporter = FakeReporter()

        def on_text(text):
            raise TimeoutError("The read operation timed out")

        session = FakeSession([assistant_text("reply"), result("s-9")], on_text=on_text, reporter=reporter)
        load_functions(_save_session=lambda thread, sid: saved.append(sid))["_reader_loop"](session)
        self.assertEqual(["s-9"], saved)
        self.assertEqual({"post_reply": True}, reporter.finished[0])

    def test_non_object_json_is_skipped_and_reading_continues(self):
        session = FakeSession([[1, 2], result("s-3")])
        load_functions()["_reader_loop"](session)
        self.assertEqual("s-3", session.session_id)

    def test_an_escalation_check_error_still_ends_the_reader(self):
        posted = []

        def broken(*args):
            raise ValueError("lstat: embedded null character in path")

        session = FakeSession([assistant_text("private"), assistant_text("later"), result()],
                              on_text=posted.append, alive=True)
        session.escalation_message_file = Path("escalation.txt")
        session.first_tool_seen = True
        load_functions(contains_private_escalation_action=broken)["_reader_loop"](session)
        self.assertEqual([], posted)
        self.assertEqual(["stop_tree"], session.proc.calls)

    def test_a_failed_reader_drops_the_session_before_stopping_its_process(self):
        sessions = {}
        order = []
        session = FakeSession([], alive=True, events_raise=UnicodeDecodeError("utf-8", b"", 0, 1, "bad"))
        sessions[session.thread_ts] = session

        def stop(proc):
            order.append(("registered", session.thread_ts in sessions, "done", session._turn_done.is_set()))
            return _fake_stop_process_tree(proc)

        load_functions(_live_sessions=sessions, stop_process_tree=stop)["_reader_loop"](session)
        self.assertEqual([("registered", False, "done", False)], order)
        self.assertTrue(session._turn_done.is_set())

    def test_a_tool_free_reply_counts_as_delivered(self):
        posted = []
        reporter = FakeReporter(active=True)
        session = FakeSession([assistant_text("Plain answer"), result()], on_text=posted.append, reporter=reporter)
        load_functions()["_reader_loop"](session)
        self.assertEqual(["Plain answer"], posted)
        self.assertTrue(reporter.answered, "finish() would post the no-reply notice under a real reply")

    def test_a_stop_that_raises_still_ends_the_reader(self):
        def broken(proc):
            raise OSError("ps unavailable")

        session = FakeSession([], alive=True, events_raise=OSError("pipe"))
        load_functions(stop_process_tree=broken)["_reader_loop"](session)
        self.assertTrue(session._turn_done.is_set())

    def test_a_normal_end_of_output_leaves_the_process_alone(self):
        session = FakeSession([result()], alive=True)
        load_functions()["_reader_loop"](session)
        self.assertEqual([], session.proc.calls)


class OwnerTurnBusyTest(unittest.TestCase):
    def test_a_pending_owner_tool_call_keeps_the_turn_busy(self):
        busy = load_functions()["_owner_turn_busy"]
        session = FakeSession([])
        self.assertFalse(busy(session))
        session.pending_tools.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1"}]}})
        self.assertTrue(busy(session))
        session.pending_tools.observe({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}})
        self.assertFalse(busy(session))

    def test_a_running_background_agent_keeps_only_a_review_busy(self):
        busy = load_functions()["_owner_turn_busy"]
        session = FakeSession([])
        session.background_agents.observe(agent_started("a"))
        self.assertFalse(busy(session))
        session.review = True
        self.assertTrue(busy(session))
        session.background_agents.observe(task_done("a"))
        self.assertFalse(busy(session))

    def test_a_live_governed_delegate_keeps_the_turn_busy(self):
        busy = load_functions(governed_delegate_active=lambda pid_file: True)["_owner_turn_busy"]
        session = FakeSession([])
        session.workspace = type("Workspace", (), {"delegate_pid_file": Path("delegate.pid")})()
        self.assertTrue(busy(session))


class FakeSlack:
    def __init__(self, fail_first=False):
        self.posts = []
        self.fail_first = fail_first

    def chat_postMessage(self, **kwargs):
        if self.fail_first:
            self.fail_first = False
            raise ConnectionError("no route")
        self.posts.append(kwargs["text"])


def usage_event(tokens):
    return {"type": "assistant", "message": {"usage": {"cache_read_input_tokens": tokens}, "content": []}}


def track_all(slack, session, readings):
    track = load_functions(slack_client=slack)["_track_context"]
    for tokens in readings:
        track(session, usage_event(tokens))


class ContextNoticeTest(unittest.TestCase):
    def test_thread_gets_one_message_at_eighty_percent_and_status_line_gets_every_step(self):
        slack = FakeSlack()
        reporter = FakeReporter(active=True)
        session = FakeSession([], reporter=reporter)
        track_all(slack, session, (150_000, 450_000, 750_000, 810_000, 910_000, 960_000))
        self.assertEqual(1, len(slack.posts))
        self.assertIn("81%", slack.posts[0])
        self.assertEqual(["context 15%", "context 45%", "context 75%", "context 81%", "context 91%"], reporter.notes)

    def test_no_message_below_the_warning_line(self):
        slack = FakeSlack()
        track_all(slack, FakeSession([]), (150_000, 450_000, 790_000))
        self.assertEqual([], slack.posts)

    def test_a_failed_warning_is_retried_at_the_next_step(self):
        slack = FakeSlack(fail_first=True)
        track_all(slack, FakeSession([]), (810_000, 910_000))
        self.assertEqual(1, len(slack.posts))
        self.assertIn("91%", slack.posts[0])

    def test_hovering_at_the_line_does_not_repeat_the_warning(self):
        slack = FakeSlack()
        track_all(slack, FakeSession([]), (805_000, 798_000, 803_000))
        self.assertEqual(1, len(slack.posts))

    def test_compaction_re_arms_the_warning(self):
        slack = FakeSlack()
        track_all(slack, FakeSession([]), (850_000, 120_000, 820_000))
        self.assertEqual(2, len(slack.posts))


if __name__ == "__main__":
    unittest.main()
