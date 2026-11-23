"""Behavioural tests of the review path in bot.py, compiled against stub globals.

bot.py cannot be imported in tests (it needs Slack credentials and an external env file at
import time), so the functions under test are compiled from its source, as in
test_reader_resilience.py. Every module global they read is stubbed.
"""

import ast
import contextlib
import builtins
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

import cargo_chief_safety
from cargo_chief_safety import (
    AuthorityPolicy,
    RoomPolicy,
    SafetyError,
    TOKEN_PERMISSIONS_ENV,
    build_authority_envelope,
    build_claude_command,
    claude_owner_env,
    github_wrapper_downscopes,
    prepare_thread_workspace,
    review_turn_env,
)
from review_requests import (
    BUSY_REPLY,
    CAP_REPLY,
    CONFIG_REPLY,
    ERROR_REPLY,
    WRAPPER_REPLY,
    ReviewAdmission,
    ReviewCapStore,
    ReviewPlan,
    build_review_task,
)

PLAN = ReviewPlan("incremental", 2, 3, "a" * 40)

BOT = Path(__file__).parents[1] / "bot.py"
FUNCTIONS = (
    "process_message_async", "_spawn_claude_process", "_get_or_create_live_session", "_clear_eyes",
    "_review_still_runnable", "_notice_idle_text", "_start_review_notice", "_codex_notice_user", "_codex_resumes_as",
    "_save_openai_session", "_load_openai_sessions", "_get_openai_session_record", "_take_thread_turn",
)
CLASSES = ("_ReviewBusy", "_NoLiveSession", "_CodexReviewDone", "_SessionsFull", "_RerouteAfterTurn")
PR = "https://github.com/Cargo-Chief/c4/pull/123"
ENV = {"AUTHORIZED_USERS": "UKIRK", "CARGO_CHIEF_APPROVERS": "UKIRK"}


def _globals_used(nodes):
    used = set()
    for function in nodes:
        local = {arg.arg for arg in ast.walk(function) if isinstance(arg, ast.arg)}
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                local.add(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                local.add(node.name)
        for node in ast.walk(function):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id not in local and not hasattr(builtins, node.id)):
                used.add(node.id)
    return used


def load(**overrides):
    tree = ast.parse(BOT.read_text(encoding="utf-8"))
    wanted = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS)
        or (isinstance(node, ast.ClassDef) and node.name in CLASSES)
    ]
    namespace = {name: mock.MagicMock(name=name) for name in _globals_used(wanted)}
    namespace.update({
        "json": json, "os": os, "re": re, "sys": sys, "time": time, "threading": threading,
        "tempfile": tempfile, "subprocess": subprocess, "Path": Path,
        "AuthorityPolicy": AuthorityPolicy, "RoomPolicy": RoomPolicy, "SafetyError": SafetyError,
        "ReviewAdmission": ReviewAdmission, "ThreadWorkspace": object, "LiveSession": object,
        "build_authority_envelope": build_authority_envelope,
        "build_review_task": build_review_task, "build_claude_command": build_claude_command,
        "claude_owner_env": claude_owner_env, "review_turn_env": review_turn_env,
        "github_wrapper_downscopes": github_wrapper_downscopes,
        "BUSY_REPLY": BUSY_REPLY, "CONFIG_REPLY": CONFIG_REPLY, "ERROR_REPLY": ERROR_REPLY,
        "WRAPPER_REPLY": WRAPPER_REPLY, "PROVIDER_REPLY": "paused",
        "BOT_USER_ID": "ULISA", "MAX_TURN_RUNTIME": 60, "CLAUDE_TIMEOUT": 60, "REVIEW_MAX_RUNTIME": 45,
        "STOP_WAIT_SECONDS": 1, "MAX_LIVE_SESSIONS": 2,
        "logger": logging.getLogger("test-review-turn"),
        "audit_logger": logging.getLogger("test-review-turn-audit"),
        "_live_sessions": {}, "_live_sessions_lock": threading.Lock(),
        "_thread_msg_counts": {},
        "_get_user_name": lambda user: "Display Name",
        "_get_provider_override": lambda thread: None,
        "_limit_paused": lambda: False,
        "_get_openai_session": lambda thread: None,
        "_get_session": lambda thread: "saved-session",
        "_review_turn_running": lambda thread: False,
        "use_openai_provider": lambda *args, **kwargs: False,
        "_review_provider": lambda thread: "claude",
        "_codex_review_ready": lambda: True,
        "contextlib": contextlib,
        "_thread_turn_running": lambda thread: False,
        "_thread_uses_openai": lambda thread: False,
        "_openai_turn_lock": lambda thread: threading.Lock(),
    })
    namespace.update(overrides)
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(BOT), "exec"), namespace)
    return namespace


class Room:
    """A temporary workspace with an overlay and an App wrapper that may or may not downscope."""

    def __init__(self, *, review_only=True, wrapper_downscopes=True):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        overlay = self.root / "agent-kit" / "modes" / "review-only.md"
        overlay.parent.mkdir(parents=True)
        overlay.write_text("review only\n")
        wrapper = self.root / "docs" / "scripts" / "ops" / "with_github_app_token.py"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('PERMISSIONS_ENV = "CARGO_CHIEF_GITHUB_TOKEN_PERMISSIONS"\\ndef requested_permissions(env=None):\\n    pass\\nMESSAGE = "GitHub granted more than the requested permissions"\\n' if wrapper_downscopes else "print()\n")
        self.policy = RoomPolicy(
            root=self.root, permission_mode="auto", overlay=overlay, escalation_channel="DAPPROVER",
            backend="claude", model="claude-opus-5-5", effort="high", role="eng",
            fallback_backend="codex", fallback_model="gpt-6-sol", fallback_effort="high",
            review_only=review_only,
        )

    def close(self):
        self.temp.cleanup()


class SpawnTest(unittest.TestCase):
    def setUp(self):
        self.room = Room()
        self.addCleanup(self.room.close)
        self.popen = []

        def popen(cmd, **kwargs):
            self.popen.append((cmd, kwargs))
            return mock.MagicMock(pid=1)

        self.ns = load(
            _get_trust_battery_context=lambda: "",
            resolve_model_settings=lambda channel, user: ("", "high", ""),
            resolve_room_policy=lambda config, channel, user: self.room.policy,
            preflight_room=lambda policy: [],
            prepare_thread_workspace=lambda root, **kwargs: prepare_thread_workspace(
                self.room.root, **kwargs
            ),
            resolve_thread_bundle=lambda workspace: self.room.root / "bundle",
            _identity_prompt=lambda: "",
            SOURCE_DIR=Path("/home-base"),
            TEMP_DIR=self.room.root,
            subprocess=mock.MagicMock(Popen=popen, PIPE=subprocess.PIPE),
        )

    def spawn(self, **kwargs):
        env = {
            **ENV, "PATH": "/usr/bin", "AWS_PROFILE": "kirk", "SLACK_BOT_TOKEN": "x",
            "SLACK_SIGNING_SECRET": "x", "CLAUDE_CODE_OAUTH_TOKEN": "keep",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            return self.ns["_spawn_claude_process"](
                session_id="saved", user_id="USTRANGER", thread_ts="1.1", channel="CREVIEWS", **kwargs
            )

    def test_review_spawn_is_fresh_downscoped_and_has_no_routes(self):
        self.spawn(review_policy=self.room.policy)
        cmd, kwargs = self.popen[0]
        child = kwargs["env"]
        self.assertNotIn("--resume", cmd)
        self.assertNotIn("CLAUDE_SESSION_ID", child)
        for key in (
            "CARGO_CHIEF_ESCALATION_CHANNEL", "CARGO_CHIEF_ESCALATION_MESSAGE_FILE",
            "CARGO_CHIEF_ESCALATION_RECEIPT_FILE", "CARGO_CHIEF_ESCALATION_ATTEMPT_FILE",
            "CARGO_CHIEF_PARKING_CLAIM_FILE", "CARGO_CHIEF_BUNDLE_CLAIM_FILE", "CARGO_CHIEF_MAX_LIVE_BUNDLES",
            "CARGO_CHIEF_THREAD_BUNDLE_DIR", "AWS_PROFILE", "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET",
        ):
            self.assertNotIn(key, child, key)
        self.assertEqual("1", child["CARGO_CHIEF_REVIEW_ONLY"])
        self.assertEqual("", child["CARGO_CHIEF_REVIEW_PR"])
        self.assertEqual("", child["CARGO_CHIEF_REVIEW_MODE"])
        self.assertIn(TOKEN_PERMISSIONS_ENV, child)
        self.assertEqual("keep", child["CLAUDE_CODE_OAUTH_TOKEN"])
        self.assertNotIn("bundle", str(kwargs["cwd"]))
        prompt = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertNotIn("--escalate", prompt)
        self.assertNotIn("--check-parking", prompt)
        self.assertNotIn("CN-####", prompt)

    def test_review_only_room_refuses_an_unpinned_spawn(self):
        with self.assertRaisesRegex(SafetyError, "admitted review turns only"):
            self.spawn()
        self.assertEqual([], self.popen)

    def test_pinned_policy_must_be_review_only(self):
        normal = Room(review_only=False)
        self.addCleanup(normal.close)
        with self.assertRaisesRegex(SafetyError, "review-only room policy"):
            self.spawn(review_policy=normal.policy)

    def test_outdated_wrapper_refuses_the_spawn(self):
        stale = Room(wrapper_downscopes=False)
        self.addCleanup(stale.close)
        with self.assertRaisesRegex(SafetyError, "out of date"):
            self.spawn(review_policy=stale.policy)
        self.assertEqual([], self.popen)

    def test_normal_room_keeps_its_routes_and_resume(self):
        normal = Room(review_only=False)
        self.addCleanup(normal.close)
        self.room = normal
        self.spawn()
        cmd, kwargs = self.popen[0]
        self.assertEqual("saved", cmd[cmd.index("--resume") + 1])
        self.assertIn("CARGO_CHIEF_ESCALATION_CHANNEL", kwargs["env"])
        self.assertEqual("kirk", kwargs["env"]["AWS_PROFILE"])
        for key in ("CARGO_CHIEF_ROOT", "CARGO_CHIEF_THREAD_WORK_DIR", "CARGO_CHIEF_BUNDLE_CLAIM_FILE"):
            self.assertIn(key, kwargs["env"], key)
        self.assertIn("CARGO_CHIEF_MAX_LIVE_BUNDLES", kwargs["env"])


class FakeSession:
    def __init__(self, *, locked=False):
        self.proc = mock.MagicMock(pid=7)
        self.proc.poll.return_value = None
        self.turn_lock = threading.Lock()
        if locked:
            self.turn_lock.acquire()
        self.stop_done = None
        self.reporter = None
        self.workspace = None
        self.session_id = None
        self.pending_reactions = []
        self.pending_tools = mock.MagicMock()
        self.review_done = False
        self.limit_notices = set()
        self._turn_done = threading.Event()
        self.thread_ts = "1.1"


class ProcessReviewTest(unittest.TestCase):
    def setUp(self):
        self.room = Room()
        self.addCleanup(self.room.close)
        self.posts = []
        self.audits = []
        self.sent = []
        self.closed = []
        self.created = []
        self.unauthorized = []
        self.created_prs = []
        self.created_plans = []
        self.caps = ReviewCapStore(self.room.root / "caps.json")
        self.resolved = self.room.policy
        self.session = FakeSession()

        def create(thread_ts, channel, user_id="", review_policy=None, review_pr="", review_plan=None,
                   existing_only=False, evict_waiting=False):
            self.created_plans.append(review_plan)
            self.created.append(review_policy)
            self.created_prs.append(review_pr)
            return self.session

        self.ns = load(
            resolve_room_policy=lambda config, channel, user: self.resolved,
            _send_to_claude=lambda session, text: self.sent.append(text),
            _close_review_session=self.closed.append,
            log_unauthorized=self.unauthorized.append,
            wait_for_turn_completion=lambda *args, **kwargs: True,
        )
        self.ns["_get_or_create_live_session"] = create

    def admission(self, limit=3):
        return ReviewAdmission(
            event={"user": "USTRANGER", "channel": "CREVIEWS", "ts": "1.1", "text": f"review {PR}"},
            pr=PR, policy=self.room.policy, limit=limit, caps=self.caps,
            post=lambda channel, thread, text: self.posts.append(text), audit=self.audits.append,
            plan=PLAN,
        )

    def run_turn(self, admission=None, event=None):
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.ns["process_message_async"](
                event or admission.event, review_admission=admission,
            )

    def outcomes(self):
        return [line.rsplit("OUTCOME:", 1)[1] for line in self.audits]

    def test_admitted_review_runs_the_harness_task_under_the_pinned_policy(self):
        self.run_turn(self.admission())
        self.assertEqual([self.room.policy], self.created)
        self.assertEqual(1, len(self.sent))
        task, envelope = self.sent[0].split("\n\n", 1)
        self.assertEqual(build_review_task(PR, self.room.policy.root, PLAN), task)
        self.assertEqual([PLAN], self.created_plans)
        self.assertEqual([PR], self.created_prs)
        payload = json.loads(envelope.splitlines()[1])
        self.assertEqual("review_requester", payload["current_sender"]["authority"])
        self.assertEqual("review requester", payload["current_sender"]["display_name"])
        self.assertEqual("Display Name", payload["untrusted_sender_display_name"])
        self.assertEqual(["accepted"], self.outcomes())
        self.assertEqual([self.session], self.closed)

    def test_a_review_holds_the_review_slot_for_its_turn_and_frees_it_on_failure(self):
        calls = []
        slots = mock.MagicMock()
        slots.acquire.side_effect = lambda *args: calls.append("acquire")
        slots.release.side_effect = lambda: calls.append("release")
        self.ns["_review_slots"] = slots
        self.run_turn(self.admission())
        self.assertEqual(["acquire", "release"], calls)
        calls.clear()
        self.ns["_send_to_claude"] = mock.MagicMock(side_effect=RuntimeError("boom"))
        self.run_turn(self.admission())
        self.assertEqual(["acquire", "release"], calls)

    def test_the_recheck_after_waiting_refuses_what_changed(self):
        check = self.ns["_review_still_runnable"]
        cases = [
            ("refused_state", lambda: False, mock.MagicMock(return_value=mock.MagicMock(state="MERGED")), False),
            ("refused_lookup", lambda: False, mock.MagicMock(side_effect=RuntimeError("gh")), False),
            (None, lambda: False, mock.MagicMock(return_value=mock.MagicMock(state="OPEN")), True),
        ]
        for outcome, paused, lookup, ok in cases:
            with self.subTest(outcome=outcome):
                self.audits.clear()
                self.ns["use_openai_provider"] = lambda *args, **kwargs: paused()
                self.ns["_lookup_review_pr"] = lookup
                admission = self.admission()
                self.assertIs(ok, check(admission, self.room.policy, "1.1"))
                expected = [] if outcome is None else ["refused_provider" if outcome == "paused" else outcome]
                self.assertEqual(expected, self.outcomes())

    def test_a_full_review_queue_refuses_aloud_without_starting(self):
        self.ns["_review_slots"] = mock.MagicMock(**{"acquire.return_value": "full"})
        self.ns["REVIEW_QUEUE_FULL_REPLY"] = "too many"
        self.run_turn(self.admission())
        self.assertEqual(["refused_queue"], self.outcomes())
        self.assertEqual([], self.sent)
        self.assertEqual(["too many"], self.posts)

    def test_a_queued_review_whose_pr_closed_meanwhile_is_refused(self):
        self.ns["_review_slots"] = mock.MagicMock(**{"acquire.return_value": "queued"})
        self.ns["_review_still_runnable"] = lambda admission, policy, thread: admission.refuse(
            "refused_state", "not open") or False
        self.run_turn(self.admission())
        self.assertEqual(["refused_state"], self.outcomes())
        self.assertEqual([], self.sent)
        self.ns["_review_slots"].release.assert_called_once_with()

    def test_a_review_turn_is_bounded_by_the_review_limit(self):
        limits = []

        def wait(*args, max_duration, **kwargs):
            limits.append(max_duration)
            return True

        self.ns["wait_for_turn_completion"] = wait
        self.run_turn(self.admission())
        self.assertEqual(1, len(limits))
        self.assertLessEqual(limits[0], 45)

    def test_a_timed_out_review_asks_for_a_rerun_not_a_resume(self):
        posted = []
        self.ns["slack_client"].chat_postMessage.side_effect = lambda **kwargs: posted.append(kwargs["text"])
        self.ns["stop_timed_out_session"] = lambda session, interrupt: "interrupted"
        self.ns["wait_for_turn_completion"] = lambda *args, **kwargs: False
        self.run_turn(self.admission())
        self.assertEqual(1, len(posted))
        self.assertIn("ask again to rerun it", posted[0])
        self.assertNotIn("resume", posted[0])
        self.assertEqual([self.session], self.closed)

    def limited_turn(self, review_done=False):
        def send(session, text):
            session.review_done = review_done
            session.limit_notices.add("LIMIT")
            session._on_text("LIMIT")

        replies = []
        self.ns["_send_to_claude"] = send
        self.ns["post_response"] = lambda channel, text, thread_ts=None: replies.append(text)
        self.ns["_enter_limit_pause"] = lambda text: None
        self.run_turn(self.admission())
        return replies

    def test_a_review_that_hits_the_limit_moves_to_codex(self):
        self.assertEqual([], self.limited_turn())
        moved = self.ns["_continue_review_after_limit"]
        self.assertEqual(1, moved.call_count)
        (admission, policy), turn = moved.call_args
        self.assertEqual(PR, admission.pr)
        self.assertTrue(policy.review_only)
        self.assertEqual("1.1", turn["thread_ts"])
        self.assertIn("CARGO_CHIEF_AUTHORITY_ENVELOPE", turn["envelope"])
        self.assertEqual([self.session], self.closed)

    def test_a_review_moved_after_a_limit_never_says_it_lost_its_train_of_thought(self):
        posted = []
        self.ns["slack_client"].chat_postMessage.side_effect = lambda **kwargs: posted.append(kwargs["text"])
        self.session.proc.poll.return_value = 0
        self.ns["TurnReporter"] = lambda **kwargs: mock.MagicMock(noticed_no_reply=False, active=False)
        self.limited_turn()
        self.assertFalse(any("train of thought" in text for text in posted))

    def test_a_review_that_already_replied_is_not_rerun(self):
        self.assertEqual([], self.limited_turn(review_done=True))
        self.ns["_continue_review_after_limit"].assert_not_called()

    def test_a_limited_review_runs_on_codex_without_a_claude_session(self):
        self.ns["_review_provider"] = lambda thread: "codex"
        runs = []

        def run(admission, policy, **turn):
            runs.append(admission.started)
            turn["on_text"]("summary")
            return "done"

        self.ns["_run_codex_review"] = run
        self.run_turn(self.admission())
        self.assertEqual([True], runs, "the cap is charged before the Codex turn runs")
        self.assertEqual(["accepted"], self.outcomes())
        self.assertEqual([], self.sent, "nothing is sent to a Claude process")
        self.ns["_defer_review"].assert_not_called()

    def test_a_review_codex_cannot_run_is_deferred_before_its_cap_is_charged(self):
        self.ns["_review_provider"] = lambda thread: "codex"
        self.ns["_codex_review_ready"] = lambda: False
        self.ns["_run_codex_review"] = mock.MagicMock()
        admission = self.admission()
        self.run_turn(admission)
        self.ns["_defer_review"].assert_called_once_with(admission)
        self.assertFalse(admission.started)
        self.ns["_run_codex_review"].assert_not_called()

    def test_a_codex_review_that_cannot_run_is_deferred(self):
        self.ns["_review_provider"] = lambda thread: "codex"
        self.ns["_run_codex_review"] = lambda admission, policy, **turn: "unavailable"
        admission = self.admission()
        self.run_turn(admission)
        self.ns["_defer_review"].assert_called_once_with(admission)
        self.assertEqual([], self.sent)

    def test_a_notice_is_consumed_and_cleared_so_a_later_echo_is_plain_text(self):
        def send(session, text):
            self.assertEqual(set(), session.limit_notices, "a new turn starts with no recorded notices")
            session.limit_notices.add("LIMIT")
            session._on_text("LIMIT")
            session._on_text("LIMIT")

        replies = []
        paused = []
        self.session.limit_notices.add("LIMIT")
        self.ns["_send_to_claude"] = send
        self.ns["post_response"] = lambda channel, text, thread_ts=None: replies.append(text)
        self.ns["_enter_limit_pause"] = paused.append
        self.run_turn(self.admission())
        self.assertEqual(["LIMIT"], paused)
        self.assertEqual(["LIMIT"], replies)

    def test_a_quoted_limit_string_is_not_treated_as_the_notice(self):
        def send(session, text):
            session._on_text("LIMIT")

        replies = []
        self.ns["_send_to_claude"] = send
        self.ns["post_response"] = lambda channel, text, thread_ts=None: replies.append(text)
        self.ns["_enter_limit_pause"] = lambda text: self.fail("model text must not pause the account")
        self.run_turn(self.admission())
        self.assertEqual(["LIMIT"], replies)

    def test_a_finished_review_that_times_out_posts_no_timeout_note(self):
        posted = []
        self.session.review_done = True
        self.ns["slack_client"].chat_postMessage.side_effect = lambda **kwargs: posted.append(kwargs["text"])
        self.ns["stop_timed_out_session"] = lambda session, interrupt: "review stopped"
        self.ns["wait_for_turn_completion"] = lambda *args, **kwargs: False
        self.run_turn(self.admission())
        self.assertEqual([], posted)
        self.assertEqual([self.session], self.closed)

    def test_unauthorized_sender_is_still_refused_in_a_normal_room(self):
        self.resolved = Room(review_only=False).policy
        event = {"user": "USTRANGER", "channel": "C1", "ts": "1.1", "text": "do work"}
        self.run_turn(event=event)
        self.assertEqual([event], self.unauthorized)
        self.assertEqual([], self.created)

    def test_plain_turn_in_a_review_only_room_is_refused(self):
        event = {"user": "UKIRK", "channel": "CREVIEWS", "ts": "1.1", "text": "do work"}
        self.run_turn(event=event)
        self.assertEqual([event], self.unauthorized)
        self.assertEqual([], self.created)

    def test_saved_route_into_a_review_only_room_is_closed_aloud(self):
        posted = []
        self.ns["_reply_routes"].claim.return_value = mock.MagicMock(
            status="routed", route={"thread": "1.1", "channel": "CREVIEWS"},
        )
        self.ns["slack_client"].chat_postMessage.side_effect = lambda **kwargs: posted.append(kwargs)
        event = {"user": "UKIRK", "channel": "DAPPROVER", "ts": "5.5", "thread_ts": "5.0", "text": "yes"}
        self.run_turn(event=event)
        self.assertEqual(
            [{"channel": "DAPPROVER", "thread_ts": "5.0", "text": "This private decision is already closed."}],
            posted,
        )
        self.assertEqual([], self.created)

    def test_room_flipped_after_admission_refuses_with_a_reply(self):
        normal = Room(review_only=False)
        self.addCleanup(normal.close)
        self.resolved = normal.policy
        self.run_turn(self.admission())
        self.assertEqual([CONFIG_REPLY], self.posts)
        self.assertEqual(["refused_room"], self.outcomes())
        self.assertEqual([], self.created)

    def test_running_turn_gets_the_plain_busy_reply(self):
        self.ns["_live_sessions"]["1.1"] = FakeSession(locked=True)
        self.run_turn(self.admission())
        self.assertEqual([BUSY_REPLY], self.posts)
        self.assertEqual(["refused_busy"], self.outcomes())
        self.assertEqual([], self.sent)
        self.assertEqual([], self.closed, "the running process is not the review's to close")

    def test_cap_is_charged_only_when_the_turn_starts(self):
        for _ in range(3):
            self.caps.admit(PR, 3)
        self.run_turn(self.admission())
        self.assertEqual([CAP_REPLY], self.posts)
        self.assertEqual(["refused_cap"], self.outcomes())
        self.assertEqual([], self.sent)
        self.assertEqual([self.session], self.closed)

    def test_spawn_failure_refuses_without_charging_the_cap(self):
        def create(*args, **kwargs):
            raise SafetyError(WRAPPER_REPLY)

        self.ns["_get_or_create_live_session"] = create
        self.run_turn(self.admission(limit=1))
        self.assertEqual([ERROR_REPLY], self.posts)
        self.assertEqual(["refused_error"], self.outcomes())
        self.assertFalse(self.caps.exhausted(PR, 1))


class SessionsFullTest(unittest.TestCase):
    """A new work thread at the cap with nothing evictable is refused, never served by evicting a waiter."""

    def setUp(self):
        self.room = Room(review_only=False)
        self.addCleanup(self.room.close)
        self.slack = mock.MagicMock()
        self.sent = []

        def create(*args, **kwargs):
            raise self.ns["_SessionsFull"]()

        self.ns = load(
            resolve_room_policy=lambda config, channel, user: self.room.policy,
            _send_to_claude=lambda session, text: self.sent.append(text),
            slack_client=self.slack,
            SESSIONS_FULL_REPLY="busy",
        )
        self.ns["_get_or_create_live_session"] = create

    def test_the_new_thread_gets_the_busy_reply_and_no_turn(self):
        event = {"user": "UKIRK", "channel": "DKIRK", "channel_type": "im", "ts": "5.1", "text": "hello"}
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.ns["process_message_async"](event)
        self.assertEqual([], self.sent)
        texts = [call.kwargs.get("text") for call in self.slack.chat_postMessage.call_args_list]
        self.assertEqual(["busy"], texts)



class EvictionTest(unittest.TestCase):
    """The real eviction rule: waiting and waking threads are spared unless a one-shot turn needs the slot."""

    def setUp(self):
        from session_lifecycle import needs_eviction, oldest_evictable_session
        self.closed = []
        self.waiting = set()
        self.ns = load(
            needs_eviction=needs_eviction,
            oldest_evictable_session=oldest_evictable_session,
            close_process_tree=lambda proc, *args, **kwargs: self.closed.append(proc.thread),
            _waiting_threads=lambda: self.waiting,
            LiveSession=lambda **kwargs: mock.MagicMock(**kwargs),
            _reader_loop=lambda session: None,
        )
        self.ns["_spawn_claude_process"] = lambda **kwargs: (mock.MagicMock(), mock.MagicMock())
        for thread, at in (("old", 10), ("new", 20)):
            self.ns["_live_sessions"][thread] = self.session(thread, at)

    def session(self, thread, at):
        proc = mock.MagicMock(thread=thread)
        proc.poll.return_value = None
        return mock.MagicMock(proc=proc, review=False, waking=False, stop_done=None, bundle_moved=False,
                              last_activity=at, turn_lock=threading.Lock(), channel="D1")

    def create(self, **kwargs):
        return self.ns["_get_or_create_live_session"]("fresh", "D1", user_id="UKIRK", **kwargs)

    def test_a_waiting_thread_is_spared_and_the_other_goes(self):
        self.waiting = {"old"}
        self.create()
        self.assertEqual(["new"], self.closed)

    def test_when_every_thread_waits_or_wakes_a_new_thread_is_refused(self):
        self.waiting = {"old"}
        self.ns["_live_sessions"]["new"].waking = True
        with self.assertRaises(self.ns["_SessionsFull"]):
            self.create()
        self.assertEqual([], self.closed)

    def test_a_one_shot_turn_takes_the_oldest_waiter_but_never_a_waking_one(self):
        self.waiting = {"old", "new"}
        self.ns["_live_sessions"]["old"].waking = True
        self.create(evict_waiting=True)
        self.assertEqual(["new"], self.closed)



class NoticeIdleTextTest(unittest.TestCase):
    def test_a_refusal_says_so_and_a_hand_back_keeps_its_note(self):
        ns = load(REVIEW_NOTICE_IDLE="ended {pr}", REVIEW_REFUSED_NOTE="declined {pr}: {reason}")
        self.assertEqual("ended P", ns["_notice_idle_text"]({"pr": "P", "refused": ""}))
        self.assertEqual("declined P: busy Tell me to continue when ready.",
                         ns["_notice_idle_text"]({"pr": "P", "refused": "busy"}))



class StartReviewNoticeTest(unittest.TestCase):
    ENTRY = {"pr": "P", "channel": "D1", "thread": "5.1", "at": 1.0}

    def setUp(self):
        self.slack = mock.MagicMock()
        self.slack.chat_postMessage.return_value = {"ts": "9.9"}
        self.notices = mock.MagicMock()
        self.turns = []
        self.live = None
        self.ns = load(
            slack_client=self.slack, _review_notices=self.notices, _notice_room=lambda channel: True,
            _live_session_for=lambda thread, channel: self.live,
            REVIEW_NOTICE_IDLE="ended {pr}", REVIEW_NOTICE_MESSAGE="read {pr}",
            REVIEW_REFUSED_NOTE="declined {pr}: {reason}", REVIEW_REFUSED_MESSAGE="declined {pr}: {reason}",
        )
        # Compiled from bot.py by load(), so replaced afterwards.
        self.ns["process_message_async"] = lambda event, **kwargs: self.turns.append((event["text"], kwargs))
        self.codex_user = ""
        self.ns["_codex_notice_user"] = lambda thread, channel, requested_at: self.codex_user

    def start(self, **extra):
        self.ns["_start_review_notice"]({**self.ENTRY, **extra}, {"user": "UPEER", "thread_ts": "7.1"})

    def test_a_failed_note_restores_a_hand_back_but_never_a_refusal(self):
        self.slack.chat_postMessage.side_effect = RuntimeError("slack down")
        self.start()
        self.assertEqual(1, self.notices.add.call_count)
        self.assertEqual(1.0, self.notices.add.call_args.kwargs["at"])
        self.notices.add.reset_mock()
        self.start(refused="busy")
        self.notices.add.assert_not_called()

    def test_a_refusal_wakes_the_live_session_with_the_reason_and_clears_waking(self):
        self.live = mock.MagicMock(user_id="UKIRK", waking=False)
        self.start(refused="busy")
        self.assertEqual("declined P: busy", self.turns[0][0])
        self.assertEqual({"pr": "P", "refused": "busy", "room_user": "UKIRK"}, self.turns[0][1]["review_notice"])
        self.assertFalse(self.live.waking)

    def test_an_idle_thread_gets_the_refusal_note(self):
        self.start(refused="busy")
        self.assertEqual([], self.turns)
        self.assertEqual("declined P: busy Tell me to continue when ready.",
                         self.slack.chat_postMessage.call_args.kwargs["text"])

    def test_a_codex_fallback_thread_is_woken_as_its_recorded_person(self):
        self.codex_user = "UKIRK"
        with self.assertLogs("test-review-turn-audit", level="INFO") as logs:
            self.start()
        self.assertEqual("read P", self.turns[0][0])
        self.assertEqual({"pr": "P", "refused": "", "room_user": "UKIRK"}, self.turns[0][1]["review_notice"])
        self.assertEqual("The peer review of P has ended; picking it up.",
                         self.slack.chat_postMessage.call_args.kwargs["text"])
        self.assertIn("OUTCOME:woken_codex", logs.output[-1])


class CodexNoticeUserTest(unittest.TestCase):
    """Who a notice may resume a Codex fallback thread as."""

    def setUp(self):
        self.record = {"session_id": "codex-1", "channel": "D1", "user": "UKIRK", "saved_at": 100.0}
        self.on_openai = True
        self.ns = load(use_openai_provider=lambda *args, **kwargs: self.on_openai)
        # Compiled from bot.py by load(), so replaced afterwards.
        self.ns["_get_openai_session_record"] = lambda thread: self.record

    def user(self, channel="D1", requested_at=90.0):
        with mock.patch.dict(os.environ, ENV, clear=True):
            return self.ns["_codex_notice_user"]("5.1", channel, requested_at)

    def test_the_recorded_authorized_person_in_the_same_channel(self):
        self.assertEqual("UKIRK", self.user())

    def test_nothing_without_a_codex_session_or_off_the_fallback(self):
        self.record = {}
        self.assertEqual("", self.user())
        self.record, self.on_openai = {"session_id": "codex-1", "channel": "D1", "user": "UKIRK", "saved_at": 100.0}, False
        self.assertEqual("", self.user())

    def test_nothing_for_another_channel_an_unrecorded_or_unauthorized_person(self):
        self.assertEqual("", self.user(channel="D2"))
        for change in ({"user": ""}, {"channel": ""}, {"user": "USTRANGER"}, {"saved_at": None}):
            with self.subTest(change=change):
                self.record = {"session_id": "codex-1", "channel": "D1", "user": "UKIRK", "saved_at": 100.0, **change}
                self.assertEqual("", self.user())

    def test_nothing_when_the_session_has_not_run_since_the_request(self):
        # A requesting turn that names another thread finds no Codex turn there after its request.
        self.assertEqual("", self.user(requested_at=101.0))

    def test_a_run_resumes_only_the_recorded_session_as_its_person(self):
        resumes = self.ns["_codex_resumes_as"]
        self.assertTrue(resumes("5.1", "D1", "UKIRK", "codex-1"))
        self.assertFalse(resumes("5.1", "D1", "UKIRK", None))
        self.assertFalse(resumes("5.1", "D1", "UKIRK", "codex-2"))
        self.assertFalse(resumes("5.1", "D1", "UOTHER", "codex-1"))
        self.assertFalse(resumes("5.1", "D2", "UKIRK", "codex-1"))


class CodexSessionRecordTest(unittest.TestCase):
    """The harness records the thread's person on each Codex turn, except a forwarded reply."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "openai-sessions.json"
        self.ns = load(
            OPENAI_SESSION_FILE=self.file, _session_file_lock=threading.Lock(), MAX_SESSIONS=200,
            write_private_json=lambda path, value: path.write_text(json.dumps(value)),
        )

    def save(self, user, channel="D1"):
        self.ns["_save_openai_session"]("5.1", "codex-1", Path(self.temp.name), channel=channel, user_id=user)

    def test_a_forwarded_reply_keeps_the_recorded_person(self):
        self.save("UKIRK")
        self.save(None)
        record = self.ns["_get_openai_session_record"]("5.1")
        self.assertEqual("UKIRK", record["user"])
        self.assertIsInstance(record["saved_at"], float)

    def test_a_forwarded_reply_into_another_room_records_no_one(self):
        self.save("UKIRK")
        self.save(None, channel="D2")
        self.assertEqual("", self.ns["_get_openai_session_record"]("5.1")["user"])

    def test_a_malformed_file_reads_as_empty(self):
        self.file.write_text("[1, 2]")
        self.assertEqual({}, self.ns["_get_openai_session_record"]("5.1"))
        self.file.write_text('{"5.1": "legacy-id"}')
        self.assertEqual({}, self.ns["_get_openai_session_record"]("5.1"))


class CodexNoticeTurnTest(unittest.TestCase):
    """A notice turn on the Codex fallback: the peer bot speaks, the person's identity runs it."""

    def setUp(self):
        self.room = Room(review_only=False)
        self.addCleanup(self.room.close)
        self.runs = []
        self.posts = []
        self.session = "codex-1"
        slack = mock.MagicMock()
        slack.chat_postMessage.side_effect = lambda **kwargs: self.posts.append(kwargs["text"])

        def run(**kwargs):
            self.runs.append(kwargs)
            return "codex-1", mock.MagicMock()

        self.ns = load(
            resolve_room_policy=lambda config, channel, user: self.room.policy,
            peer_review_config=lambda config: mock.MagicMock(peer_bot_user_ids=frozenset({"UPEER"})),
            use_openai_provider=lambda *args, **kwargs: True,
            _thread_uses_openai=lambda thread: True,
            _get_openai_session=lambda thread: self.session,
            _run_openai_fallback=run,
            _take_handoff=lambda *args, **kwargs: None,
            slack_client=slack,
            REVIEW_NOTICE_IDLE="ended {pr}",
        )

    def notice(self):
        event = {"type": "review_notice", "user": "UPEER", "channel": "D1", "channel_type": "im",
                 "thread_ts": "5.1", "ts": "9.9", "text": f"read {PR}"}
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.ns["process_message_async"](
                event, review_notice={"pr": PR, "refused": "", "room_user": "UKIRK"},
            )

    def test_it_resumes_the_codex_session_as_the_person_with_peer_authority(self):
        self.notice()
        self.assertEqual(1, len(self.runs))
        self.assertEqual("UKIRK", self.runs[0]["user_id"])
        self.assertTrue(self.runs[0]["resume_only"])
        payload = json.loads(self.runs[0]["prompt"].split("\n\n", 1)[-1].splitlines()[1])
        self.assertEqual("review_notice", payload["current_sender"]["authority"])
        self.assertEqual(PR, payload["review_notice_pr"])

    def test_a_refused_resume_leaves_the_note(self):
        def refuse(**kwargs):
            self.runs.append(kwargs)
            raise self.ns["_NoLiveSession"]()

        self.ns["_run_openai_fallback"] = refuse
        self.notice()
        self.assertEqual([f"ended {PR}"], self.posts)

    def test_it_never_starts_a_codex_session(self):
        self.session = None
        self.notice()
        self.assertEqual([], self.runs)
        self.assertEqual([f"ended {PR}"], self.posts)


class OneProviderPerThreadTest(unittest.TestCase):
    """A Claude work turn never runs beside the thread's Codex turn; a message that finds one waits, then reroutes."""

    def setUp(self):
        self.room = Room(review_only=False)
        self.addCleanup(self.room.close)
        self.slack = mock.MagicMock()
        self.posts = []
        self.slack.chat_postMessage.side_effect = lambda **kwargs: self.posts.append(kwargs["text"])
        self.lock = threading.Lock()
        self.state = {"codex_running": False, "openai": False}
        self.sent = []
        self.runs = []
        self.waits = []
        self.session = FakeSession()

        def send(session, text):
            self.sent.append((text, self.lock.locked()))

        def wait(thread_ts, queue_notice):
            self.waits.append(thread_ts)
            self.state["codex_running"] = False

        def run(**kwargs):
            self.runs.append(kwargs)
            return "codex-1", mock.MagicMock()

        self.ns = load(
            resolve_room_policy=lambda config, channel, user: self.room.policy,
            _send_to_claude=send,
            slack_client=self.slack,
            wait_for_turn_completion=lambda *args, **kwargs: True,
            _take_handoff=lambda *args, **kwargs: None,
            _openai_turn_lock=lambda thread: self.lock,
            _thread_turn_running=lambda thread: self.state["codex_running"] or self.lock.locked(),
            _wait_for_thread_turn=wait,
            use_openai_provider=lambda *args, **kwargs: self.state["openai"],
            _thread_uses_openai=lambda thread: self.state["openai"],
            _run_openai_fallback=run,
            REVIEW_NOTICE_IDLE="ended {pr}",
        )
        self.ns["_get_or_create_live_session"] = lambda *args, **kwargs: self.session

    def message(self, **kwargs):
        event = {"user": "UKIRK", "channel": "DKIRK", "channel_type": "im", "thread_ts": "5.1", "ts": "6.1",
                 "text": "carry on"}
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.ns["process_message_async"](event, **kwargs)

    def test_a_message_during_a_codex_turn_waits_then_runs_on_claude(self):
        self.state["codex_running"] = True
        self.message()
        self.assertEqual(["5.1"], self.waits)
        self.assertEqual(1, len(self.sent))
        self.assertEqual([], self.runs)

    def test_a_claude_turn_holds_the_thread_lock_while_it_runs_and_releases_it(self):
        self.message()
        self.assertEqual([True], [held for _, held in self.sent])
        self.assertFalse(self.lock.locked())

    def test_a_queued_codex_turn_reroutes_when_the_thread_moved_to_claude(self):
        answers = iter([True, False, False, False])
        self.ns["_thread_uses_openai"] = lambda thread: next(answers)
        self.message()
        self.assertEqual([], self.runs)
        self.assertEqual(1, len(self.sent))
        self.assertFalse(self.lock.locked())

    def test_a_claude_message_queued_for_a_slot_reroutes_when_the_thread_moved_to_codex(self):
        answers = iter([False, True, True, True])
        self.ns["_thread_uses_openai"] = lambda thread: next(answers)
        admission = mock.MagicMock()
        self.ns["_turn_admission"] = admission
        self.message()
        self.assertEqual([], self.sent)
        self.assertEqual(1, len(self.runs))
        self.assertFalse(self.lock.locked())
        # The slot it waited for is given back before it waits for the thread.
        self.assertEqual(1, admission.acquire.call_count)
        self.assertEqual(1, admission.release.call_count)

    def test_a_provider_lookup_error_never_leaks_the_thread_lock(self):
        def broken(thread):
            if self.lock.locked():
                raise OSError("state file unreadable")
            return False

        self.ns["_thread_uses_openai"] = broken
        self.message()
        self.assertFalse(self.lock.locked())
        self.assertEqual([], self.sent)

    def notice_setup(self):
        self.session.channel = "DKIRK"
        self.session.review = False
        self.session.waking = True
        self.ns["_live_sessions"]["5.1"] = self.session
        self.ns["peer_review_config"] = lambda config: mock.MagicMock(peer_bot_user_ids=frozenset({"UPEER"}))

    def notice(self):
        event = {"type": "review_notice", "user": "UPEER", "channel": "DKIRK", "channel_type": "im",
                 "thread_ts": "5.1", "ts": "9.9", "text": f"read {PR}"}
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.ns["process_message_async"](event, review_notice={"pr": PR, "refused": "", "room_user": "UKIRK"})

    def test_a_notice_waits_for_the_codex_turn_then_wakes_its_session(self):
        self.notice_setup()
        self.state["codex_running"] = True
        self.notice()
        self.assertEqual(["5.1"], self.waits)
        self.assertEqual(1, len(self.sent))
        self.assertFalse(self.session.waking)

    def test_a_notice_wakes_only_the_session_it_was_sent_to(self):
        self.notice_setup()
        self.state["codex_running"] = True
        other = FakeSession()
        other.channel, other.review, other.waking = "DKIRK", False, False

        def replaced(thread_ts, queue_notice):
            self.waits.append(thread_ts)
            self.assertTrue(self.session.waking)
            self.ns["_live_sessions"]["5.1"] = other
            self.state["codex_running"] = False

        self.ns["_wait_for_thread_turn"] = replaced
        self.notice()
        self.assertEqual([], self.sent)
        self.assertFalse(self.session.waking)
        self.assertEqual([f"ended {PR}"], self.posts)

    def test_a_notice_never_moves_provider_while_it_waits(self):
        self.notice_setup()
        self.state["codex_running"] = True

        def switched(thread_ts, queue_notice):
            self.waits.append(thread_ts)
            self.state["codex_running"] = False
            self.state["openai"] = True

        self.ns["_wait_for_thread_turn"] = switched
        self.notice()
        self.assertEqual([], self.sent)
        self.assertEqual([], self.runs)
        self.assertFalse(self.session.waking)
        self.assertEqual([f"ended {PR}"], self.posts)

    def test_a_limit_replay_inside_a_claude_turn_reuses_the_held_lock(self):
        class NoReentry:
            def __init__(self):
                self.inner = threading.Lock()

            def acquire(self, blocking=True):
                if blocking and self.inner.locked():
                    raise AssertionError("the thread lock was taken twice")
                return self.inner.acquire(blocking)

            def release(self):
                self.inner.release()

            def locked(self):
                return self.inner.locked()

            __enter__ = lambda self: self.acquire()
            __exit__ = lambda self, *exc: self.release()

        self.lock = NoReentry()
        self.ns["_openai_turn_lock"] = lambda thread: self.lock
        self.session.limit_notices = set()

        def limited(session, text):
            session.limit_notices.add("limit")
            session._on_text("limit")

        self.ns["_send_to_claude"] = limited
        self.ns["_enter_limit_pause"] = lambda text: None
        self.ns["_fetch_thread_context"] = lambda *args: None
        self.message()
        self.assertEqual(1, len(self.runs))
        self.assertFalse(self.lock.locked())

if __name__ == "__main__":
    unittest.main()
