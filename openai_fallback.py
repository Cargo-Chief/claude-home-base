"""Governed one-turn Codex execution for Claude credit-limit fallback."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
import threading
import time
from typing import Callable, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from session_lifecycle import wait_for_turn_completion


CLAUDE_LIMIT_RE = re.compile(
    r"You've hit your (?:(?:usage|weekly|monthly(?: spend)?|session|daily|\d+-hour) )?limit",
    re.IGNORECASE,
)
LIMIT_RESET_RE = re.compile(
    r"resets\s+(?:at\s+)?(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*(am|pm)(?:\s*\(([^)]+)\))?", re.IGNORECASE,
)
LIMIT_RESET_BUFFER = 120  # seconds, so a resume never lands a minute before the limit lifts
LIMIT_RESET_GRACE = timedelta(minutes=5)  # a reset this recent has just passed; it is not tomorrow's
# The model name the Claude CLI puts on messages it writes itself, such as a usage-limit notice
SYNTHETIC_MODEL = "<synthetic>"


def is_claude_limit_notice(text: str) -> bool:
    """Return whether Claude emitted an account usage-limit notice."""
    return CLAUDE_LIMIT_RE.search(text) is not None


def limit_reset_epoch(text: str, now: datetime | None = None) -> float | None:
    """The next 'resets 4pm (Zone)' time as an epoch, read in that zone (else local time); None if absent."""
    match = LIMIT_RESET_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
    minute = int(match.group(2) or 0)
    zone = None
    if match.group(4):
        try:
            zone = ZoneInfo(match.group(4).strip())
        except Exception:
            zone = None
    if zone is not None:
        current = (now or datetime.now(timezone.utc)).astimezone(zone)
    else:
        # Naive local time, so the conversion below applies the host's DST rules for the reset date.
        current = now or datetime.now()
    reset = current.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    if reset <= current - LIMIT_RESET_GRACE:
        reset += timedelta(days=1)
    elif reset < current:
        reset = current
    return reset.timestamp() + LIMIT_RESET_BUFFER


def fallback_error_kind(error: str | None) -> str:
    """Return a content-free failure category safe for service logs."""
    return {
        "Codex fallback timed out": "timeout",
        "Codex fallback could not start": "start",
    }.get(error, "provider")


def model_notice_text(model: str, previous: str = "") -> str | None:
    """Build truthful model attribution, excluding CLI synthetic envelopes."""
    if not model or model == SYNTHETIC_MODEL or model == previous:
        return None
    if previous:
        return f"model changed: {previous} → {model}"
    return f"model: {model}"


@dataclass
class CodexTurnResult:
    session_id: str | None = None
    texts: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error: str | None = None


def parse_codex_events(lines: Iterable[str]) -> CodexTurnResult:
    """Extract only routing metadata and assistant text from `codex exec --json`."""
    result = CodexTurnResult()
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        event_type = event.get("type", "")
        if event_type == "thread.started":
            result.session_id = event.get("thread_id") or event.get("thread", {}).get("id")
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if item.get("type") in {"agent_message", "agentMessage"}:
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text.strip():
                    result.texts.append(text.strip())
        elif event_type == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                result.usage = usage
        elif event_type in {"error", "turn.failed"}:
            detail = event.get("message") or event.get("error") or "Codex turn failed"
            result.error = str(detail)
    return result


def run_codex_turn(
    command: list[str],
    prompt: str,
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout: int,
    on_process: Callable[[subprocess.Popen | None], None] | None = None,
    delegate_active: Callable[[], bool] = lambda: False,
    max_duration: float | None = None,
    wait_for_completion: Callable = wait_for_turn_completion,
) -> CodexTurnResult:
    """Run one governed Codex turn with a progress-aware inactivity timeout."""
    process = None
    stdout_lines: list[str] = []
    stdout_done = threading.Event()
    activity_lock = threading.Lock()
    last_activity = time.monotonic()
    input_error = []

    def send_prompt() -> None:
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except OSError as exc:
            input_error.append(exc)

    def record_stdout() -> None:
        nonlocal last_activity
        try:
            for line in process.stdout:
                stdout_lines.append(line)
                with activity_lock:
                    last_activity = time.monotonic()
        finally:
            stdout_done.set()

    def discard_stderr() -> None:
        for _line in process.stderr:
            pass

    def activity_at() -> float:
        with activity_lock:
            return last_activity

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=dict(env),
            text=True,
        )
        if on_process:
            on_process(process)
        stdout_reader = threading.Thread(target=record_stdout, daemon=True)
        stderr_reader = threading.Thread(target=discard_stderr, daemon=True)
        input_writer = threading.Thread(target=send_prompt, daemon=True)
        stdout_reader.start()
        stderr_reader.start()
        input_writer.start()
        completed = wait_for_completion(
            stdout_done,
            inactivity_timeout=timeout,
            activity_at=activity_at,
            delegate_active=delegate_active,
            max_duration=max_duration if max_duration is not None else 4 * timeout,
        )
        if not completed:
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)
        process.wait(timeout=5)
        input_writer.join(timeout=1)
        stdout_reader.join(timeout=1)
        stderr_reader.join(timeout=1)
        if input_error:
            return CodexTurnResult(error="Codex fallback could not start")
    except subprocess.TimeoutExpired:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        return CodexTurnResult(error="Codex fallback timed out")
    except OSError:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        return CodexTurnResult(error="Codex fallback could not start")
    finally:
        if on_process:
            on_process(None)
    result = parse_codex_events(stdout_lines)
    if process.returncode != 0 and not result.error:
        result.error = f"Codex fallback exited with status {process.returncode}"
    return result
