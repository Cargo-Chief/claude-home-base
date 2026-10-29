"""One Slack thread per ticket: the agent's one-shot request to move new work to a fresh thread."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cargo_chief_safety import SafetyError

MAX_CLAIM_BYTES = 16384
MAX_TITLE_CHARS = 120
MAX_BRIEF_CHARS = 6000


@dataclass(frozen=True)
class HandoffRequest:
    title: str
    brief: str


def read_handoff_claim(claim: Path) -> HandoffRequest:
    """Validate the claim file's JSON: a one-line title and a non-empty brief."""
    if not claim.is_file() or claim.is_symlink() or claim.stat().st_size > MAX_CLAIM_BYTES:
        raise SafetyError("handoff claim file is unsafe")
    try:
        data = json.loads(claim.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafetyError("handoff claim must be a JSON object") from exc
    if not isinstance(data, dict) or set(data) != {"title", "brief"}:
        raise SafetyError('handoff claim must be exactly {"title": ..., "brief": ...}')
    title, brief = data["title"], data["brief"]
    if not isinstance(title, str) or not isinstance(brief, str):
        raise SafetyError("handoff title and brief must be strings")
    title, brief = title.strip(), brief.strip()
    if not title or "\n" in title or len(title) > MAX_TITLE_CHARS:
        raise SafetyError(f"handoff title must be one line of 1-{MAX_TITLE_CHARS} characters")
    if not brief or len(brief) > MAX_BRIEF_CHARS:
        raise SafetyError(f"handoff brief must be 1-{MAX_BRIEF_CHARS} characters")
    return HandoffRequest(title=title, brief=brief)


def consume_handoff_claim(claim: Path) -> HandoffRequest | None:
    """Validate and delete the one-shot claim; None when the turn wrote none."""
    if not claim.exists() and not claim.is_symlink():
        return None
    try:
        return read_handoff_claim(claim)
    finally:
        discard_handoff_claim(claim)


def discard_handoff_claim(claim: Path) -> bool:
    """Delete a claim left by an earlier turn. Returns whether one was there."""
    try:
        claim.unlink()
    except FileNotFoundError:
        return False
    return True


def handoff_refusal(*, review: bool, from_handoff: bool) -> str | None:
    """Why this turn may not open a thread, or None when it may."""
    if review:
        return "a review turn cannot hand off"
    if from_handoff:
        return "a turn started by a handoff cannot hand off again; a person's message must start the next one"
    return None


def new_thread_text(request: HandoffRequest, *, source_link: str) -> str:
    return f"*{request.title}*\n_Handed off from {source_link}_\n\n{request.brief}"


def source_thread_text(request: HandoffRequest, *, new_link: str) -> str:
    return f"Moved *{request.title}* to its own thread: {new_link}"


def handoff_turn_message(request: HandoffRequest) -> str:
    return f"Handoff: {request.title}\n\n{request.brief}"
