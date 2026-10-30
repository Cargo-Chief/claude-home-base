import ast
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import thread_handoff
from cargo_chief_safety import (
    HANDOFF_CLAIM_NAME,
    check_bundle_handoff,
    consume_bundle_claim,
    resolve_thread_bundle,
    ticket_keys,
    transfer_bundle,
    AuthorityPolicy,
    SafetyError,
    ThreadWorkspace,
    build_authority_envelope,
    handoff_prompt,
    prepare_thread_workspace,
)
from governed_delegation import BUDGET_UNIT, DelegationError, budget_status, delegation_verification_status
from thread_handoff import (
    HandoffRequest,
    consume_handoff_claim,
    discard_handoff_claim,
    handoff_refusal,
    new_thread_text,
    read_handoff_claim,
    source_thread_text,
)

BOT = Path(__file__).parents[1] / "bot.py"


def _bot_function(name):
    tree = ast.parse(BOT.read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _compile(names, namespace):
    functions = [_bot_function(name) for name in names]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(BOT), "exec"), namespace)
    return namespace


class ClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claim = Path(self.tmp.name) / HANDOFF_CLAIM_NAME

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        self.claim.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")

    def test_a_claim_may_name_a_well_formed_bundle(self):
        self.write({"title": "CN-9546 rotate", "brief": "b", "bundle": "CN-9546-rotate-key"})
        self.assertEqual(HandoffRequest("CN-9546 rotate", "b", "CN-9546-rotate-key"), consume_handoff_claim(self.claim))
        for bundle in ("../escape", "CN-9546", 7, "cn-1-x"):
            self.write({"title": "t", "brief": "b", "bundle": bundle})
            with self.assertRaisesRegex(SafetyError, "handoff bundle"):
                read_handoff_claim(self.claim)
        self.write({"title": "t", "brief": "b", "extra": "x"})
        with self.assertRaisesRegex(SafetyError, "optional"):
            read_handoff_claim(self.claim)

    def test_a_valid_claim_is_read_trimmed_and_consumed(self):
        self.write({"title": "  CN-1234 rotate keys ", "brief": " do the thing \n"})
        self.assertEqual(HandoffRequest("CN-1234 rotate keys", "do the thing"), consume_handoff_claim(self.claim))
        self.assertFalse(self.claim.exists())

    def test_no_claim_is_none(self):
        self.assertIsNone(consume_handoff_claim(self.claim))

    def test_an_invalid_claim_is_refused_and_still_consumed(self):
        for bad in (
            "not json",
            [],
            {"title": "CN-1"},
            {"title": "CN-1", "brief": "b", "user": "U1"},
            {"title": "two\nlines", "brief": "b"},
            {"title": "two\rlines", "brief": "b"},
            {"title": "two lines", "brief": "b"},
            {"title": "x" * 121, "brief": "b"},
            {"title": "CN-1", "brief": " "},
            {"title": "CN-1", "brief": "b" * 6001},
            {"title": 1, "brief": "b"},
            json.dumps({"title": "CN-1", "brief": "b" * 17000}),
        ):
            with self.subTest(bad=str(bad)[:40]):
                self.write(bad)
                with self.assertRaises(SafetyError):
                    consume_handoff_claim(self.claim)
                self.assertFalse(self.claim.exists())

    def test_a_symlinked_claim_is_refused_and_its_target_left_alone(self):
        target = Path(self.tmp.name) / "elsewhere.json"
        target.write_text(json.dumps({"title": "CN-1", "brief": "b"}), encoding="utf-8")
        os.symlink(target, self.claim)
        with self.assertRaises(SafetyError):
            read_handoff_claim(self.claim)
        with self.assertRaises(SafetyError):
            consume_handoff_claim(self.claim)
        self.assertFalse(self.claim.is_symlink())
        self.assertTrue(target.exists())

    def test_a_directory_at_the_claim_path_is_refused_and_removed_without_raising_os_errors(self):
        self.claim.mkdir()
        with self.assertRaises(SafetyError):
            consume_handoff_claim(self.claim)
        self.assertFalse(self.claim.exists())
        self.claim.mkdir()
        (self.claim / "inside").write_text("x", encoding="utf-8")
        self.assertFalse(discard_handoff_claim(self.claim))

    def test_discard_reports_whether_a_claim_was_left(self):
        self.assertFalse(discard_handoff_claim(self.claim))
        self.write({"title": "CN-1", "brief": "b"})
        self.assertTrue(discard_handoff_claim(self.claim))

    def test_the_workspace_names_the_claim(self):
        workspace = prepare_thread_workspace(Path(self.tmp.name), channel="C1", thread="1.0")
        self.assertEqual(HANDOFF_CLAIM_NAME, workspace.handoff_claim_file.name)
        self.assertEqual(workspace.path, workspace.handoff_claim_file.parent)


class RefusalAndTextTest(unittest.TestCase):
    def test_each_condition_refuses(self):
        self.assertIsNone(handoff_refusal(review=False, from_handoff=False))
        for condition in ("forwarded", "stopped", "delegation_blocked"):
            with self.subTest(condition=condition):
                self.assertIsNotNone(handoff_refusal(review=False, from_handoff=False, **{condition: True}))
        self.assertIn("review", handoff_refusal(review=True, from_handoff=False))
        self.assertIn("handoff", handoff_refusal(review=False, from_handoff=True))
        self.assertIn("more than one person", handoff_refusal(review=False, from_handoff=False, senders=2))

    def test_posted_text_cannot_mention_link_or_broadcast(self):
        request = HandoffRequest("CN-1 <!here>", "ping <!channel> and <@U123> & <https://x|y>")
        posted = new_thread_text(request, source_link="https://slack/src")
        for raw in ("<!here>", "<!channel>", "<@U123>", "<https://x|y>"):
            self.assertNotIn(raw, posted)
        self.assertIn("&lt;!channel&gt;", posted)
        self.assertIn("&amp;", posted)
        self.assertNotIn("<!here>", source_thread_text(request, new_link="https://slack/new"))

    def test_the_turn_message_says_the_brief_is_the_agents_own(self):
        message = thread_handoff.handoff_turn_message(HandoffRequest("CN-1", "brief"))
        self.assertIn("not the requester's words", message)


class AuthorityTest(unittest.TestCase):
    def setUp(self):
        self.authority = AuthorityPolicy(authorized_users=frozenset({"U1"}), approvers=frozenset({"U1"}))

    def envelope(self, **kwargs):
        text = build_authority_envelope(
            self.authority, sender_id="U1", sender_name="Kirk", channel_id="C1",
            permission_mode="auto", message="brief", **kwargs,
        )
        return json.loads(text.splitlines()[1])

    def test_a_handoff_turn_is_never_an_approver(self):
        payload = self.envelope(handoff_from="1.0")
        self.assertFalse(payload["current_sender"]["named_approver"])
        self.assertEqual("authorized_operator", payload["current_sender"]["authority"])
        self.assertEqual("1.0", payload["handoff_from_thread"])

    def test_an_ordinary_turn_is_unchanged(self):
        payload = self.envelope()
        self.assertTrue(payload["current_sender"]["named_approver"])
        self.assertNotIn("handoff_from_thread", payload)

    def test_a_handoff_still_needs_an_authorized_requester(self):
        with self.assertRaises(SafetyError):
            build_authority_envelope(
                self.authority, sender_id="U2", sender_name="X", channel_id="C1",
                permission_mode="auto", message="brief", handoff_from="1.0",
            )

    def test_the_prompt_names_the_claim_the_audience_and_the_limits(self):
        prompt = handoff_prompt(Path("/w/handoff-claim.json"))
        self.assertIn("/w/handoff-claim.json", prompt)
        self.assertIn("visible to everyone in this room", prompt)
        self.assertIn("never satisfies an", prompt)
        self.assertIn("cannot hand off again", prompt)


class FakeSlack:
    def __init__(self, fail=()):
        self.posts = []
        self.fail = set(fail)

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack/{channel}/{message_ts}"}

    def chat_postMessage(self, channel, text, thread_ts=None):
        kind = "reply" if thread_ts else "top"
        if kind in self.fail:
            raise ConnectionError("slack refused")
        self.posts.append((thread_ts, text))
        return {"ts": "9.0"}


class RecordingLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class TakeHandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = prepare_thread_workspace(Path(self.tmp.name), channel="C1", thread="1.0")
        self.slack = FakeSlack()
        self.audit = RecordingLog()
        audit_logger = logging.getLogger("test-take-handoff")
        audit_logger.addHandler(self.audit)
        audit_logger.propagate = False
        audit_logger.setLevel(logging.INFO)
        namespace = _compile(("_delegation_blocked", "_take_handoff"), {
            "ThreadWorkspace": ThreadWorkspace,
            "HandoffRequest": HandoffRequest,
            "SafetyError": SafetyError,
            "DelegationError": DelegationError,
            "budget_status": budget_status,
            "delegation_verification_status": delegation_verification_status,
            "consume_handoff_claim": consume_handoff_claim,
            "handoff_refusal": handoff_refusal,
            "audit_logger": audit_logger,
            "logger": logging.getLogger("test-take-handoff-log"),
            "slack_client": self.slack,
        })
        self.take = namespace["_take_handoff"]

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self):
        self.workspace.handoff_claim_file.write_text(json.dumps({"title": "CN-2 next", "brief": "b"}), encoding="utf-8")

    def run_take(self, **conditions):
        args = {"review": False, "from_handoff": False, **conditions}
        return self.take(self.workspace, user_id="U1", channel="C1", thread_ts="1.0", **args)

    def test_no_claim_is_none_and_silent(self):
        self.assertIsNone(self.run_take())
        self.assertEqual([], self.slack.posts)

    def test_a_clean_turn_returns_the_request(self):
        self.claim()
        self.assertEqual(HandoffRequest("CN-2 next", "b"), self.run_take(senders=1))
        self.assertFalse(self.workspace.handoff_claim_file.exists())

    def test_each_refusal_posts_audits_and_consumes(self):
        for conditions in ({"stopped": True}, {"forwarded": True}, {"senders": 2}, {"from_handoff": True}, {"review": True}):
            with self.subTest(conditions=conditions):
                self.slack.posts.clear()
                self.audit.lines.clear()
                self.claim()
                self.assertIsNone(self.run_take(**conditions))
                ((thread, text),) = self.slack.posts
                self.assertEqual("1.0", thread)
                self.assertTrue(text.startswith("Handoff refused:"))
                self.assertTrue(self.audit.lines[0].startswith("HANDOFF_REFUSED"))
                self.assertFalse(self.workspace.handoff_claim_file.exists())

    def test_an_exhausted_delegation_budget_refuses(self):
        self.workspace.delegation_budget_file.write_text(
            json.dumps({"limit": 1000, "used": 1000, "unit": BUDGET_UNIT}), encoding="utf-8"
        )
        self.claim()
        self.assertIsNone(self.run_take())
        self.assertIn("delegation budget", self.slack.posts[0][1])

    def test_a_directory_claim_is_refused_not_raised(self):
        self.workspace.handoff_claim_file.mkdir()
        self.assertIsNone(self.run_take())
        self.assertTrue(self.slack.posts[0][1].startswith("Handoff refused:"))

    def test_a_refusal_post_failure_does_not_raise(self):
        self.slack.fail.add("reply")
        self.claim()
        self.assertIsNone(self.run_take(stopped=True))


class FakeThread:
    started = []

    def __init__(self, target, args, kwargs, daemon):
        self.call = (target, args, kwargs)

    def start(self):
        FakeThread.started.append(self.call)


class StartHandoffTest(unittest.TestCase):
    def setUp(self):
        FakeThread.started = []
        self.slack = FakeSlack()
        self.audit = RecordingLog()
        audit_logger = logging.getLogger("test-start-handoff")
        audit_logger.addHandler(self.audit)
        audit_logger.propagate = False
        audit_logger.setLevel(logging.INFO)
        namespace = _compile(("_thread_link", "_start_handoff"), {
            "HandoffRequest": HandoffRequest,
            "audit_logger": audit_logger,
            "logger": logging.getLogger("test-start-handoff-log"),
            "slack_client": self.slack,
            "threading": type("threading", (), {"Thread": FakeThread}),
            "process_message_async": "process_message_async",
            "new_thread_text": new_thread_text,
            "source_thread_text": source_thread_text,
            "handoff_turn_message": thread_handoff.handoff_turn_message,
        })
        self.start = lambda: namespace["_start_handoff"](
            HandoffRequest("CN-2 next", "the brief"), user_id="U1", channel="C1", channel_type="channel",
            source_thread="1.0",
        )

    def test_opens_starts_then_links(self):
        self.start()
        (new_parent, new_text), (source_thread, source_text) = self.slack.posts
        self.assertIsNone(new_parent)
        self.assertIn("https://slack/C1/1.0", new_text)
        self.assertEqual("1.0", source_thread)
        self.assertIn("https://slack/C1/9.0", source_text)
        ((target, (event,), kwargs),) = FakeThread.started
        self.assertEqual({"handoff_from": "1.0"}, kwargs)
        self.assertEqual(("U1", "C1", "9.0", "channel"), (event["user"], event["channel"], event["ts"], event["channel_type"]))
        self.assertTrue(self.audit.lines[0].startswith("THREAD_HANDOFF"))

    def test_a_refused_new_thread_is_audited_and_reported(self):
        self.slack.fail.add("top")
        self.start()
        self.assertEqual([], FakeThread.started)
        self.assertTrue(self.audit.lines[0].startswith("HANDOFF_FAILED"))
        ((thread, text),) = self.slack.posts
        self.assertEqual("1.0", thread)
        self.assertTrue(text.startswith("Handoff failed"))

    def test_a_failed_link_still_starts_the_new_turn(self):
        self.slack.fail.add("reply")
        self.start()
        self.assertEqual(1, len(FakeThread.started))
        self.assertTrue(self.audit.lines[0].startswith("THREAD_HANDOFF"))


class FakeReplies:
    def __init__(self, text=None, fail=False):
        self.text, self.fail = text, fail

    def conversations_replies(self, channel, ts, limit):
        if self.fail:
            raise ConnectionError("slack refused")
        return {"messages": [] if self.text is None else [{"text": self.text}]}


class ThreadTicketKeysTest(unittest.TestCase):
    def keys(self, slack):
        namespace = _compile(("_thread_ticket_keys",), {
            "slack_client": slack, "ticket_keys": ticket_keys,
            "logger": logging.getLogger("test-ticket-keys"),
        })
        return namespace["_thread_ticket_keys"]("C1", "1.0")

    def test_reads_the_opening_message_and_fails_open(self):
        self.assertEqual(frozenset({"CN-9532"}), self.keys(FakeReplies("*CN-9532 — confirm endpoint*")))
        self.assertEqual(frozenset(), self.keys(FakeReplies(None)))
        self.assertEqual(frozenset(), self.keys(FakeReplies(fail=True)))


class HandoffBundleTest(unittest.TestCase):
    def setUp(self):
        FakeThread.started = []
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        bundle = self.root / "worktrees" / "CN-2-next"
        bundle.mkdir(parents=True)
        (bundle / "TASK.md").write_text("task\n")
        self.source = prepare_thread_workspace(self.root, channel="C1", thread="1.0")
        self.source.bundle_claim_file.write_text("CN-2-next")
        consume_bundle_claim(self.source, max_live_bundles=3)
        self.slack = FakeSlack()
        self.audit = RecordingLog()
        audit_logger = logging.getLogger("test-handoff-bundle")
        audit_logger.addHandler(self.audit)
        audit_logger.propagate = False
        audit_logger.setLevel(logging.INFO)
        self.namespace = _compile(("_delegation_blocked", "_take_handoff", "_thread_link", "_move_handoff_bundle", "_start_handoff"), {
            "ThreadWorkspace": ThreadWorkspace,
            "HandoffRequest": HandoffRequest,
            "SafetyError": SafetyError,
            "DelegationError": DelegationError,
            "budget_status": budget_status,
            "delegation_verification_status": delegation_verification_status,
            "consume_handoff_claim": consume_handoff_claim,
            "handoff_refusal": handoff_refusal,
            "check_bundle_handoff": check_bundle_handoff,
            "transfer_bundle": transfer_bundle,
            "prepare_thread_workspace": prepare_thread_workspace,
            "RUNTIME_POLICY": type("RuntimePolicy", (), {"max_live_bundles": 3}),
            "audit_logger": audit_logger,
            "logger": logging.getLogger("test-handoff-bundle-log"),
            "slack_client": self.slack,
            "threading": type("threading", (), {"Thread": FakeThread}),
            "process_message_async": "process_message_async",
            "new_thread_text": new_thread_text,
            "source_thread_text": source_thread_text,
            "handoff_turn_message": thread_handoff.handoff_turn_message,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_bundle_moves_to_the_new_thread_before_its_session_starts(self):
        request = HandoffRequest("CN-2 next", "b", "CN-2-next")
        bound_at_start = []
        original_start = FakeThread.start
        FakeThread.start = lambda thread: bound_at_start.append(
            resolve_thread_bundle(prepare_thread_workspace(self.root, channel="C1", thread="9.0"))
        )
        try:
            self.namespace["_start_handoff"](
                request, user_id="U1", channel="C1", channel_type="im", source_thread="1.0",
                source_workspace=self.source,
            )
        finally:
            FakeThread.start = original_start
        self.assertEqual([(self.root / "worktrees" / "CN-2-next").resolve()], bound_at_start)
        self.assertIsNone(resolve_thread_bundle(self.source))
        self.assertTrue(any(line.startswith("BUNDLE_BOUND") and "VIA:handoff" in line for line in self.audit.lines))

    def test_a_claim_naming_a_bundle_bound_elsewhere_is_refused_before_anything_posts(self):
        other = prepare_thread_workspace(self.root, channel="C1", thread="2.0")
        other.handoff_claim_file.write_text(json.dumps({"title": "CN-2 next", "brief": "b", "bundle": "CN-2-next"}))
        taken = self.namespace["_take_handoff"](
            other, user_id="U1", channel="C1", thread_ts="2.0", review=False, from_handoff=False,
        )
        self.assertIsNone(taken)
        ((thread, text),) = self.slack.posts
        self.assertEqual("2.0", thread)
        self.assertIn("already bound to another thread", text)


class WiringTest(unittest.TestCase):
    def test_a_handoff_turn_skips_reply_routes_and_the_relevance_filter_and_marks_its_envelope(self):
        process = ast.unparse(_bot_function("process_message_async"))
        self.assertIn("if not review and (not handoff_from):", process)
        self.assertIn("if not review and (not handoff_from) and needs_relevance_prefix(", process)
        self.assertIn("handoff_from=handoff_from", process)

    def test_the_claim_is_taken_inside_the_turn_and_acted_on_after_it(self):
        process = ast.unparse(_bot_function("process_message_async"))
        taken = [index for index in range(len(process)) if process.startswith("_take_handoff(", index)]
        self.assertEqual(2, len(taken))
        self.assertTrue(all(index < process.index("except _ReviewBusy:") for index in taken))
        self.assertLess(process.index("audit_interaction(event, full_response"), process.index("_start_handoff("))

    def test_steering_records_the_sender_and_never_steers_a_handoff(self):
        process = ast.unparse(_bot_function("process_message_async"))
        steer = process.index("if session and session.turn_lock.locked():\n            if handoff_from:")
        self.assertLess(steer, process.index("session.turn_senders.add(user_id)"))
        self.assertIn("session.turn_senders = {user_id}", process)

    def test_stop_marks_the_thread_and_codex_drops_an_unclean_turns_claim(self):
        stop = ast.unparse(_bot_function("_maybe_stop_from_message"))
        self.assertIn("_handoff_stops.add(thread_ts)", stop)
        fallback = ast.unparse(_bot_function("_run_openai_fallback"))
        self.assertIn("if stopped or result.error or attempted or (verification_status is not None):", fallback)
        self.assertEqual(2, fallback.count("discard_handoff_claim(workspace.handoff_claim_file)"))


if __name__ == "__main__":
    unittest.main()
