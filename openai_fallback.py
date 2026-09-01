"""Governed one-turn Codex execution for Claude credit-limit fallback."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import subprocess
from typing import Callable, Iterable, Mapping


CLAUDE_LIMIT_RE = re.compile(
    r"You've hit your (?:(?:usage|weekly) )?limit",
    re.IGNORECASE,
)


def is_claude_limit_notice(text: str) -> bool:
    """Return whether Claude emitted an account usage-limit notice."""
    return CLAUDE_LIMIT_RE.search(text) is not None


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
) -> CodexTurnResult:
    """Run one governed Codex turn without exposing stderr or configuration values."""
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
        stdout, _stderr = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return CodexTurnResult(error="Codex fallback timed out")
    except OSError:
        return CodexTurnResult(error="Codex fallback could not start")
    finally:
        if on_process:
            on_process(None)
    result = parse_codex_events(stdout.splitlines())
    if process.returncode != 0 and not result.error:
        result.error = f"Codex fallback exited with status {process.returncode}"
    return result
