import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))

import cargo_chief_safety
from review_post import (
    REVIEW_PR_ENV,
    REVIEW_REQUESTER_LOGIN_ENV,
    REVIEW_SELF_LOGIN_ENV,
    ReviewPostError,
    admitted_pr,
    approval_refusal,
    build_review,
    is_this_app,
    list_threads,
    main,
    submit,
)

PR = "https://github.com/Cargo-Chief/c4/pull/4249"
HEAD = "abc123"
ENV = {
    REVIEW_SELF_LOGIN_ENV: "lisa-cargochief[bot]",
    REVIEW_REQUESTER_LOGIN_ENV: "kfranzenCC",
    REVIEW_PR_ENV: PR,
}
OPEN = {"state": "open", "draft": False, "user": {"login": "dev1", "type": "User"}}


def thread(tid, login="lisa-cargochief", typename="Bot", resolved=False, body="finding\nmore"):
    return {"id": tid, "isResolved": resolved, "path": "a.py", "line": 3,
            "comments": {"nodes": [{"author": {"login": login, "__typename": typename}, "body": body}]}}


class FakeGitHub:
    def __init__(self, pull=None, threads=(), heads=None, fail_resolve=(), fail_submit=False):
        self._pull = {**OPEN, **(pull or {})}
        self._threads = list(threads)
        self._heads = list(heads or [])
        self.fail_resolve = set(fail_resolve)
        self.fail_submit = fail_submit
        self.calls = []

    def pull(self, owner, repo, number):
        self.calls.append(("pull", owner, repo, number))
        head = self._heads.pop(0) if self._heads else HEAD
        return {**self._pull, "head": {"sha": head}}

    def threads(self, owner, repo, number):
        self.calls.append(("threads",))
        return self._threads

    def resolve(self, thread_id):
        self.calls.append(("resolve", thread_id))
        if thread_id in self.fail_resolve:
            raise ReviewPostError("graphql failed")

    def submit(self, owner, repo, number, review):
        self.calls.append(("submit", review["event"]))
        if self.fail_submit:
            raise ReviewPostError("422")
        return {"html_url": "https://github.com/x"}

    def names(self):
        return [call[0] if call[0] != "resolve" else f"resolve:{call[1]}" for call in self.calls]


def payload(comments=(), body="Summary"):
    return {"commit_id": HEAD, "body": body, "event": "APPROVE",
            "comments": [{"path": "a.py", "line": 3, "side": "RIGHT", "body": c, "extra": 1} for c in comments]}


class ReviewPostTest(unittest.TestCase):
    def test_env_names_match_the_harness(self):
        self.assertEqual(cargo_chief_safety.REVIEW_PR_ENV, REVIEW_PR_ENV)
        self.assertEqual(cargo_chief_safety.REVIEW_SELF_LOGIN_ENV, REVIEW_SELF_LOGIN_ENV)
        self.assertEqual(cargo_chief_safety.REVIEW_REQUESTER_LOGIN_ENV, REVIEW_REQUESTER_LOGIN_ENV)

    def test_posts_only_to_the_admitted_pr(self):
        self.assertEqual(("Cargo-Chief", "c4", 4249), admitted_pr(PR, ENV))
        self.assertEqual(("Cargo-Chief", "c4", 4249), admitted_pr("https://github.com/Cargo-Chief/C4/pull/04249", ENV))
        for other in ("https://github.com/Cargo-Chief/booking-agent/pull/900", "https://github.com/other/c4/pull/4249", "4249"):
            with self.subTest(other=other), self.assertRaisesRegex(ReviewPostError, "may post only to"):
                admitted_pr(other, ENV)
        with self.assertRaisesRegex(ReviewPostError, "not set"):
            admitted_pr(PR, {})

    def test_own_threads_need_the_bot_account(self):
        self.assertTrue(is_this_app({"login": "lisa-cargochief", "__typename": "Bot"}, "lisa-cargochief[bot]"))
        self.assertFalse(is_this_app({"login": "lisa-cargochief", "__typename": "User"}, "lisa-cargochief[bot]"))
        self.assertFalse(is_this_app({"login": "nedcc", "__typename": "Bot"}, "lisa-cargochief[bot]"))

    def test_review_takes_the_verdict_from_the_flag_and_keeps_only_comment_fields(self):
        review = build_review(payload(["fix this"]), "request_changes", HEAD)
        self.assertEqual("REQUEST_CHANGES", review["event"])
        self.assertEqual([{"path": "a.py", "line": 3, "side": "RIGHT", "body": "fix this"}], review["comments"])
        self.assertEqual("COMMENT", build_review(payload(), "comment", HEAD)["event"])
        self.assertEqual("APPROVE", build_review(payload(body=""), "approve", HEAD)["event"])

    def test_review_refuses_bad_payloads(self):
        cases = [
            (payload(), "comment", "other", "older commit"),
            (payload(["nit"]), "approve", HEAD, "cannot carry findings"),
            (payload(["fix"], body=" "), "request_changes", HEAD, "needs a review body"),
            (payload(body=""), "comment", HEAD, "needs a review body"),
            ({**payload(), "comments": [{"path": "a.py", "line": "3", "body": "x"}]}, "comment", HEAD, "integer line"),
            ({**payload(), "comments": [{"path": "a.py", "body": "x"}]}, "comment", HEAD, "integer line"),
            ({**payload(), "comments": [{"path": "a.py", "line": 3, "side": "up", "body": "x"}]}, "comment", HEAD, "LEFT or RIGHT"),
            ({**payload(), "comments": [{"path": "a.py", "line": 3, "start_line": 5, "body": "x"}]}, "comment", HEAD, "start_line"),
        ]
        for body, verdict, head, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ReviewPostError, message):
                build_review(body, verdict, head)

    def test_approval_refusals(self):
        ok = dict(pull=OPEN, login="lisa-cargochief[bot]", requester_login="kfranzenCC", open_own_threads=0)
        self.assertIsNone(approval_refusal(**ok))
        self.assertIsNone(approval_refusal(**{**ok, "pull": {**OPEN, "user": {"login": "kfranzenCC", "type": "User"}}}),
                          "a mapped requester may get an approval on their own PR")
        self.assertIn("no GitHub login", approval_refusal(**{**ok, "requester_login": ""}))
        self.assertIn("agent wrote", approval_refusal(**{**ok, "pull": {**OPEN, "user": {"login": "lisa-cargochief[bot]", "type": "Bot"}}}))
        self.assertIsNone(approval_refusal(**{**ok, "pull": {**OPEN, "user": {"login": "lisa-cargochief", "type": "User"}}}))
        self.assertIn("still open", approval_refusal(**{**ok, "open_own_threads": 2}))
        self.assertIn("ready-for-review", approval_refusal(**{**ok, "pull": {**OPEN, "draft": True}}))
        self.assertIn("ready-for-review", approval_refusal(**{**ok, "pull": {**OPEN, "state": "closed"}}))

    def test_submit_posts_the_review_before_resolving_threads(self):
        github = FakeGitHub(threads=[thread("T1"), thread("T2", login="nedcc"), thread("T3", resolved=True)])
        out = submit(github, PR, payload(), "approve", ["T1", "T3"], ENV)
        self.assertEqual(["pull", "threads", "pull", "submit", "resolve:T1"], github.names())
        self.assertIn("APPROVE review posted", out)
        self.assertIn("resolved 1 threads", out)

    def test_failed_submit_resolves_nothing(self):
        github = FakeGitHub(threads=[thread("T1")], fail_submit=True)
        with self.assertRaisesRegex(ReviewPostError, "422"):
            submit(github, PR, payload(["x"]), "request_changes", ["T1"], ENV)
        self.assertNotIn("resolve:T1", github.names())

    def test_failed_resolve_is_reported_after_the_review_posts(self):
        github = FakeGitHub(threads=[thread("T1"), thread("T2")], fail_resolve={"T1"})
        out = submit(github, PR, payload(), "comment", ["T1", "T2"], ENV)
        self.assertIn("resolved 1 threads", out)
        self.assertIn("could not resolve T1", out)

    def test_submit_refuses_approval_with_own_thread_left_open(self):
        github = FakeGitHub(threads=[thread("T1")])
        with self.assertRaisesRegex(ReviewPostError, "still open"):
            submit(github, PR, payload(), "approve", [], ENV)
        self.assertNotIn("submit", github.names())

    def test_submit_refuses_approval_when_the_head_moves(self):
        github = FakeGitHub(heads=[HEAD, "newer"])
        with self.assertRaisesRegex(ReviewPostError, "head moved"):
            submit(github, PR, payload(), "approve", [], ENV)
        self.assertNotIn("submit", github.names())

    def test_submit_refuses_a_closed_pr(self):
        with self.assertRaisesRegex(ReviewPostError, "no longer open"):
            submit(FakeGitHub(pull={"state": "closed"}), PR, payload(), "comment", [], ENV)

    def test_submit_refuses_to_resolve_someone_elses_thread(self):
        github = FakeGitHub(threads=[thread("T2", login="nedcc"), thread("T4", typename="User")])
        for tid in ("T2", "T4"):
            with self.subTest(tid=tid), self.assertRaisesRegex(ReviewPostError, "not threads this agent started"):
                submit(github, PR, payload(["x"]), "request_changes", [tid], ENV)
        self.assertNotIn("submit", github.names())

    def test_comment_without_resolve_skips_the_thread_query(self):
        github = FakeGitHub()
        submit(github, PR, payload(["x"]), "comment", [], ENV)
        self.assertEqual(["pull", "submit"], github.names())

    def test_submit_refuses_approval_for_unmapped_requester(self):
        with self.assertRaisesRegex(ReviewPostError, "no GitHub login"):
            submit(FakeGitHub(), PR, payload(), "approve", [], {**ENV, REVIEW_REQUESTER_LOGIN_ENV: ""})

    def test_threads_lists_only_own_and_survives_empty_bodies(self):
        github = FakeGitHub(threads=[thread("T1"), thread("T2", login="nedcc"), thread("T3", body="  ")])
        rows = [json.loads(line) for line in list_threads(github, PR, ENV).splitlines()]
        self.assertEqual(["T1", "T3"], [row["id"] for row in rows])
        self.assertEqual(["finding", ""], [row["first_line"] for row in rows])

    def test_repeated_resolve_flags_accumulate(self):
        github = FakeGitHub(threads=[thread("T1"), thread("T2")])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            path.write_text(json.dumps(payload()))
            code = main(["submit", PR, "--review", str(path), "--verdict", "comment",
                         "--resolve", "T1", "--resolve", "T2"], github=github, env=ENV)
        self.assertEqual(0, code)
        self.assertEqual(["resolve:T1", "resolve:T2"], [n for n in github.names() if n.startswith("resolve")])

    def test_main_reports_refusals_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            path.write_text("[]")
            self.assertEqual(2, main(["submit", PR, "--review", str(path), "--verdict", "comment"],
                                     github=FakeGitHub(), env=ENV))


if __name__ == "__main__":
    unittest.main()
