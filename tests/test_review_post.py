import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))

from review_post import (
    ReviewPostError,
    approval_refusal,
    build_review,
    list_threads,
    normalize_login,
    parse_pr_url,
    submit,
)

PR = "https://github.com/Cargo-Chief/c4/pull/4249"
HEAD = "abc123"
ENV = {"CARGO_CHIEF_REVIEW_SELF_LOGIN": "lisa-cargochief[bot]", "CARGO_CHIEF_REVIEW_REQUESTER_LOGIN": "kfranzenCC"}


def thread(tid, author, resolved=False):
    return {"id": tid, "isResolved": resolved, "path": "a.py", "line": 3,
            "comments": {"nodes": [{"author": {"login": author}, "body": "finding\nmore"}]}}


class FakeGitHub:
    def __init__(self, author="someone", threads=()):
        self.author = author
        self._threads = list(threads)
        self.resolved = []
        self.submitted = []

    def pull(self, owner, repo, number):
        return {"head": {"sha": HEAD}, "user": {"login": self.author}}

    def threads(self, owner, repo, number):
        return self._threads

    def resolve(self, thread_id):
        self.resolved.append(thread_id)

    def submit(self, owner, repo, number, review):
        self.submitted.append(review)
        return {"html_url": "https://github.com/x"}


def payload(comments=(), body="Summary"):
    return {"commit_id": HEAD, "body": body, "event": "APPROVE",
            "comments": [{"path": "a.py", "line": 3, "side": "RIGHT", "body": c, "extra": 1} for c in comments]}


class ReviewPostTest(unittest.TestCase):
    def test_logins_compare_by_bare_name(self):
        for spelling in ("lisa-cargochief[bot]", "app/lisa-cargochief", "Lisa-CargoChief"):
            self.assertEqual("lisa-cargochief", normalize_login(spelling))

    def test_pr_url_must_be_a_cargo_chief_pr(self):
        self.assertEqual(("Cargo-Chief", "c4", 4249), parse_pr_url(PR))
        for bad in ("https://github.com/other/c4/pull/1", "4249", PR + "/files"):
            with self.subTest(bad=bad), self.assertRaises(ReviewPostError):
                parse_pr_url(bad)

    def test_review_takes_the_verdict_from_the_flag_and_keeps_only_comment_fields(self):
        review = build_review(payload(["fix this"]), "request_changes", HEAD)
        self.assertEqual("REQUEST_CHANGES", review["event"])
        self.assertEqual([{"path": "a.py", "line": 3, "side": "RIGHT", "body": "fix this"}], review["comments"])
        self.assertEqual("COMMENT", build_review(payload(), "comment", HEAD)["event"])

    def test_review_refuses_stale_commit_findings_on_approval_and_empty_change_request(self):
        with self.assertRaisesRegex(ReviewPostError, "older commit"):
            build_review(payload(), "comment", "other")
        with self.assertRaisesRegex(ReviewPostError, "cannot carry findings"):
            build_review(payload(["nit"]), "approve", HEAD)
        with self.assertRaisesRegex(ReviewPostError, "needs a review body"):
            build_review(payload(["fix"], body=" "), "request_changes", HEAD)

    def test_approval_refusals(self):
        ok = dict(pr_author="dev1", self_login="lisa-cargochief[bot]", requester_login="kfranzenCC", open_own_threads=0)
        self.assertIsNone(approval_refusal(**ok))
        self.assertIn("no GitHub login", approval_refusal(**{**ok, "requester_login": ""}))
        self.assertIn("requester wrote", approval_refusal(**{**ok, "pr_author": "KFranzenCC"}))
        self.assertIn("agent wrote", approval_refusal(**{**ok, "pr_author": "app/lisa-cargochief"}))
        self.assertIn("still open", approval_refusal(**{**ok, "open_own_threads": 2}))

    def test_submit_resolves_own_threads_then_approves(self):
        github = FakeGitHub(threads=[thread("T1", "lisa-cargochief"), thread("T2", "nedcc"),
                                     thread("T3", "lisa-cargochief", resolved=True)])
        out = submit(github, PR, payload(), "approve", ["T1"], ENV)
        self.assertEqual(["T1"], github.resolved)
        self.assertEqual("APPROVE", github.submitted[0]["event"])
        self.assertIn("APPROVE review posted", out)

    def test_submit_refuses_approval_with_own_thread_left_open(self):
        github = FakeGitHub(threads=[thread("T1", "lisa-cargochief")])
        with self.assertRaisesRegex(ReviewPostError, "still open"):
            submit(github, PR, payload(), "approve", [], ENV)
        self.assertEqual([], github.resolved)
        self.assertEqual([], github.submitted)

    def test_submit_refuses_to_resolve_someone_elses_thread(self):
        github = FakeGitHub(threads=[thread("T2", "nedcc")])
        with self.assertRaisesRegex(ReviewPostError, "not one this agent started"):
            submit(github, PR, payload(["x"]), "request_changes", ["T2"], ENV)
        self.assertEqual([], github.resolved)

    def test_submit_refuses_approval_for_unmapped_requester_or_pr_author(self):
        env = {**ENV, "CARGO_CHIEF_REVIEW_REQUESTER_LOGIN": ""}
        with self.assertRaisesRegex(ReviewPostError, "no GitHub login"):
            submit(FakeGitHub(), PR, payload(), "approve", [], env)
        with self.assertRaisesRegex(ReviewPostError, "requester wrote"):
            submit(FakeGitHub(author="kfranzenCC"), PR, payload(), "approve", [], ENV)
        github = FakeGitHub(author="kfranzenCC")
        submit(github, PR, payload(["fix"]), "request_changes", [], ENV)
        self.assertEqual("REQUEST_CHANGES", github.submitted[0]["event"])

    def test_submit_needs_self_login(self):
        with self.assertRaisesRegex(ReviewPostError, "not set"):
            submit(FakeGitHub(), PR, payload(), "comment", [], {})

    def test_threads_lists_only_own(self):
        github = FakeGitHub(threads=[thread("T1", "lisa-cargochief[bot]"), thread("T2", "nedcc")])
        rows = [json.loads(line) for line in list_threads(github, PR, ENV).splitlines()]
        self.assertEqual([{"id": "T1", "state": "open", "path": "a.py", "line": 3, "first_line": "finding"}], rows)


if __name__ == "__main__":
    unittest.main()
