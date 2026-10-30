"""The one route a review-only turn has for writing to GitHub.

The review task gives the exact command lines. Subcommands:
    threads <pr-url>    list this agent's review threads on the PR
    submit <pr-url> --review <review.json> --verdict comment|request_changes|approve [--resolve <id>...]

review.json may carry `replies` ([{"thread_id", "body"}]) on this agent's own threads; they post
after the review and before any thread is resolved. Each review body carries the pass marker the
harness reads to plan the next pass. A close-out adds findings only on changes beyond the fixes.

The room's permission rules deny the raw reviews and GraphQL endpoints because they cannot see a
payload file; this script reads the payload and enforces the rules in code. It posts only to the
PR the harness admitted and touches only threads this agent started. A comment or change request
posts first and then does its thread work; an approval does its thread work first and posts only
if all of it succeeded. It approves only an open, non-draft PR whose head is still the reviewed
commit, with no findings attached, none of this agent's own threads left open, a requester with a
mapped GitHub login, and a PR this agent did not write.

A review token may be refused a resolve. A thread this agent has answered, whose latest comment is
still its own reply, then counts as settled; the approval names it for the author to resolve.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Callable, Mapping, NamedTuple

from cargo_chief_safety import (
    REVIEW_BASE_ENV,
    REVIEW_MODE_ENV,
    REVIEW_PASS_ENV,
    REVIEW_PR_ENV,
    REVIEW_REQUESTER_LOGIN_ENV,
    REVIEW_SELF_LOGIN_ENV,
)
from review_requests import REVIEW_MARKER_RE, REVIEW_MODES, normalize_login, review_marker, single_pr_url

VERDICTS = {"comment": "COMMENT", "request_changes": "REQUEST_CHANGES", "approve": "APPROVE"}
SIDES = ("LEFT", "RIGHT")

THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved path line
          comments(first: 1) { totalCount nodes { author { __typename login } body } }
          latest: comments(last: 1) { nodes { author { __typename login } } }
        }
      }
    }
  }
}
"""
REPLY_MUTATION = (
    "mutation($id: ID!, $body: String!) { addPullRequestReviewThreadReply("
    "input: {pullRequestReviewThreadId: $id, body: $body}) { comment { id } } }"
)
RESOLVE_MUTATION = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"


class ReviewPostError(Exception):
    pass


def admitted_pr(pr_url: str, env: Mapping[str, str]) -> tuple[str, str, int]:
    """Owner, repo and number of the PR, which must be the one the harness admitted for this turn."""
    admitted = env.get(REVIEW_PR_ENV, "")
    if not admitted:
        raise ReviewPostError(f"{REVIEW_PR_ENV} is not set; run this from a review turn")
    canonical = single_pr_url(pr_url.strip())
    if canonical is None or canonical != admitted:
        raise ReviewPostError(f"this turn may post only to {admitted}")
    _, _, _, owner, repo, _, number = canonical.split("/")
    return owner, repo, int(number)


def self_login(env: Mapping[str, str]) -> str:
    login = env.get(REVIEW_SELF_LOGIN_ENV, "")
    if not login:
        raise ReviewPostError(f"{REVIEW_SELF_LOGIN_ENV} is not set; run this from a review turn")
    return login


def is_this_app(author: Mapping, login: str) -> bool:
    """A GraphQL author is this agent only when it is a Bot; a user may hold the App's bare name."""
    return author.get("__typename") == "Bot" and normalize_login(author.get("login") or "") == normalize_login(login)


class GitHub:
    def __init__(self, runner: Callable = subprocess.run):
        self._runner = runner

    def _gh(self, args: list[str], payload: dict | None = None) -> object:
        result = self._runner(
            ["gh", *args], input=json.dumps(payload) if payload is not None else None,
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise ReviewPostError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()[:1000]}")
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

    def reply(self, thread_id: str, body: str) -> None:
        self._gh(["api", "graphql", "--input", "-"], {"query": REPLY_MUTATION, "variables": {"id": thread_id, "body": body}})

    def resolve(self, thread_id: str) -> None:
        self._gh(["api", "graphql", "--input", "-"], {"query": RESOLVE_MUTATION, "variables": {"id": thread_id}})

    def compare_status(self, owner: str, repo: str, base: str, head: str) -> str:
        return self._gh(["api", f"repos/{owner}/{repo}/compare/{base}...{head}", "--jq", "{status: .status}"])["status"]

    def submit(self, owner: str, repo: str, number: int, review: dict) -> Mapping:
        return self._gh(["api", f"repos/{owner}/{repo}/pulls/{number}/reviews", "--input", "-"], review)


def _first_comment(thread: Mapping) -> Mapping:
    nodes = (thread.get("comments") or {}).get("nodes") or []
    return nodes[0] if nodes else {}


def has_answered(thread: Mapping, login: str) -> bool:
    """This agent replied on its thread and its reply is still the latest comment."""
    comments = thread.get("comments") or {}
    latest = ((thread.get("latest") or {}).get("nodes") or [{}])[0]
    return (comments.get("totalCount") or 0) > 1 and is_this_app(latest.get("author") or {}, login)


# GitHub's answer when a token may not resolve; anything else (5xx, rate limit) stays a failure.
_REFUSAL_MARKERS = ("resource not accessible", "does not have permission", "forbidden")
_RETRYABLE_MARKERS = ("rate limit", "abuse", "try again")


def _refused(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(m in text for m in _REFUSAL_MARKERS) and not any(m in text for m in _RETRYABLE_MARKERS)


def _reason(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text.split("failed: ", 1)[-1][:300]


def own_threads(github: GitHub, owner: str, repo: str, number: int, login: str) -> list[Mapping]:
    return [
        thread for thread in github.threads(owner, repo, number)
        if is_this_app(_first_comment(thread).get("author") or {}, login)
    ]


def _clean_comment(comment: object) -> dict:
    if not isinstance(comment, Mapping):
        raise ReviewPostError("every inline comment must be an object")
    path, body, line = comment.get("path"), comment.get("body"), comment.get("line")
    if not isinstance(path, str) or not path or not isinstance(body, str) or not body.strip():
        raise ReviewPostError("every inline comment needs a path and a body")
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise ReviewPostError(f"comment on {path} needs an integer line in the new file")
    cleaned = {"path": path, "line": line, "side": comment.get("side", "RIGHT"), "body": body}
    if cleaned["side"] not in SIDES:
        raise ReviewPostError(f"comment on {path} has side {cleaned['side']!r}; use LEFT or RIGHT")
    start = comment.get("start_line")
    if start is not None:
        if isinstance(start, bool) or not isinstance(start, int) or not 1 <= start < line:
            raise ReviewPostError(f"comment on {path} needs start_line below line")
        cleaned["start_line"] = start
        cleaned["start_side"] = comment.get("start_side", cleaned["side"])
        if cleaned["start_side"] not in SIDES:
            raise ReviewPostError(f"comment on {path} has an invalid start_side")
    return cleaned


def review_pass(env: Mapping[str, str]) -> tuple[str, int]:
    """The mode and pass number the harness set for this turn."""
    mode, pass_no = env.get(REVIEW_MODE_ENV, ""), env.get(REVIEW_PASS_ENV, "")
    if mode not in REVIEW_MODES or not pass_no.isdigit() or int(pass_no) < 1:
        raise ReviewPostError(f"{REVIEW_MODE_ENV} and {REVIEW_PASS_ENV} are not set; run this from a review turn")
    return mode, int(pass_no)


def thread_replies(payload: Mapping) -> list[tuple[str, str]]:
    replies = payload.get("replies") or []
    if not isinstance(replies, list):
        raise ReviewPostError("review.json replies must be a list")
    cleaned = []
    for item in replies:
        if not isinstance(item, Mapping) or not isinstance(item.get("thread_id"), str) \
                or not isinstance(item.get("body"), str) or not item["body"].strip():
            raise ReviewPostError("every reply needs a thread_id and a body")
        cleaned.append((item["thread_id"], item["body"]))
    return cleaned


def build_review(payload: Mapping, verdict: str, head_sha: str) -> dict:
    """The review to submit: the payload's body and inline comments under the chosen verdict."""
    if payload.get("commit_id") != head_sha:
        raise ReviewPostError("the review was written against an older commit; review the current head")
    body = payload.get("body") or ""
    comments = payload.get("comments") or []
    if not isinstance(body, str) or not isinstance(comments, list):
        raise ReviewPostError("review.json needs a string body and a list of comments")
    cleaned = [_clean_comment(comment) for comment in comments]
    if verdict != "approve" and not body.strip():
        raise ReviewPostError("a comment or change request needs a review body")
    if verdict == "approve" and cleaned:
        raise ReviewPostError("an approval cannot carry findings; request changes or comment instead")
    review = {"commit_id": head_sha, "event": VERDICTS[verdict], "body": body}
    if cleaned:
        review["comments"] = cleaned
    return review


def approval_refusal(*, pull: Mapping, login: str, requester_login: str, open_own_threads: int) -> str | None:
    """Why this review may not approve, or None."""
    if pull.get("state") != "open" or pull.get("draft"):
        return "only an open, ready-for-review PR can be approved"
    if not requester_login:
        return "the requester has no GitHub login in peer_review.github_logins, so this review cannot approve"
    author = pull.get("user") or {}
    if author.get("type") == "Bot" and normalize_login(author.get("login") or "") == normalize_login(login):
        return "this agent wrote this PR, so it cannot approve it"
    if open_own_threads:
        return f"{open_own_threads} of this agent's earlier threads are still open; resolve the fixed ones first"
    return None


class ThreadWork(NamedTuple):
    replied: int
    resolved: int
    failed: list[str]
    refused: list[tuple[str, str]]  # (thread id, GitHub's text) for resolves refused for lack of permission


def _thread_work(
    github: GitHub, replies: list[tuple[str, str]], resolve_ids: list[str], mine: Mapping[str, Mapping],
) -> ThreadWork:
    """Post the replies, then resolve; a thread whose reply failed stays open."""
    failed, refused, unreplied = [], [], set()
    replied = resolved = 0
    for thread_id, body in replies:
        try:
            github.reply(thread_id, body)
            replied += 1
        except Exception as exc:
            failed.append(f"reply on {thread_id}: {_reason(exc)}")
            unreplied.add(thread_id)
    for thread_id in resolve_ids:
        if mine[thread_id].get("isResolved"):
            continue
        if thread_id in unreplied:
            failed.append(f"resolve {thread_id} (skipped: its reply failed)")
            continue
        try:
            github.resolve(thread_id)
            resolved += 1
        except Exception as exc:
            if _refused(exc):
                refused.append((thread_id, _reason(exc)))
            else:
                failed.append(f"resolve {thread_id}: {_reason(exc)}")
    return ThreadWork(replied, resolved, failed, refused)


def _settle_refused(
    github: GitHub, owner: str, repo: str, number: int, login: str, work: ThreadWork,
) -> tuple[list[str], list[str]]:
    """Refused resolves, read fresh: (threads this agent has answered, failures for the rest)."""
    if not work.refused:
        return [], []
    try:
        fresh = {thread["id"]: thread for thread in own_threads(github, owner, repo, number, login)}
    except Exception as exc:
        raise ReviewPostError(
            f"could not re-read the threads after posting {work.replied} replies (they are posted; "
            f"do not post them again): {_reason(exc)}"
        ) from exc
    answered, unanswered = [], []
    for tid, reason in work.refused:
        if tid in fresh and has_answered(fresh[tid], login):
            answered.append(tid)
        else:
            unanswered.append(f"resolve {tid}: {reason}; your reply is not the thread's latest comment")
    return answered, unanswered


def submit(
    github: GitHub, pr_url: str, payload: Mapping, verdict: str, resolve_ids: list[str],
    env: Mapping[str, str],
) -> str:
    login = self_login(env)
    owner, repo, number = admitted_pr(pr_url, env)
    mode, pass_no = review_pass(env)
    pull = github.pull(owner, repo, number)
    if pull.get("state") != "open":
        raise ReviewPostError("the PR is no longer open")
    head_sha = pull["head"]["sha"]
    review = build_review(payload, verdict, head_sha)
    # Only the trailing marker is genuine; the model's text may quote one from the PR.
    body = REVIEW_MARKER_RE.sub("", review["body"]).strip()
    # A close-out that only names disputes reviewed nothing new, so it keeps the base.
    disputes_only = mode == "closeout" and verdict == "comment" and not review.get("comments")
    marked_head = env.get(REVIEW_BASE_ENV, "") or head_sha if disputes_only else head_sha
    review["body"] = f"{body}\n\n{review_marker(pass_no, mode, marked_head)}".lstrip()
    replies = thread_replies(payload)
    resolve_ids = list(dict.fromkeys(resolve_ids))

    mine: dict[str, Mapping] = {}
    if resolve_ids or replies or verdict == "approve":
        mine = {thread["id"]: thread for thread in own_threads(github, owner, repo, number, login)}
    unknown = [thread_id for thread_id in [*resolve_ids, *(tid for tid, _ in replies)] if thread_id not in mine]
    if unknown:
        raise ReviewPostError(f"not threads this agent started on this PR: {', '.join(unknown)}")
    if verdict == "approve":
        still_open = sum(1 for tid, t in mine.items() if not t.get("isResolved") and tid not in resolve_ids)
        refusal = approval_refusal(
            pull=pull, login=login, requester_login=env.get(REVIEW_REQUESTER_LOGIN_ENV, ""),
            open_own_threads=still_open,
        )
        if refusal:
            raise ReviewPostError(refusal)
        base = env.get(REVIEW_BASE_ENV, "")
        if mode == "closeout" and (not base or github.compare_status(owner, repo, base, head_sha) not in ("ahead", "identical")):
            raise ReviewPostError("the branch was rewritten since your last pass, so a close-out cannot approve it")

    if verdict == "approve":
        # The approval lands only after every reply and resolve it depends on has.
        work = _thread_work(github, replies, resolve_ids, mine)
        left, unanswered = _settle_refused(github, owner, repo, number, login, work)
        failed = [*work.failed, *unanswered]
        if failed:
            raise ReviewPostError(f"approval not posted: {', '.join(failed)}")
        if github.pull(owner, repo, number)["head"]["sha"] != head_sha:
            raise ReviewPostError("the PR head moved during posting; review the current head")
        if left:
            text, marker = review["body"].rsplit("\n\n", 1) if "\n\n" in review["body"] else ("", review["body"])
            review["body"] = f"{text}\n\n{_left_note(left, mine)}\n\n{marker}".lstrip()
        result = github.submit(owner, repo, number, review)
    else:
        result = github.submit(owner, repo, number, review)
        # The review is posted; from here a failure is reported, never raised as a refusal.
        work = _thread_work(github, replies, resolve_ids, mine)
        try:
            left, unanswered = _settle_refused(github, owner, repo, number, login, work)
        except ReviewPostError as exc:
            left, unanswered = [], [str(exc)]
        failed = [*work.failed, *unanswered]
    message = (
        f"{review['event']} review posted for pass {pass_no} ({mode}): "
        f"{(result or {}).get('html_url', '')} ({work.replied} replies, resolved {work.resolved} threads)"
    )
    if failed:
        message += f"; failed: {', '.join(failed)}"
    if left:
        reasons = dict(work.refused)
        message += ("; answered but GitHub refused the resolve, so the author must resolve: "
                    + ", ".join(f"{tid} ({reasons[tid]})" for tid in left))
    return message


def _left_note(left: list[str], mine: Mapping[str, Mapping]) -> str:
    """The note an approval carries for threads it answered but could not resolve."""
    where = [f"`{mine[tid].get('path')}" + (f":{mine[tid]['line']}`" if mine[tid].get("line") else "` (outdated)")
             for tid in left]
    return (f"{len(left)} of my threads are settled but GitHub would not let this review resolve them; "
            f"please resolve: {', '.join(where)}.")


def list_threads(github: GitHub, pr_url: str, env: Mapping[str, str]) -> str:
    login = self_login(env)
    owner, repo, number = admitted_pr(pr_url, env)
    rows = []
    for thread in own_threads(github, owner, repo, number, login):
        first = ((_first_comment(thread).get("body") or "").strip().splitlines() or [""])[0][:100]
        state = "resolved" if thread.get("isResolved") else "answered" if has_answered(thread, login) else "open"
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
    post.add_argument("--resolve", nargs="+", action="extend", default=[], help="IDs of this agent's threads now fixed")
    args = parser.parse_args(argv)
    github = github or GitHub()
    env = os.environ if env is None else env
    try:
        if args.command == "threads":
            print(list_threads(github, args.pr_url, env))
        else:
            with open(args.review, encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, Mapping):
                raise ReviewPostError("review.json must be an object")
            print(submit(github, args.pr_url, payload, args.verdict, args.resolve, env))
    except (ReviewPostError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
