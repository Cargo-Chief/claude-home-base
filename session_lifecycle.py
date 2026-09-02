"""Pure selection rules for live Claude session lifecycle management."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
