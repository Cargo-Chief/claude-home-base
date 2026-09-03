"""Pure selection rules for live Claude session lifecycle management."""

from __future__ import annotations

import threading
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


def stop_timed_out_session(session: Any, interrupt) -> bool:
    """Silence a timed-out turn before interrupting it.

    The Slack request handler may stop waiting before the long-lived provider
    process exits. Clearing the callback first prevents that orphaned turn from
    posting late output after the timeout has been reported.
    """
    session._on_text = None
    session.pre_tool_text.clear()
    return interrupt(session)
