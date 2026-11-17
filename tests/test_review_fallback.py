"""Tests for running a peer review on the Codex fallback, and for deferring one neither provider can run."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))

from cargo_chief_safety import (
    CODEX_REVIEW_HARNESS_PROMPT,
    RoomPolicy,
    TOKEN_PERMISSIONS_ENV,
    build_codex_review_prompt,
)
from review_fallback import (
    DEFERRED_FIRST_AT_KEY,
    POST_REQUEST_FILE_NAME,
    REVIEW_FILE_NAME,
    DeferStoreError,
    DeferredReviewStore,
    PostRequest,
    PostRequestError,
    build_codex_review_command,
    codex_config_paths,
    codex_review_env,
    post_review,
    pr_repo,
    read_post_request,
    review_checkout,
)
from review_requests import ReviewPlan, build_review_task, plan_review_pass, review_marker

PR = "https://github.com/Cargo-Chief/c4/pull/123"


def policy(root: Path) -> RoomPolicy:
    overlay = root / "overlay.md"
    overlay.write_text("review only\n")
    return RoomPolicy(
        root=root, permission_mode="auto", overlay=overlay, escalation_channel="DAPPROVER",
        backend="claude", model="claude-opus-5-5", effort="high", role="eng",
        fallback_backend="codex", fallback_model="gpt-6-sol", fallback_effort="high", review_only=True,
    )


def clone(path: Path, url: str) -> Path:
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "config").write_text(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n')
    return path


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()


class CommandTest(Temp):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.codex_home = self.home / ".codex"
        (self.codex_home / "rules").mkdir(parents=True)
        for name in ("config.toml", "cargo-chief.config.toml", "AGENTS.md", "auth.json", "state_5.sqlite"):
            (self.codex_home / name).write_text("x")
        (self.codex_home / "sessions").mkdir()
        self.command = build_codex_review_command(
            policy(self.root), cwd=self.root / "work" / "thread", writable=[self.root / "work"],
            home=self.home, codex_home=self.codex_home,
        )

    def flag(self, prefix):
        values = [arg for arg in self.command if arg.startswith(prefix)]
        self.assertEqual(1, len(values), prefix)
        return values[0][len(prefix):]

    def test_no_approvals_and_no_learned_rules(self):
        self.assertEqual('"never"', self.flag("approval_policy="))
        self.assertIn("--ignore-rules", self.command)
        self.assertIn("--ignore-user-config", self.command)
        self.assertIn("features.apps=false", self.command)
        self.assertIn("mcp_servers={}", self.command)
        self.assertIn("--ephemeral", self.command)
        self.assertEqual('"cc_review"', self.flag("default_permissions="))
        self.assertIn("features.multi_agent=false", self.command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", self.command)

    def test_credentials_are_unreadable_and_codex_config_read_only(self):
        table = self.flag("permissions.cc_review.filesystem=")
        filesystem = dict(
            (json.loads(key), json.loads(value))
            for key, value in (item.split(" = ") for item in table.strip("{}").split(", "))
        )
        for denied in (".config/cargo-chief", ".config/gh", ".aws", ".ssh"):
            self.assertEqual("none", filesystem[str(self.home / denied)], denied)
        self.assertEqual("write", filesystem[str(self.root / "work")])
        self.assertEqual("write", filesystem[str(self.codex_home)])
        for kept in ("rules", "auth.json", "config.toml", "cargo-chief.config.toml", "AGENTS.md"):
            self.assertEqual("read", filesystem[str(self.codex_home / kept)], kept)
        self.assertNotIn(str(self.codex_home / "state_5.sqlite"), filesystem)
        self.assertNotIn(str(self.root / "repos"), filesystem)

    def test_codex_config_paths_lists_only_files_that_steer_later_turns(self):
        names = {path.name for path in codex_config_paths(self.codex_home)}
        self.assertEqual({"rules", "auth.json", "config.toml", "cargo-chief.config.toml", "AGENTS.md"}, names)
        self.assertEqual({"rules", "auth.json"}, {p.name for p in codex_config_paths(self.root / "absent")})

    def test_model_and_cwd(self):
        self.assertEqual("gpt-6-sol", self.command[self.command.index("--model") + 1])
        self.assertEqual(str(self.root / "work" / "thread"), self.command[self.command.index("--cd") + 1])
        self.assertEqual("-", self.command[-1])


class EnvTest(unittest.TestCase):
    def test_read_only_token_and_no_claude_or_service_secrets(self):
        env = codex_review_env(
            {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "c", "ANTHROPIC_API_KEY": "a", "SLACK_BOT_TOKEN": "s",
             "AWS_PROFILE": "p", "OPENAI_API_KEY": "o", "CODEX_HOME": "/h/.codex"},
            self_login="lisa", requester_login="ned", pr=PR, mode="full", pass_no=1, review_base="",
        )
        self.assertEqual({"contents": "read", "pull_requests": "read"}, json.loads(env[TOKEN_PERMISSIONS_ENV]))
        for gone in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "AWS_PROFILE", "OPENAI_API_KEY"):
            self.assertNotIn(gone, env)
        self.assertEqual("/h/.codex", env["CODEX_HOME"])
        self.assertEqual(PR, env["CARGO_CHIEF_REVIEW_PR"])
        self.assertEqual("1", env["CARGO_CHIEF_REVIEW_ONLY"])


class CheckoutTest(Temp):
    def test_finds_the_clone_by_origin_including_top_level_repos(self):
        clone(self.root / "repos" / "c4", "git@github.com:Cargo-Chief/c4.git")
        clone(self.root / "docs", "https://github.com/Cargo-Chief/platform-docs")
        clone(self.root / "repos" / "c4-client", "https://github.com/Cargo-Chief/c4-client.git")
        self.assertEqual((self.root / "repos" / "c4").resolve(), review_checkout(self.root, PR))
        self.assertEqual(
            (self.root / "docs").resolve(),
            review_checkout(self.root, "https://github.com/Cargo-Chief/platform-docs/pull/1170"),
        )

    def test_a_name_match_with_another_origin_is_not_the_clone(self):
        clone(self.root / "repos" / "c4", "https://github.com/someone/c4.git")
        self.assertIsNone(review_checkout(self.root, PR))

    def test_a_worktree_or_odd_url_is_never_granted(self):
        worktree = self.root / "repos" / "c4"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: /elsewhere\n")
        self.assertIsNone(review_checkout(self.root, PR))
        self.assertIsNone(review_checkout(self.root, "https://example.com/x/pull/1"))
        self.assertEqual("c4", pr_repo(PR))
        self.assertIsNone(pr_repo(PR + "/files"))


class PostRequestTest(Temp):
    def write(self, verdict="approve", resolve=(), review=b'{"body": "ok"}', **extra):
        (self.root / REVIEW_FILE_NAME).write_bytes(review)
        (self.root / POST_REQUEST_FILE_NAME).write_text(
            json.dumps({"verdict": verdict, "resolve": list(resolve), **extra})
        )

    def test_no_request_means_nothing_to_post(self):
        self.assertIsNone(read_post_request(self.root))

    def test_a_valid_request(self):
        self.write("request_changes", ["PRRT_kwDOabc", "PRRT_kwDOabc"])
        request = read_post_request(self.root)
        self.assertEqual(("request_changes", ("PRRT_kwDOabc",), b'{"body": "ok"}'),
                         (request.verdict, request.resolve, request.review))

    def test_invalid_requests_are_refused(self):
        cases = [
            dict(verdict="merge"),
            dict(resolve=["has space"]),
            dict(resolve=["x"] * 101),
            dict(extra="field"),
        ]
        for case in cases:
            with self.subTest(case=case):
                self.write(**case)
                with self.assertRaises(PostRequestError):
                    read_post_request(self.root)

    def test_symlinks_are_never_followed(self):
        secret = self.root / "secret.env"
        secret.write_text("TOKEN=value")
        self.write()
        (self.root / REVIEW_FILE_NAME).unlink()
        os.symlink(secret, self.root / REVIEW_FILE_NAME)
        with self.assertRaisesRegex(PostRequestError, "not a plain file"):
            read_post_request(self.root)
        self.write()
        (self.root / POST_REQUEST_FILE_NAME).unlink()
        os.symlink(secret, self.root / POST_REQUEST_FILE_NAME)
        with self.assertRaisesRegex(PostRequestError, "not a plain file"):
            read_post_request(self.root)

    def test_missing_or_oversized_review_is_refused(self):
        self.write()
        (self.root / REVIEW_FILE_NAME).unlink()
        with self.assertRaisesRegex(PostRequestError, "missing"):
            read_post_request(self.root)
        self.write(review=b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(PostRequestError, "larger"):
            read_post_request(self.root)


class PostReviewTest(Temp):
    def test_posts_a_harness_copy_through_the_wrapper_and_removes_it(self):
        seen = {}

        def runner(command, **kwargs):
            seen["command"] = command
            seen["env"] = kwargs["env"]
            seen["review"] = Path(command[command.index("--review") + 1]).read_bytes()
            return subprocess.CompletedProcess(command, 0, "posted", "")

        ok, _ = post_review(
            PostRequest("approve", ("T1", "T2"), b"REVIEW"), pr=PR, python="py", wrapper=Path("/w.py"),
            script=Path("/review_post.py"), env={"K": "v"}, scratch_dir=self.root / "tmp", runner=runner,
        )
        self.assertTrue(ok)
        command = seen["command"]
        self.assertEqual(["py", "/w.py", "--", "py", "/review_post.py", "submit", PR], command[:7])
        self.assertEqual(["--verdict", "approve", "--resolve", "T1", "T2"], command[9:])
        self.assertEqual(b"REVIEW", seen["review"])
        self.assertEqual({"K": "v"}, seen["env"])
        self.assertEqual([], list((self.root / "tmp").iterdir()), "the copy is removed")

    def test_a_refusal_reports_the_tail_of_stderr(self):
        runner = lambda command, **kwargs: subprocess.CompletedProcess(command, 2, "", "refused:\n  head moved")
        ok, detail = post_review(
            PostRequest("comment", (), b"R"), pr=PR, python="py", wrapper=Path("/w.py"),
            script=Path("/p.py"), env={}, scratch_dir=self.root, runner=runner,
        )
        self.assertFalse(ok)
        self.assertEqual("refused: head moved", detail)

    def test_a_timeout_is_reported_not_raised(self):
        def runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)
        ok, detail = post_review(
            PostRequest("comment", (), b"R"), pr=PR, python="py", wrapper=Path("/w.py"),
            script=Path("/p.py"), env={}, scratch_dir=self.root, runner=runner,
        )
        self.assertEqual((False, "posting the review timed out"), (ok, detail))


class DeferredStoreTest(Temp):
    def setUp(self):
        super().setUp()
        self.now = [1000.0]
        self.store = DeferredReviewStore(self.root / "deferred.json", window=100, clock=lambda: self.now[0])
        self.event = {"user": "UNED", "bot_id": "BNED", "channel": "C", "ts": "1.1",
                      "text": f"<@ULISA> please review {PR}", "blocks": [{"big": "x"}]}

    def test_due_entries_are_taken_once_with_only_replay_fields(self):
        self.store.add(self.event, pr=PR, not_before=1050)
        self.assertEqual(([], []), self.store.take_due())
        self.now[0] = 1050
        due, expired = self.store.take_due()
        self.assertEqual([], expired)
        self.assertEqual(1, len(due))
        event = due[0]["event"]
        self.assertNotIn("blocks", event)
        self.assertEqual("BNED", event["bot_id"])
        self.assertEqual(1000.0, event[DEFERRED_FIRST_AT_KEY])
        self.assertEqual(([], []), self.store.take_due())

    def test_one_entry_per_pr_keeping_the_first_deferral(self):
        first = self.store.add(self.event, pr=PR, not_before=1010)
        self.now[0] = 1020
        replayed = dict(self.event, **{DEFERRED_FIRST_AT_KEY: first})
        self.assertEqual(1000.0, self.store.add(replayed, pr=PR, not_before=1030))
        self.now[0] = 1030
        due, _ = self.store.take_due()
        self.assertEqual(1, len(due))
        self.assertEqual(1000.0, due[0]["first_at"])

    def test_a_request_deferred_past_the_window_expires(self):
        self.store.add(self.event, pr=PR, not_before=2000)
        self.now[0] = 1101
        due, expired = self.store.take_due()
        self.assertEqual(([], [PR]), (due, [entry["pr"] for entry in expired]))

    def test_an_unreadable_record_raises_and_malformed_entries_are_dropped(self):
        (self.root / "deferred.json").write_text("{not json")
        with self.assertRaises(DeferStoreError):
            self.store.take_due()
        (self.root / "deferred.json").write_text(json.dumps({PR: {"event": "x"}, "y": 3}))
        self.assertEqual(([], []), self.store.take_due())
        self.assertEqual({}, json.loads((self.root / "deferred.json").read_text()))


class TaskAndPromptTest(Temp):
    def test_harness_posted_task_names_its_files_and_no_write_route(self):
        plan = ReviewPlan("full", 1, 3)
        task = build_review_task(PR, self.root, plan, harness_posts=(self.root / "r.json", self.root / "p.json"))
        self.assertIn(str(self.root / "r.json"), task)
        self.assertIn(str(self.root / "p.json"), task)
        self.assertIn("read-only", task)
        self.assertNotIn(" submit ", task)
        self.assertNotIn("with_github_app_token", task)
        self.assertIn("REVIEW COMPLETE", task)
        claude_task = build_review_task(PR, self.root, plan)
        self.assertIn(" submit ", claude_task)

    def test_codex_review_prompt_is_review_only_and_non_mutating(self):
        prompt = build_codex_review_prompt(
            policy(self.root), inbound_prompt="TASK", transport_python=Path("/py"),
            transport_script=Path("/hb/bot.py"), delegation_request_file=self.root / "d.json",
            implementation_claim_file=self.root / "i", delegate_timeout=60,
        )
        self.assertIn(CODEX_REVIEW_HARNESS_PROMPT, prompt)
        self.assertIn("mutation=false", prompt)
        self.assertNotIn("--escalate", prompt)
        self.assertNotIn("--request-review", prompt)
        self.assertTrue(prompt.endswith("TASK"))

    def test_plan_counts_marked_reviews_without_changing_equality(self):
        mine = {"type": "Bot", "login": "lisa[bot]"}
        reviews = [dict(mine, body=review_marker(1, "full", "a" * 40)),
                   dict(mine, body="no marker"),
                   dict(mine, body=review_marker(2, "incremental", "b" * 40))]
        plan = plan_review_pass(reviews, "lisa", 3)
        self.assertEqual(2, plan.marked)
        self.assertEqual(ReviewPlan("incremental", 3, 3, "b" * 40), plan)
        self.assertEqual(0, plan_review_pass([], "lisa", 3).marked)


if __name__ == "__main__":
    unittest.main()
