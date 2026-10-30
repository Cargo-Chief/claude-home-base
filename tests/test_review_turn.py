"""Behavioural tests of the review path in bot.py, compiled against stub globals.

bot.py cannot be imported in tests (it needs Slack credentials and an external env file at
import time), so the functions under test are compiled from its source, as in
test_reader_resilience.py. Every module global they read is stubbed.
"""

import ast
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
)
CLASSES = ("_ReviewBusy", "_NoLiveSession")
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
            "CARGO_CHIEF_PARKING_CLAIM_FILE", "CARGO_CHIEF_BUNDLE_CLAIM_FILE",
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
                   existing_only=False):
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


if __name__ == "__main__":
    unittest.main()
