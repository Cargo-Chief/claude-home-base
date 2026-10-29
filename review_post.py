"""The one route a review-only turn has for writing to GitHub.

Run under the GitHub App wrapper:
    python3 docs/scripts/ops/with_github_app_token.py -- python3 claude-home-base/review_post.py threads <pr-url>
    python3 docs/scripts/ops/with_github_app_token.py -- python3 claude-home-base/review_post.py submit <pr-url> \
        --review <review.json> --verdict comment|request_changes|approve [--resolve <thread-id> ...]

The room's permission rules deny the raw reviews and GraphQL endpoints because they cannot see a
payload file; this script reads the payload and enforces the rules in code. It submits one review
with its inline comments, resolves only threads this agent started, and approves only when the
review has no findings, none of its own earlier threads are open, the reviewed commit is still the
head, and the requester is a known GitHub login who did not write the PR.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Callable, Mapping

SELF_LOGIN_ENV = "CARGO_CHIEF_REVIEW_SELF_LOGIN"
REQUESTER_LOGIN_ENV = "CARGO_CHIEF_REVIEW_REQUESTER_LOGIN"
VERDICTS = {"comment": "COMMENT", "request_changes": "REQUEST_CHANGES", "approve": "APPROVE"}
COMMENT_KEYS = ("path", "line", "side", "start_line", "start_side", "body")
_PR_URL_RE = re.compile(r"https://github\.com/(Cargo-Chief)/([A-Za-z0-9_.-]+)/pull/([0-9]+)")
_THREAD_ID_RE = re.compile(r"[A-Za-z0-9_=-]+")

THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved path line
          comments(first: 1) { nodes { author { login } body } }
        }
      }
    }
  }
}
"""
RESOLVE_MUTATION = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"


class ReviewPostError(Exception):
    pass


def normalize_login(login: str) -> str:
    """GitHub spells an App as app/name, name[bot] or name depending on the API; compare bare names."""
    login = login.strip().lower()
    if login.startswith("app/"):
        login = login[len("app/"):]
    return login.removesuffix("[bot]")


def parse_pr_url(url: str) -> tuple[str, str, int]:
    match = _PR_URL_RE.fullmatch(url.strip())
    if not match:
        raise ReviewPostError("the PR must be a https://github.com/Cargo-Chief/<repo>/pull/<n> URL")
    return match.group(1), match.group(2), int(match.group(3))


class GitHub:
    def __init__(self, runner: Callable = subprocess.run):
        self._runner = runner

    def _gh(self, args: list[str], payload: dict | None = None) -> object:
        result = self._runner(
            ["gh", *args], input=json.dumps(payload) if payload is not None else None,
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise ReviewPostError(f"gh {args[0]} {args[1] if len(args) > 1 else ''} failed: {result.stderr.strip()[:300]}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def pull(self, owner: str, repo: str, number: int) -> Mapping:
        return self._gh(["api", f"repos/{owner}/{repo}/pulls/{number}"])

    def threads(self, owner: str, repo: str, number: int) -> list[Mapping]:
        threads, cursor = [], None
        while True:
            variables = {"owner": owner, "repo": repo, "number": number, "cursor": cursor}
            data = self._gh(["api", "graphql", "--input", "-"], {"query": THREADS_QUERY, "variables": variables})
            page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            threads.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return threads
            cursor = page["pageInfo"]["endCursor"]

    def resolve(self, thread_id: str) -> None:
        self._gh(["api", "graphql", "--input", "-"], {"query": RESOLVE_MUTATION, "variables": {"id": thread_id}})

    def submit(self, owner: str, repo: str, number: int, review: dict) -> Mapping:
        return self._gh(["api", f"repos/{owner}/{repo}/pulls/{number}/reviews", "--input", "-"], review)


def _thread_author(thread: Mapping) -> str:
    nodes = (thread.get("comments") or {}).get("nodes") or []
    author = (nodes[0].get("author") or {}) if nodes else {}
    return normalize_login(author.get("login") or "")


def own_threads(github: GitHub, owner: str, repo: str, number: int, self_login: str) -> list[Mapping]:
    me = normalize_login(self_login)
    return [thread for thread in github.threads(owner, repo, number) if _thread_author(thread) == me]


def build_review(payload: Mapping, verdict: str, head_sha: str) -> dict:
    """The review to submit: the payload's body and inline comments under the chosen verdict."""
    if payload.get("commit_id") != head_sha:
        raise ReviewPostError("the review was written against an older commit; review the current head")
    body = payload.get("body") or ""
    comments = payload.get("comments") or []
    if not isinstance(body, str) or not isinstance(comments, list):
        raise ReviewPostError("review.json needs a string body and a list of comments")
    cleaned = []
    for comment in comments:
        if not isinstance(comment, Mapping) or not comment.get("path") or not comment.get("body"):
            raise ReviewPostError("every inline comment needs a path and a body")
        cleaned.append({key: comment[key] for key in COMMENT_KEYS if key in comment})
    if verdict == "request_changes" and not body.strip():
        raise ReviewPostError("requesting changes needs a review body saying what to change")
    if verdict == "approve" and cleaned:
        raise ReviewPostError("an approval cannot carry findings; request changes or comment instead")
    review = {"commit_id": head_sha, "event": VERDICTS[verdict], "body": body}
    if cleaned:
        review["comments"] = cleaned
    return review


def approval_refusal(
    *, pr_author: str, self_login: str, requester_login: str, open_own_threads: int,
) -> str | None:
    """Why this review may not approve, or None."""
    if not requester_login:
        return "the requester has no GitHub login in peer_review.github_logins, so this review cannot approve"
    if normalize_login(pr_author) == normalize_login(requester_login):
        return "the requester wrote this PR, so this review cannot approve it"
    if normalize_login(pr_author) == normalize_login(self_login):
        return "this agent wrote this PR, so it cannot approve it"
    if open_own_threads:
        return f"{open_own_threads} of this agent's earlier threads are still open; resolve the fixed ones first"
    return None


def submit(
    github: GitHub, pr_url: str, payload: Mapping, verdict: str, resolve_ids: list[str],
    env: Mapping[str, str],
) -> str:
    self_login = env.get(SELF_LOGIN_ENV, "")
    if not self_login:
        raise ReviewPostError(f"{SELF_LOGIN_ENV} is not set; run this from a review turn")
    owner, repo, number = parse_pr_url(pr_url)
    pull = github.pull(owner, repo, number)
    head_sha = pull["head"]["sha"]
    review = build_review(payload, verdict, head_sha)

    mine = {thread["id"]: thread for thread in own_threads(github, owner, repo, number, self_login)}
    for thread_id in resolve_ids:
        if not _THREAD_ID_RE.fullmatch(thread_id) or thread_id not in mine:
            raise ReviewPostError(f"thread {thread_id} is not one this agent started on this PR")
    if verdict == "approve":
        still_open = sum(1 for tid, t in mine.items() if not t.get("isResolved") and tid not in resolve_ids)
        refusal = approval_refusal(
            pr_author=pull["user"]["login"], self_login=self_login,
            requester_login=env.get(REQUESTER_LOGIN_ENV, ""), open_own_threads=still_open,
        )
        if refusal:
            raise ReviewPostError(refusal)

    for thread_id in resolve_ids:
        if not mine[thread_id].get("isResolved"):
            github.resolve(thread_id)
    result = github.submit(owner, repo, number, review)
    return f"{review['event']} review posted: {result.get('html_url', '')} (resolved {len(resolve_ids)} threads)"


def list_threads(github: GitHub, pr_url: str, env: Mapping[str, str]) -> str:
    self_login = env.get(SELF_LOGIN_ENV, "")
    if not self_login:
        raise ReviewPostError(f"{SELF_LOGIN_ENV} is not set; run this from a review turn")
    owner, repo, number = parse_pr_url(pr_url)
    rows = []
    for thread in own_threads(github, owner, repo, number, self_login):
        first = thread["comments"]["nodes"][0]["body"].splitlines()[0][:100] if thread["comments"]["nodes"] else ""
        state = "resolved" if thread.get("isResolved") else "open"
        rows.append(json.dumps({"id": thread["id"], "state": state, "path": thread.get("path"),
                                "line": thread.get("line"), "first_line": first}))
    return "\n".join(rows) or "no threads from this agent on this PR"


def main(argv: list[str] | None = None, github: GitHub | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post a review-only turn's review")
    sub = parser.add_subparsers(dest="command", required=True)
    threads = sub.add_parser("threads", help="list this agent's review threads on a PR")
    threads.add_argument("pr_url")
    post = sub.add_parser("submit", help="submit one review with its verdict")
    post.add_argument("pr_url")
    post.add_argument("--review", required=True, help="review.json: commit_id, body, comments")
    post.add_argument("--verdict", required=True, choices=sorted(VERDICTS))
    post.add_argument("--resolve", nargs="*", default=[], help="IDs of this agent's threads now fixed")
    args = parser.parse_args(argv)
    github = github or GitHub()
    env = os.environ if env is None else env
    try:
        if args.command == "threads":
            print(list_threads(github, args.pr_url, env))
        else:
            with open(args.review, encoding="utf-8") as handle:
                payload = json.load(handle)
            print(submit(github, args.pr_url, payload, args.verdict, args.resolve, env))
    except (ReviewPostError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
