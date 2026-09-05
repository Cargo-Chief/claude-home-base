"""Pure selection rules for live Claude session lifecycle management."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any, Callable


class TurnAdmission:
    """Bound concurrent turns while allowing callers to wait for capacity."""

    def __init__(self, limit: int):
        if limit < 1:
            raise ValueError("turn admission limit must be positive")
        self._semaphore = threading.BoundedSemaphore(limit)

    def acquire(self, on_queued: Callable[[], None] | None = None) -> bool:
        """Wait for a slot and return whether the caller had to queue."""
        if self._semaphore.acquire(blocking=False):
            return False
        if on_queued is not None:
            on_queued()
        self._semaphore.acquire()
        return True

    def release(self) -> None:
        self._semaphore.release()


def oldest_evictable_session(sessions: Mapping[str, Any]) -> str | None:
    """Return the oldest unlocked session; active turns are never evictable."""
    candidates = {
        thread: session
        for thread, session in sessions.items()
        if not session.turn_lock.locked()
    }
    if not candidates:
        return None
    return min(candidates, key=lambda thread: candidates[thread].last_activity)


def wait_for_turn_completion(
    done_event: Any,
    *,
    inactivity_timeout: float,
    activity_at: Callable[[], float],
    delegate_active: Callable[[], bool],
    clock: Callable[[], float] = time.monotonic,
    poll_interval: float = 1.0,
    max_duration: float | None = None,
) -> bool:
    """Wait until completion or a full period without observable progress.

    Governed delegates enforce their own call timeout and token allocation.
    While one is live, keep the owning turn available so the launcher can
    finish metering and return control. When it exits, give the owner a fresh
    inactivity window to consume that result.
    """
    if (inactivity_timeout <= 0 or poll_interval <= 0
            or (max_duration is not None and max_duration <= 0)):
        raise ValueError("turn wait intervals must be positive")
    observed_activity = activity_at()
    deadline = clock() + inactivity_timeout
    hard_deadline = clock() + max_duration if max_duration is not None else None
    delegate_was_active = False
    while True:
        now = clock()
        current_activity = activity_at()
        if current_activity > observed_activity:
            observed_activity = current_activity
            deadline = now + inactivity_timeout

        is_delegate_active = delegate_active()
        if is_delegate_active or delegate_was_active:
            deadline = now + inactivity_timeout
        delegate_was_active = is_delegate_active

        effective_deadline = min(deadline, hard_deadline) if hard_deadline else deadline
        remaining = effective_deadline - now
        if remaining <= 0:
            return bool(done_event.is_set())
        if done_event.wait(timeout=min(poll_interval, remaining)):
            return True


def owner_stream_event_is_activity(event: Mapping[str, Any]) -> bool:
    """Exclude echoed inbound text while retaining provider/tool progress."""
    if event.get("type") != "user":
        return True
    content = event.get("message", {}).get("content", [])
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def stop_timed_out_session(session: Any, interrupt) -> bool:
    """Silence a timed-out turn before interrupting it.

    The Slack request handler may stop waiting before the long-lived provider
    process exits. Clearing the callback first prevents that orphaned turn from
    posting late output after the timeout has been reported.
    """
    session._on_text = None
    session.pre_tool_text.clear()
    return interrupt(session)
