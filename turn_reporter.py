"""Slack turn reporting: one live status line per turn, messages for humans, and a final reply.

Narration an agent writes between tool calls updates a single status message in place instead of
posting a new message each time. A text block that starts with a human marker is posted as its own
message. Text written after the turn's last tool call is the turn's reply and is posted when the
turn ends. A heartbeat keeps the status fresh and posts a short progress notice on long turns.

All Slack I/O happens outside the reporter's lock, so a slow Slack call never blocks the reader.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

# A text block starting with one of these is for a person and is always posted as a message.
QUESTION_MARKER = "❓"
MESSAGE_MARKER = "💬"
HUMAN_MARKERS = (QUESTION_MARKER, MESSAGE_MARKER)

STATUS_EDIT_INTERVAL = 5.0
STATUS_FIRST_AFTER = 60.0
STATUS_RETRY_AFTER = 30.0
PROGRESS_POST_INTERVAL = 20 * 60.0
STEP_MAX_CHARS = 200

Action = Callable[[], None]


def is_for_human(text: str) -> bool:
    return text.lstrip().startswith(HUMAN_MARKERS)


def plain(text: str) -> str:
    """Make agent text safe inside a mrkdwn status line: no formatting, links or mentions."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "".join(ch for ch in text if ch not in "*_`~")


def step_line(text: str) -> str:
    """The first non-empty line of a narration block, trimmed for a status line."""
    for line in text.splitlines():
        line = plain(line).strip()
        if line:
            return line if len(line) <= STEP_MAX_CHARS else line[: STEP_MAX_CHARS - 1] + "…"
    return ""


def format_elapsed(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return f"{int(seconds)}s"
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _run(actions: list[Action]) -> None:
    for action in actions:
        try:
            action()
        except Exception as exc:
            logger.warning(f"turn reporter Slack call failed: {exc}")


class TurnReporter:
    """Per-turn reporting state. Slack I/O is injected so the logic is testable.

    post: posts agent text as a message (the turn's on_text).
    post_notice: posts a harness notice that is not agent output.
    post_status / update_status: create and edit the live status line.
    """

    def __init__(
        self,
        post: Callable[[str], None],
        post_notice: Callable[[str], None],
        post_status: Callable[[str], str | None],
        update_status: Callable[[str, str], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._post = post
        self._post_notice = post_notice
        self._post_status = post_status
        self._update_status = update_status
        self._clock = clock
        self._lock = threading.Lock()
        self.active = False
        self.stopped = False
        self.seen_work = False
        self.status_ts: str | None = None
        self.status_creating = False
        self.status_retry_at = 0.0
        self.step = ""
        self.note = ""
        self.tail: list[str] = []
        self.started = self.last_post = self.last_activity = self.last_edit = 0.0

    def begin(self) -> None:
        with self._lock:
            now = self._clock()
            self.active = True
            self.stopped = False
            self.seen_work = False
            self.status_ts = None
            self.status_creating = False
            self.status_retry_at = 0.0
            self.step = ""
            self.note = ""
            self.tail = []
            self.started = self.last_post = self.last_activity = now
            self.last_edit = 0.0

    def text(self, text: str) -> None:
        """Route one assistant text block: to a person, or into the status and the reply tail."""
        actions: list[Action] = []
        with self._lock:
            if not self.active:
                return
            now = self._clock()
            self.last_activity = now
            self.seen_work = True
            if is_for_human(text):
                actions.append(lambda: self._post(text))
                self.last_post = now
            else:
                self.tail.append(text)
                line = step_line(text)
                if line:
                    self.step = line
                actions += self._render(now, force=self.status_ts is None)
        _run(actions)

    def tool_used(self) -> None:
        """Text before a tool call is narration, not the reply."""
        with self._lock:
            self.last_activity = self._clock()
            self.seen_work = True
            self.tail = []

    def activity(self) -> None:
        with self._lock:
            self.last_activity = self._clock()

    def set_note(self, note: str) -> None:
        with self._lock:
            if not self.active:
                return
            self.note = plain(note)
            actions = self._render(self._clock(), force=False)
        _run(actions)

    def mark_stopped(self) -> None:
        """The person stopped the turn: no reply is posted and the status reads stopped."""
        with self._lock:
            self.stopped = True

    def tick(self) -> None:
        """Heartbeat: refresh the status and post a progress notice on long, quiet turns."""
        actions: list[Action] = []
        with self._lock:
            if not self.active or not self.seen_work:
                return
            now = self._clock()
            if self.status_ts is None and now - self.started < STATUS_FIRST_AFTER:
                return
            actions += self._render(now, force=True)
            if now - self.last_post >= PROGRESS_POST_INTERVAL:
                step = f": {self.step}" if self.step else ""
                note = f"still working ({format_elapsed(now - self.started)}){step}"
                actions.append(lambda: self._post_notice(note))
                self.last_post = now
        _run(actions)

    def finish(self, *, post_reply: bool = True, state: str = "done") -> None:
        """End the turn: post the reply blocks and mark the status finished."""
        actions: list[Action] = []
        with self._lock:
            if not self.active:
                return
            self.active = False
            if self.stopped:
                post_reply, state = False, "stopped"
            if post_reply:
                for block in self.tail:
                    actions.append(lambda block=block: self._post(block))
            self.tail = []
            if self.status_ts:
                status_ts = self.status_ts
                mark = ":white_check_mark:" if state == "done" else ":octagonal_sign:"
                line = f"{mark} _{state} · {format_elapsed(self._clock() - self.started)}_"
                actions.append(lambda: self._update_status(status_ts, line))
        _run(actions)

    def _render(self, now: float, *, force: bool) -> list[Action]:
        """Build the status update under the lock; the caller runs the returned Slack action."""
        if not force and now - self.last_edit < STATUS_EDIT_INTERVAL:
            return []
        parts = [f"working · {format_elapsed(now - self.started)}"]
        if self.step:
            parts.append(self.step)
        quiet = now - self.last_activity
        if quiet >= 60:
            parts.append(f"last activity {format_elapsed(quiet)} ago")
        if self.note:
            parts.append(self.note)
        line = ":hourglass_flowing_sand: _" + " · ".join(parts) + "_"
        if self.status_ts is not None:
            self.last_edit = now
            status_ts = self.status_ts
            return [lambda: self._update_status(status_ts, line)]
        if self.status_creating or now < self.status_retry_at:
            return []
        self.status_creating = True
        self.last_edit = now
        return [lambda: self._create_status(line)]

    def _create_status(self, line: str) -> None:
        try:
            ts = self._post_status(line)
        except Exception as exc:
            ts = None
            logger.warning(f"turn reporter could not post status line: {exc}")
        with self._lock:
            self.status_creating = False
            if ts:
                self.status_ts = ts
            else:
                self.status_retry_at = self._clock() + STATUS_RETRY_AFTER


def tick_all(reporters: Iterable[tuple[TurnReporter, bool]]) -> None:
    """One heartbeat pass. A turn no longer running is finished; no reporter can stop the pass."""
    for reporter, running in reporters:
        try:
            if not reporter.active:
                continue
            if running:
                reporter.tick()
            else:
                reporter.finish(state="stopped")
        except Exception as exc:
            logger.warning(f"turn reporter heartbeat failed: {exc}")
