"""Behavioural tests of the Codex review path and review deferral in bot.py, against stub globals.

As in test_review_turn.py, bot.py cannot be imported in tests, so the functions are compiled from
its source and every module global they read is stubbed.
"""

import ast
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

from cargo_chief_safety import (
    RoomPolicy,
    SafetyError,
    TOKEN_PERMISSIONS_ENV,
    build_codex_review_prompt,
    claude_owner_env,
    github_wrapper_path,
    prepare_thread_workspace,
    review_turn_env,
)
from openai_fallback import CodexTurnResult, fallback_error_kind
from review_fallback import (
    DEFERRED_FIRST_AT_KEY,
    POST_REQUEST_FILE_NAME,
    git_fetch_paths,
    REVIEW_FILE_NAME,
    PostRequestError,
    build_codex_review_command,
    codex_review_env,
    read_post_request,
    review_checkout,
)
from review_requests import (
    CODEX_UNAVAILABLE_REPLY,
    DEFERRED_REPLY,
    REVIEW_DONE_MARKER,
    ReviewAdmission,
    ReviewCapStore,
    ReviewPlan,
    build_review_task,
)
from tests.test_review_turn import _globals_used

BOT = Path(__file__).parents[1] / "bot.py"
FUNCTIONS = (
    "_run_codex_review", "_defer_review", "_continue_review_after_limit", "_strip_review_marker",
    "_limit_pause_until", "_codex_review_ready",
)
PR = "https://github.com/Cargo-Chief/c4/pull/123"


def load(**overrides):
    tree = ast.parse(BOT.read_text(encoding="utf-8"))
    wanted = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    assigns = [
        node for node in tree.body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in ("_REVIEW_DONE_LINE", "_WITHHELD_REVIEW")
    ]
    namespace = {name: mock.MagicMock(name=name) for name in _globals_used(wanted)}
    namespace.update({
        "os": os, "re": re, "sys": sys, "time": time, "Path": Path, "SafetyError": SafetyError,
        "REVIEW_DONE_MARKER": REVIEW_DONE_MARKER, "RoomPolicy": RoomPolicy, "ReviewAdmission": ReviewAdmission,
        "prepare_thread_workspace": prepare_thread_workspace, "build_review_task": build_review_task,
        "build_codex_review_command": build_codex_review_command, "codex_review_env": codex_review_env,
        "claude_owner_env": claude_owner_env, "review_turn_env": review_turn_env,
        "github_wrapper_path": github_wrapper_path, "build_codex_review_prompt": build_codex_review_prompt,
        "review_checkout": review_checkout, "read_post_request": read_post_request,
        "PostRequestError": PostRequestError, "fallback_error_kind": fallback_error_kind,
        "REVIEW_FILE_NAME": REVIEW_FILE_NAME, "POST_REQUEST_FILE_NAME": POST_REQUEST_FILE_NAME,
        "DEFERRED_REPLY": DEFERRED_REPLY, "PROVIDER_REPLY": "paused", "WRAPPER_REPLY": "old wrapper",
        "CODEX_UNAVAILABLE_REPLY": CODEX_UNAVAILABLE_REPLY, "DEFERRED_FIRST_AT_KEY": DEFERRED_FIRST_AT_KEY,
        "git_fetch_paths": git_fetch_paths, "REVIEW_MAX_RUNTIME": 2700,
        "_codex_review_denied": lambda: [Path("/home/.config/cargo-chief")],
        "CLAUDE_TIMEOUT": 60, "DELEGATE_TIMEOUT": 30, "LIMIT_PAUSE_FALLBACK": 1800,
        "SOURCE_DIR": Path(__file__).parents[1],
        "logger": logging.getLogger("test-codex-review"),
        "audit_logger": logging.getLogger("test-codex-review-audit"),
        "_live_sessions_lock": threading.Lock(), "_openai_processes": {}, "_openai_stopped": set(),
        "_limit_pause_lock": threading.Lock(), "_limit_pause": {"until": 0.0, "announced": False},
        "_limit_paused": lambda: False,
        "_identity_prompt": lambda: "",
        "github_wrapper_downscopes": lambda root: True,
        "delegation_verification_status": lambda path: None,
        "governed_delegate_active": lambda path: False,
    })
    namespace.update(overrides)
    exec(compile(ast.Module(body=[*assigns, *wanted], type_ignores=[]), str(BOT), "exec"), namespace)
    return namespace


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        overlay = self.root / "overlay.md"
        overlay.write_text("review only\n")
        self.policy = RoomPolicy(
            root=self.root, permission_mode="auto", overlay=overlay, escalation_channel="DAPPROVER",
            backend="claude", model="claude-opus-5-5", effort="high", role="eng",
            fallback_backend="codex", fallback_model="gpt-6-sol", fallback_effort="high", review_only=True,
        )
        self.texts = []
        self.audits = []
        self.posts = []
        self.runs = []
        self.posted = []
        self.result = CodexTurnResult(texts=["Verdict: approve.\n" + REVIEW_DONE_MARKER])
        self.write_request = True
        peer = mock.MagicMock(self_github_login="lisa", github_logins={"UNED": "ned"})
        self.ns = load(
            run_codex_turn=self.run_codex,
            post_review=self.post_review,
            peer_review_config=lambda config: peer,
            validate_codex_runtime=lambda: None,
            TEMP_DIR=self.root / "tmp",
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_codex(self, command, prompt, *, cwd, env, **kwargs):
        self.runs.append((command, prompt, cwd, env))
        if self.write_request:
            (Path(cwd) / REVIEW_FILE_NAME).write_text('{"body": "x"}')
            (Path(cwd) / POST_REQUEST_FILE_NAME).write_text('{"verdict": "approve", "resolve": ["T1"]}')
        return self.result

    def post_review(self, request, **kwargs):
        self.posted.append((request, kwargs))
        return True, "ok"

    def admission(self, plan=ReviewPlan("incremental", 2, 3, "a" * 40)):
        event = {"user": "UNED", "bot_id": "BNED", "channel": "CREVIEWS", "ts": "1.1",
                 "text": f"<@ULISA> please review {PR}"}
        admission = ReviewAdmission(
            event=event, pr=PR, policy=self.policy, limit=6,
            caps=ReviewCapStore(self.root / "caps.json"),
            post=lambda channel, thread, text: self.posts.append(text),
            audit=self.audits.append, plan=plan,
        )
        return admission

    def review(self, admission=None):
        return self.ns["_run_codex_review"](
            admission or self.admission(), self.policy, channel="CREVIEWS", thread_ts="1.1",
            user_id="UNED", envelope="ENVELOPE", on_text=self.texts.append, max_duration=60,
        )


class CodexReviewTest(Base):
    def test_a_review_runs_read_only_and_the_harness_posts_it(self):
        self.assertEqual("done", self.review())
        command, prompt, cwd, env = self.runs[0]
        self.assertEqual("--", command[2])
        self.assertTrue(command[1].endswith("with_github_app_token.py"))
        self.assertEqual("codex", command[3])
        self.assertIn('approval_policy="never"', command)
        self.assertEqual({"contents": "read", "pull_requests": "read"}, json.loads(env[TOKEN_PERMISSIONS_ENV]))
        self.assertEqual("openai", env["CARGO_CHIEF_OWNER_PROVIDER"])
        self.assertIn("ENVELOPE", prompt)
        self.assertIn(str(Path(cwd) / POST_REQUEST_FILE_NAME), prompt)
        self.assertEqual(["Verdict: approve.", "Posted the review on the PR (approve)."], self.texts)
        request, kwargs = self.posted[0]
        self.assertEqual(("approve", ("T1",)), (request.verdict, request.resolve))
        write_env = kwargs["env"]
        self.assertEqual({"contents": "read", "pull_requests": "write"}, json.loads(write_env[TOKEN_PERMISSIONS_ENV]))
        self.assertEqual("ned", write_env["CARGO_CHIEF_REVIEW_REQUESTER_LOGIN"])
        self.assertEqual("2", write_env["CARGO_CHIEF_REVIEW_PASS"])

    def test_only_what_git_fetch_writes_in_the_clone_and_nothing_found_means_nothing(self):
        self.review()
        command = self.runs[0][0]
        table = next(arg for arg in command if arg.startswith("permissions.cc_review.filesystem="))
        self.assertNotIn("/repos/", table)
        self.assertIn('"/home/.config/cargo-chief" = "none"', table)
        self.runs.clear()
        clone = self.root / "repos" / "c4"
        (clone / ".git").mkdir(parents=True)
        (clone / ".git" / "config").write_text('[remote "origin"]\n\turl = https://github.com/Cargo-Chief/c4.git\n')
        self.review()
        table = next(arg for arg in self.runs[0][0] if arg.startswith("permissions.cc_review.filesystem="))
        self.assertIn(f'"{clone / ".git" / "objects"}" = "write"', table)
        self.assertNotIn(f'"{clone / ".git"}" = "write"', table)
        self.assertNotIn("/work\" = \"write\"", table)

    def test_codex_that_cannot_start_leaves_the_review_to_be_deferred(self):
        self.ns["validate_codex_runtime"] = mock.MagicMock(side_effect=SafetyError("no codex"))
        self.assertEqual("unavailable", self.review())
        self.assertEqual([], self.runs)
        self.result = CodexTurnResult(error="Codex fallback exited with status 1")
        self.ns["validate_codex_runtime"] = lambda: None
        self.write_request = False
        self.assertEqual("unavailable", self.review())
        self.assertEqual([], self.posted)
        self.assertEqual([], self.texts)

    def test_a_timeout_says_so_and_posts_nothing(self):
        self.result = CodexTurnResult(error="Codex fallback timed out")
        self.assertEqual("done", self.review())
        self.assertEqual([], self.posted)
        self.assertIn("time limit", self.texts[0])

    def test_an_error_after_the_post_request_was_written_still_posts(self):
        self.result = CodexTurnResult(texts=["Verdict: approve."], error="stream hiccup")
        self.assertEqual("done", self.review())
        self.assertEqual(1, len(self.posted))
        self.assertEqual("Posted the review on the PR (approve).", self.texts[-1])

    def test_no_delegation_state_reaches_the_turn(self):
        self.review()
        command, prompt, cwd, env = self.runs[0]
        self.assertNotIn("CARGO_CHIEF_DELEGATION_REQUEST_FILE", env)
        self.assertNotIn("governed_delegation.py", prompt)

    def test_no_post_request_or_a_bad_one_posts_nothing(self):
        self.write_request = False
        self.review()
        self.assertEqual([], self.posted)
        self.assertIn("no post request", self.texts[-1])

    def test_a_stopped_review_posts_nothing(self):
        self.ns["_openai_stopped"].add("1.1")
        self.review()
        self.assertEqual(([], []), (self.posted, self.texts))

    def test_a_refused_post_is_reported(self):
        self.ns["post_review"] = lambda request, **kwargs: (False, "head moved")
        self.review()
        self.assertEqual("review_post.py did not post the review: head moved", self.texts[-1])

    def test_an_outdated_wrapper_runs_nothing(self):
        self.ns["github_wrapper_downscopes"] = lambda root: False
        self.assertEqual("done", self.review())
        self.assertEqual(([], ["old wrapper"]), (self.runs, self.texts))


class DeferTest(Base):
    def setUp(self):
        super().setUp()
        self.store = mock.MagicMock()
        self.ns["_deferred_reviews"] = self.store

    def test_a_review_is_deferred_to_the_end_of_the_limit_pause(self):
        self.ns["_limit_pause"]["until"] = time.time() + 3600
        self.ns["_defer_review"](self.admission())
        self.assertEqual(self.ns["_limit_pause"]["until"], self.store.add.call_args.kwargs["not_before"])
        self.assertTrue(self.audits[-1].endswith("OUTCOME:deferred"))
        self.assertIn("queued it", self.posts[0])
        self.assertIn("UTC", self.posts[0])

    def test_a_replay_deferred_again_posts_nothing_new(self):
        self.ns["_limit_pause"]["until"] = time.time() + 3600
        admission = self.admission()
        admission.event[DEFERRED_FIRST_AT_KEY] = 1.0
        self.ns["_defer_review"](admission)
        self.store.add.assert_called_once()
        self.assertEqual([], self.posts)
        self.assertTrue(self.audits[-1].endswith("OUTCOME:deferred"))

    def test_outside_a_pause_a_pinned_thread_is_refused_not_queued(self):
        self.ns["_defer_review"](self.admission())
        self.store.add.assert_not_called()
        self.assertEqual([f"<@UNED> {CODEX_UNAVAILABLE_REPLY}"], self.posts)
        self.assertTrue(self.audits[-1].endswith("OUTCOME:refused_provider"))

    def test_a_record_that_cannot_be_written_refuses_aloud(self):
        self.ns["_limit_pause"]["until"] = time.time() + 3600
        self.store.add.side_effect = OSError("disk")
        self.ns["_defer_review"](self.admission())
        self.assertEqual(["<@UNED> paused"], self.posts, "a peer bot requester is mentioned")
        self.assertTrue(self.audits[-1].endswith("OUTCOME:refused_provider"))


class AfterLimitTest(Base):
    def after_limit(self, marked_now, lookup_error=None):
        plan = ReviewPlan("incremental", 2, 3, "a" * 40, marked=1)
        reviews = [{"type": "Bot", "login": "lisa[bot]",
                    "body": f"<!-- cc-peer-review pass={i + 1} mode=full head={'a' * 40} -->"}
                   for i in range(marked_now)]
        lookup = mock.MagicMock(side_effect=lookup_error, return_value=mock.MagicMock(reviews=reviews))
        self.ns["_lookup_review_pr"] = lookup
        self.ns["plan_review_pass"] = __import__("review_requests").plan_review_pass
        self.ns["_defer_review"] = mock.MagicMock()
        self.ns["_run_codex_review"] = mock.MagicMock(return_value="done")
        self.ns["_continue_review_after_limit"](
            self.admission(plan), self.policy, channel="CREVIEWS", thread_ts="1.1", user_id="UNED",
            envelope="E", on_text=self.texts.append,
        )

    def test_an_unposted_review_moves_to_codex_with_a_full_runtime(self):
        self.after_limit(marked_now=1)
        self.ns["_run_codex_review"].assert_called_once()
        self.assertEqual(2700, self.ns["_run_codex_review"].call_args.kwargs["max_duration"])
        self.ns["_defer_review"].assert_not_called()

    def test_a_review_already_on_the_pr_is_not_posted_twice(self):
        self.after_limit(marked_now=2)
        self.ns["_run_codex_review"].assert_not_called()
        self.assertIn("posted on the PR before", self.texts[0])

    def test_an_unknown_pr_state_defers_rather_than_risk_a_double_post(self):
        self.after_limit(marked_now=1, lookup_error=RuntimeError("gh"))
        self.ns["_run_codex_review"].assert_not_called()
        self.ns["_defer_review"].assert_called_once()

    def test_codex_unavailable_defers(self):
        self.after_limit(marked_now=1)
        self.ns["_run_codex_review"].return_value = "unavailable"
        self.ns["_continue_review_after_limit"](
            self.admission(ReviewPlan("incremental", 2, 3, "a" * 40, marked=1)), self.policy,
            channel="CREVIEWS", thread_ts="1.1", user_id="UNED", envelope="E",
            on_text=self.texts.append,
        )
        self.ns["_defer_review"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
