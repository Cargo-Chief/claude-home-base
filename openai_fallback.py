"""Governed one-turn Codex execution for Claude credit-limit fallback."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import subprocess
import threading
import time
from typing import Callable, Iterable, Mapping

from session_lifecycle import wait_for_turn_completion


CLAUDE_LIMIT_RE = re.compile(
    r"You've hit your (?:(?:usage|weekly|monthly(?: spend)?) )?limit",
    re.IGNORECASE,
)


def is_claude_limit_notice(text: str) -> bool:
    """Return whether Claude emitted an account usage-limit notice."""
    return CLAUDE_LIMIT_RE.search(text) is not None


def fallback_error_kind(error: str | None) -> str:
    """Return a content-free failure category safe for service logs."""
    return {
        "Codex fallback timed out": "timeout",
        "Codex fallback could not start": "start",
    }.get(error, "provider")


def model_notice_text(model: str, previous: str = "") -> str | None:
    """Build truthful model attribution, excluding CLI synthetic envelopes."""
    if not model or model == "<synthetic>" or model == previous:
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
