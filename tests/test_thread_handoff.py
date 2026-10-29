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
    AuthorityPolicy,
    SafetyError,
    build_authority_envelope,
    handoff_prompt,
    prepare_thread_workspace,
)
from thread_handoff import (
    HandoffRequest,
    consume_handoff_claim,
    discard_handoff_claim,
    handoff_refusal,
    read_handoff_claim,
)

BOT = Path(__file__).parents[1] / "bot.py"


def _bot_function(name):
    tree = ast.parse(BOT.read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


class ClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claim = Path(self.tmp.name) / HANDOFF_CLAIM_NAME

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        self.claim.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")

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
            {"title": "x" * 121, "brief": "b"},
            {"title": "CN-1", "brief": " "},
            {"title": "CN-1", "brief": "b" * 6001},
            {"title": 1, "brief": "b"},
        ):
            with self.subTest(bad=bad):
                self.write(bad)
                with self.assertRaises(SafetyError):
                    consume_handoff_claim(self.claim)
                self.assertFalse(self.claim.exists())

    def test_a_symlinked_claim_is_refused(self):
        target = Path(self.tmp.name) / "elsewhere.json"
        target.write_text(json.dumps({"title": "CN-1", "brief": "b"}), encoding="utf-8")
        os.symlink(target, self.claim)
        with self.assertRaises(SafetyError):
            read_handoff_claim(self.claim)
        with self.assertRaises(SafetyError):
            consume_handoff_claim(self.claim)
        self.assertFalse(self.claim.is_symlink())
        self.assertTrue(target.exists())

    def test_discard_reports_whether_a_claim_was_left(self):
        self.assertFalse(discard_handoff_claim(self.claim))
        self.write({"title": "CN-1", "brief": "b"})
        self.assertTrue(discard_handoff_claim(self.claim))

    def test_review_and_handoff_turns_cannot_hand_off(self):
        self.assertIsNone(handoff_refusal(review=False, from_handoff=False))
        self.assertIn("review", handoff_refusal(review=True, from_handoff=False))
        self.assertIn("handoff", handoff_refusal(review=False, from_handoff=True))

    def test_the_workspace_names_the_claim(self):
        workspace = prepare_thread_workspace(Path(self.tmp.name), channel="C1", thread="1.0")
        self.assertEqual(HANDOFF_CLAIM_NAME, workspace.handoff_claim_file.name)
        self.assertEqual(workspace.path, workspace.handoff_claim_file.parent)


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

    def test_the_prompt_names_the_claim_and_the_limits(self):
        prompt = handoff_prompt(Path("/w/handoff-claim.json"))
        self.assertIn("/w/handoff-claim.json", prompt)
        self.assertIn("never satisfies an approval gate", prompt)
        self.assertIn("cannot hand off again", prompt)


class FakeThread:
    started = []

    def __init__(self, target, args, kwargs, daemon):
        self.call = (target, args, kwargs)

    def start(self):
        FakeThread.started.append(self.call)


class FakeSlack:
    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack/{channel}/{message_ts}"}


class FinishHandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.posts = []
        FakeThread.started = []

        def post_response(channel, text, thread_ts=None):
            self.posts.append((thread_ts, text))
            return None if thread_ts else "9.0"

        namespace = {
            "Path": Path,
            "SafetyError": SafetyError,
            "HANDOFF_CLAIM_NAME": HANDOFF_CLAIM_NAME,
            "HandoffRequest": HandoffRequest,
            "validate_existing_thread_workspace": lambda root, channel, thread: self.workspace,
            "audit_logger": logging.getLogger("test-handoff-audit"),
            "post_response": post_response,
            "slack_client": FakeSlack(),
            "threading": type("threading", (), {"Thread": FakeThread}),
            "process_message_async": "process_message_async",
        }
        for name in (
            "consume_handoff_claim", "handoff_refusal", "handoff_turn_message", "new_thread_text",
            "source_thread_text",
        ):
            namespace[name] = getattr(thread_handoff, name)
        functions = [_bot_function(name) for name in ("_finish_handoff", "_thread_link", "_start_handoff")]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(BOT), "exec"), namespace)
        self.finish = namespace["_finish_handoff"]

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self):
        (self.workspace / HANDOFF_CLAIM_NAME).write_text(
            json.dumps({"title": "CN-2 next", "brief": "the brief"}), encoding="utf-8"
        )

    def run_finish(self, **kwargs):
        args = {"review": False, "from_handoff": False, **kwargs}
        self.finish(
            Path("/root"), channel="C1", channel_type="channel", thread_ts="1.0", user_id="U1", **args,
        )

    def test_no_claim_does_nothing(self):
        self.run_finish()
        self.assertEqual([], self.posts)
        self.assertEqual([], FakeThread.started)

    def test_a_claim_opens_links_and_starts_the_new_thread_as_the_requester(self):
        self.claim()
        self.run_finish()
        (new_parent, new_text), (source_thread, source_text) = self.posts
        self.assertIsNone(new_parent)
        self.assertIn("*CN-2 next*", new_text)
        self.assertIn("https://slack/C1/1.0", new_text)
        self.assertEqual("1.0", source_thread)
        self.assertIn("https://slack/C1/9.0", source_text)
        ((target, (event,), kwargs),) = FakeThread.started
        self.assertEqual("process_message_async", target)
        self.assertEqual({"handoff_from": "1.0"}, kwargs)
        self.assertEqual(("U1", "C1", "9.0", "channel"), (event["user"], event["channel"], event["ts"], event["channel_type"]))
        self.assertIn("the brief", event["text"])
        self.assertFalse((self.workspace / HANDOFF_CLAIM_NAME).exists())

    def test_a_handoff_turn_cannot_chain(self):
        self.claim()
        self.run_finish(from_handoff=True)
        ((thread, text),) = self.posts
        self.assertEqual("1.0", thread)
        self.assertTrue(text.startswith("Handoff refused:"))
        self.assertEqual([], FakeThread.started)
        self.assertFalse((self.workspace / HANDOFF_CLAIM_NAME).exists())

    def test_a_review_turn_cannot_hand_off(self):
        self.claim()
        self.run_finish(review=True)
        self.assertEqual([], FakeThread.started)


class WiringTest(unittest.TestCase):
    def test_a_handoff_turn_skips_reply_routes_and_the_relevance_filter_and_marks_its_envelope(self):
        process = ast.unparse(_bot_function("process_message_async"))
        self.assertIn("if not review and (not handoff_from):", process)
        self.assertIn("if not review and (not handoff_from) and needs_relevance_prefix(", process)
        self.assertIn("handoff_from=handoff_from", process)

    def test_the_claim_is_acted_on_only_after_a_completed_turn(self):
        process = ast.unparse(_bot_function("process_message_async"))
        self.assertLess(process.index("audit_interaction(event, full_response"), process.index("_finish_handoff("))
        self.assertLess(process.index("if skip_detected:"), process.index("_finish_handoff("))

    def test_a_claim_left_by_an_unfinished_turn_is_discarded_at_the_next_turn(self):
        process = ast.unparse(_bot_function("process_message_async"))
        self.assertLess(
            process.index("discard_handoff_claim(session.workspace.handoff_claim_file)"),
            process.index("if not wait_for_turn_completion("),
        )
        fallback = ast.unparse(_bot_function("_run_openai_fallback"))
        self.assertIn("workspace.handoff_claim_file", fallback.split("cleanup_stale_delegate_pid")[0])

    def test_stop_cancels_a_pending_handoff(self):
        stop = ast.unparse(_bot_function("_maybe_stop_from_message"))
        self.assertIn("discard_handoff_claim(workspace.handoff_claim_file)", stop)


if __name__ == "__main__":
    unittest.main()
