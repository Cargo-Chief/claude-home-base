"""One Slack thread per ticket: the agent's one-shot request to move new work to a fresh thread."""

from __future__ import annotations

import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cargo_chief_safety import BUNDLE_NAME, SafetyError

MAX_CLAIM_BYTES = 16384
MAX_TITLE_CHARS = 120
MAX_BRIEF_CHARS = 6000


@dataclass(frozen=True)
class HandoffRequest:
    title: str
    brief: str
    bundle: str | None = None


def _read_claim_bytes(claim: Path) -> bytes:
    """Read the claim through one descriptor that refuses symlinks, non-files and oversize content."""
    try:
        fd = os.open(claim, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise SafetyError("handoff claim file is unsafe") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SafetyError("handoff claim file is unsafe")
        data = os.read(fd, MAX_CLAIM_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_CLAIM_BYTES:
        raise SafetyError("handoff claim file is unsafe")
    return data


def read_handoff_claim(claim: Path) -> HandoffRequest:
    """Validate the claim's JSON: a one-line title and a non-empty brief."""
    try:
        data = json.loads(_read_claim_bytes(claim).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafetyError("handoff claim must be a JSON object") from exc
    if not isinstance(data, dict) or set(data) not in ({"title", "brief"}, {"title", "brief", "bundle"}):
        raise SafetyError('handoff claim must be {"title": ..., "brief": ...} with an optional "bundle"')
    title, brief = data["title"], data["brief"]
    if not isinstance(title, str) or not isinstance(brief, str):
        raise SafetyError("handoff title and brief must be strings")
    title, brief = title.strip(), brief.strip()
    one_line = not any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in title)
    if not title or not one_line or len(title) > MAX_TITLE_CHARS:
        raise SafetyError(f"handoff title must be one line of 1-{MAX_TITLE_CHARS} characters")
    if not brief or len(brief) > MAX_BRIEF_CHARS:
        raise SafetyError(f"handoff brief must be 1-{MAX_BRIEF_CHARS} characters")
    bundle = data.get("bundle")
    if bundle is not None and (not isinstance(bundle, str) or not BUNDLE_NAME.fullmatch(bundle)):
        raise SafetyError("handoff bundle must be a CN-####-lowercase-slug worktree bundle name")
    return HandoffRequest(title=title, brief=brief, bundle=bundle)


def consume_handoff_claim(claim: Path) -> HandoffRequest | None:
    """Validate and delete the one-shot claim; None when the turn wrote none."""
    if not os.path.lexists(claim):
        return None
    try:
        return read_handoff_claim(claim)
    finally:
        discard_handoff_claim(claim)


def discard_handoff_claim(claim: Path) -> bool:
    """Delete a claim, including an empty directory left at its path. Never raises."""
    try:
        claim.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        try:
            claim.rmdir()
        except OSError:
            return False
    return True


def handoff_refusal(
    *,
    review: bool,
    from_handoff: bool,
    forwarded: bool = False,
    stopped: bool = False,
    senders: int = 1,
    delegation_blocked: bool = False,
) -> str | None:
    """Why this turn may not open a thread, or None when it may."""
    if review:
        return "a review turn cannot hand off"
    if from_handoff:
        return "a turn started by a handoff cannot hand off again; a person's message must start the next one"
    if stopped:
        return "the turn was stopped"
    if forwarded:
        return "a forwarded reply cannot hand off; ask in the thread"
    if senders > 1:
        return "more than one person wrote into this turn; ask for the handoff in a new message"
    if delegation_blocked:
        return "this thread's delegation budget is exhausted or awaiting verification; a named approver must reset it first"
    return None


def new_thread_refusal(
    *, send: bool, send_result: bool, channel: bool, thread: str | None, forward_to: str | None,
    with_votes: bool = False, env: Mapping[str, str],
) -> str | None:
    """Why a thread session may not open a new Slack thread from the CLI, or None when it may.

    A reply in an existing thread, and a DM whose reply is forwarded back to this session's own
    thread, stay allowed.
    """
    if not env.get("CARGO_CHIEF_THREAD_WORK_DIR") or thread:
        return None
    forwarded_home = bool(forward_to) and forward_to == env.get("CLAUDE_THREAD_TS") and not with_votes
    if channel or send_result or (send and not forwarded_home):
        return (
            "a thread session opens new work only through its handoff claim, which starts the new "
            "thread's session and moves its bundle"
        )
    return None


def slack_escape(text: str) -> str:
    """Escape Slack control characters so posted text cannot mention, link or broadcast."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def new_thread_text(request: HandoffRequest, *, source_link: str) -> str:
    return f"*{slack_escape(request.title)}*\n_Handed off from {source_link}_\n\n{slack_escape(request.brief)}"


def source_thread_text(request: HandoffRequest, *, new_link: str) -> str:
    return f"Moved *{slack_escape(request.title)}* to its own thread: {new_link}"


def handoff_turn_message(request: HandoffRequest) -> str:
    return f"Handoff brief you wrote (not the requester's words): {request.title}\n\n{request.brief}"
