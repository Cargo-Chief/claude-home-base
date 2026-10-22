"""Slack turn reporting: one live status line per turn, messages for humans, and a final reply.

Narration an agent writes between tool calls updates a single status message in place instead of
posting a new message each time. A text block that starts with a human marker is posted as its own
message. Text written after the turn's last tool call is the turn's reply and is posted when the
turn ends. A heartbeat keeps the status fresh and posts a short progress note on long turns.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

# A text block starting with one of these is for a person and is always posted as a message.
QUESTION_MARKER = "❓"
MESSAGE_MARKER = "💬"
HUMAN_MARKERS = (QUESTION_MARKER, MESSAGE_MARKER)

STATUS_EDIT_INTERVAL = 5.0
STATUS_FIRST_AFTER = 60.0
PROGRESS_POST_INTERVAL = 20 * 60.0
STEP_MAX_CHARS = 200


def is_for_human(text: str) -> bool:
    return text.lstrip().startswith(HUMAN_MARKERS)


def step_line(text: str) -> str:
    """The first non-empty line of a narration block, trimmed for a status line."""
    for line in text.splitlines():
        line = line.strip().strip("*_").strip()
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


class TurnReporter:
    """Per-turn reporting state. Slack I/O is injected so the logic is testable."""

    def __init__(
        self,
        post: Callable[[str], None],
        post_status: Callable[[str], str | None],
        update_status: Callable[[str, str], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._post = post
        self._post_status = post_status
        self._update_status = update_status
        self._clock = clock
        self._lock = threading.Lock()
        self.active = False
        self.status_ts: str | None = None
        self.step = ""
        self.note = ""
        self.tail: list[str] = []
        self.started = self.last_post = self.last_activity = self.last_edit = 0.0

    def begin(self) -> None:
        with self._lock:
            now = self._clock()
            self.active = True
            self.status_ts = None
            self.step = ""
            self.note = ""
            self.tail = []
            self.started = self.last_post = self.last_activity = now
            self.last_edit = 0.0

    def text(self, text: str) -> None:
        """Route one assistant text block: to a person, or into the status and the reply tail."""
        with self._lock:
            if not self.active:
                return
            now = self._clock()
            self.last_activity = now
            if is_for_human(text):
                self._post(text)
                self.last_post = now
                return
            self.tail.append(text)
            line = step_line(text)
            if line:
                self.step = line
            self._render(now, force=self.status_ts is None)

    def tool_used(self) -> None:
        """Text before a tool call is narration, not the reply."""
        with self._lock:
            self.last_activity = self._clock()
            self.tail = []

    def activity(self) -> None:
        with self._lock:
            self.last_activity = self._clock()

    def set_note(self, note: str) -> None:
        with self._lock:
            if not self.active:
                return
            self.note = note
            self._render(self._clock(), force=False)

    def tick(self) -> None:
        """Heartbeat: refresh the status and post a progress note on long, quiet turns."""
        with self._lock:
            if not self.active:
                return
            now = self._clock()
            if self.status_ts is None and now - self.started < STATUS_FIRST_AFTER:
                return
            self._render(now, force=True)
            if now - self.last_post >= PROGRESS_POST_INTERVAL:
                step = f": {self.step}" if self.step else ""
                self._post(f"Still working ({format_elapsed(now - self.started)}){step}")
                self.last_post = now

    def finish(self, *, post_reply: bool = True, state: str = "done") -> None:
        """End the turn: post the reply tail and mark the status finished."""
        with self._lock:
            if not self.active:
                return
            self.active = False
            if post_reply and self.tail:
                self._post("\n\n".join(self.tail))
            self.tail = []
            if self.status_ts:
                elapsed = format_elapsed(self._clock() - self.started)
                mark = ":white_check_mark:" if state == "done" else ":octagonal_sign:"
                self._safe_update(f"{mark} _{state} · {elapsed}_")

    def _render(self, now: float, *, force: bool) -> None:
        if not force and now - self.last_edit < STATUS_EDIT_INTERVAL:
            return
        parts = [f"working · {format_elapsed(now - self.started)}"]
        if self.step:
            parts.append(self.step)
        quiet = now - self.last_activity
        if quiet >= 60:
            parts.append(f"last activity {format_elapsed(quiet)} ago")
        if self.note:
            parts.append(self.note)
        text = ":hourglass_flowing_sand: _" + " · ".join(parts) + "_"
        self.last_edit = now
        if self.status_ts is None:
            try:
                self.status_ts = self._post_status(text)
            except Exception:
                self.status_ts = None
        else:
            self._safe_update(text)

    def _safe_update(self, text: str) -> None:
        try:
            self._update_status(self.status_ts, text)
        except Exception:
            pass
