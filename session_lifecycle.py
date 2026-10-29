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


class BackgroundTasks:
    """The owner's background tasks still running, and whether a finished one has yet to wake it.

    A background task reports through a task_notification, often after the owner's turn has ended;
    the CLI then starts a new turn to consume it, so busy() holds until that turn's result. Tasks the
    turn awaits, and tasks owned by a subagent, finish inside a turn and are left out.
    """

    def __init__(self) -> None:
        self._running: set[str] = set()
        self._wake_pending = False
        self._lock = threading.Lock()

    def observe(self, event: Mapping[str, Any]) -> None:
        event_type = event.get("type")
        subtype = event.get("subtype")
        task_id = event.get("task_id")
        with self._lock:
            if event_type == "result":
                self._wake_pending = False
            elif event_type != "system" or not isinstance(task_id, str):
                return
            elif subtype == "task_started":
                if not (event.get("awaited") or event.get("owned_by_subagent") or event.get("ambient")):
                    self._running.add(task_id)
            elif subtype == "task_notification" and task_id in self._running:
                self._running.discard(task_id)
                self._wake_pending = True

    def busy(self) -> bool:
        with self._lock:
            return bool(self._running) or self._wake_pending


def await_background_settle(
    tasks: BackgroundTasks,
    done_event: Any,
    *,
    alive: Callable[[], bool],
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    poll_interval: float = 5.0,
) -> bool:
    """Hold a finished turn open until its background tasks report and the woken turn ends.

    Returns False when the process exits or the deadline passes with work still outstanding.
    """
    while tasks.busy():
        if not alive():
            return False
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        done_event.clear()
        if tasks.busy():
            done_event.wait(timeout=min(poll_interval, remaining))
    return True


def stop_timed_out_session(session: Any, interrupt) -> str:
    """Silence a timed-out turn before interrupting it.

    The Slack request handler may stop waiting before the long-lived provider
    process exits. Clearing the callback first prevents that orphaned turn from
    posting late output after the timeout has been reported.
    """
    session._on_text = None
    session.pre_tool_text.clear()
    return interrupt(session)
