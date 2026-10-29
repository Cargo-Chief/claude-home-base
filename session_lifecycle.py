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


class PendingTools:
    """The owner's tool calls issued and not yet answered; a quiet long-running tool is still progress.

    A subagent's own calls are left out: its Agent call is pending on the owner's side until it
    returns. A call stops counting once it has been outstanding for max_age, so a hung or orphaned
    call cannot hold the inactivity limit off for the rest of the turn.
    """

    MAX_AGE_SECONDS = 1800.0

    def __init__(self, max_age: float = MAX_AGE_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._issued: dict[str, float] = {}
        self._max_age = max_age
        self._clock = clock
        self._lock = threading.Lock()

    def observe(self, event: Mapping[str, Any]) -> None:
        if event.get("parent_tool_use_id"):
            return
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            return
        with self._lock:
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if event.get("type") == "assistant" and block.get("type") == "tool_use" and block.get("id"):
                    self._issued[str(block["id"])] = self._clock()
                elif event.get("type") == "user" and block.get("type") == "tool_result":
                    self._issued.pop(str(block.get("tool_use_id", "")), None)

    def active(self) -> bool:
        now = self._clock()
        with self._lock:
            return any(now - issued < self._max_age for issued in self._issued.values())

    def clear(self) -> None:
        """A finished or interrupted turn leaves no call outstanding."""
        with self._lock:
            self._issued.clear()


class BackgroundAgents:
    """Background subagents the owner started and has not yet been notified about.

    Only agent tasks count; a background shell or Monitor timer dies with the process rather than
    holding it open. Tasks the turn awaits, and tasks owned by a subagent, finish inside a turn. An
    agent stops counting after max_age, so a lost notification cannot hold the turn open.
    """

    MAX_AGE_SECONDS = 1800.0

    def __init__(self, max_age: float = MAX_AGE_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._running: dict[str, float] = {}
        self._max_age = max_age
        self._clock = clock
        self._lock = threading.Lock()

    def observe(self, event: Mapping[str, Any]) -> None:
        if event.get("type") != "system":
            return
        task_id = event.get("task_id")
        if not isinstance(task_id, str):
            return
        with self._lock:
            if event.get("subtype") == "task_started":
                if (event.get("task_type") == "local_agent"
                        and not (event.get("awaited") or event.get("owned_by_subagent") or event.get("ambient"))):
                    self._running[task_id] = self._clock()
            elif event.get("subtype") == "task_notification":
                self._running.pop(task_id, None)

    def running(self) -> bool:
        now = self._clock()
        with self._lock:
            return any(now - started < self._max_age for started in self._running.values())


def stop_timed_out_session(session: Any, interrupt) -> str:
    """Silence a timed-out turn before interrupting it.

    The Slack request handler may stop waiting before the long-lived provider
    process exits. Clearing the callback first prevents that orphaned turn from
    posting late output after the timeout has been reported.
    """
    session._on_text = None
    session.pre_tool_text.clear()
    return interrupt(session)
