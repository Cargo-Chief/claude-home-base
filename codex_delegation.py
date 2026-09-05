"""Incremental Codex app-server runner for governed delegates."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import queue
import subprocess
import threading
import time
from typing import Callable, Mapping


@dataclass
class CodexDelegateResult:
    texts: list[str] = field(default_factory=list)
    tokens: int = 0
    raw_tokens: int = 0
    error: str | None = None
    budget_exhausted: bool = False


def _send(process: subprocess.Popen, value: dict) -> None:
    process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _reader(stream, output: queue.Queue) -> None:
    try:
        for line in stream:
            output.put(line)
    finally:
        output.put(None)


def run_codex_delegate(
    command: list[str], prompt: str, *, cwd: str, env: Mapping[str, str],
    model: str, effort: str, read_only: bool, token_limit: int, timeout: int,
    on_process: Callable[[subprocess.Popen | None], None],
) -> CodexDelegateResult:
    """Run one ephemeral Codex turn and interrupt at the first over-limit usage event."""
    if token_limit < 1:
        return CodexDelegateResult(error="Codex delegate budget is exhausted")
    process = None
    events: queue.Queue = queue.Queue()
    thread_id = None
    turn_id = None
    texts: list[str] = []
    tokens = 0
    raw_tokens = 0
    exhausted = False
    completed = False
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=cwd, env=dict(env), text=True, bufsize=1,
        )
        on_process(process)
        threading.Thread(target=_reader, args=(process.stdout, events), daemon=True).start()
        _send(process, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "clientInfo": {
                    "name": "cargo-chief-home-base", "title": "Cargo Chief home base",
                    "version": "1.0.0",
                },
                "capabilities": {},
            },
        })
        while not completed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                line = events.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError from exc
            if line is None:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_id = event.get("id")
            method = event.get("method")
            if event_id == 1:
                if "result" not in event:
                    return CodexDelegateResult(error="Codex app server initialization failed")
                _send(process, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
                _send(process, {
                    "jsonrpc": "2.0", "id": 2, "method": "thread/start", "params": {
                        "model": model, "cwd": cwd, "approvalPolicy": "never",
                        "sandbox": "read-only" if read_only else "workspace-write",
                        "ephemeral": True,
                    },
                })
            elif event_id == 2:
                try:
                    response = event["result"]
                    thread_id = response["thread"]["id"]
                    if response["model"] != model:
                        return CodexDelegateResult(error="Codex delegate model mismatch")
                except (KeyError, TypeError):
                    return CodexDelegateResult(error="Codex thread start failed")
                _send(process, {
                    "jsonrpc": "2.0", "id": 3, "method": "turn/start", "params": {
                        "threadId": thread_id, "model": model, "effort": effort,
                        "input": [{"type": "text", "text": prompt}],
                    },
                })
            elif event_id == 3:
                try:
                    turn_id = event["result"]["turn"]["id"]
                except (KeyError, TypeError):
                    return CodexDelegateResult(error="Codex turn start failed")
            elif method == "item/completed":
                item = (event.get("params") or {}).get("item") or {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    texts.append(item["text"].strip())
            elif method == "thread/tokenUsage/updated":
                try:
                    total = event["params"]["tokenUsage"]["total"]
                    observed = total["outputTokens"]
                    observed_raw = total["totalTokens"]
                except (KeyError, TypeError):
                    return CodexDelegateResult(
                        tokens=tokens, raw_tokens=raw_tokens,
                        error="Codex delegate usage was invalid",
                    )
                if (
                    isinstance(observed, bool) or not isinstance(observed, int)
                    or isinstance(observed_raw, bool) or not isinstance(observed_raw, int)
                    or observed < tokens or observed_raw < raw_tokens or observed_raw < observed
                ):
                    return CodexDelegateResult(
                        tokens=tokens, raw_tokens=raw_tokens,
                        error="Codex delegate usage was invalid",
                    )
                tokens = observed
                raw_tokens = observed_raw
                if tokens >= token_limit and not exhausted:
                    exhausted = True
                    texts.clear()
                    if thread_id and turn_id:
                        _send(process, {
                            "jsonrpc": "2.0", "id": 4, "method": "turn/interrupt",
                            "params": {"threadId": thread_id, "turnId": turn_id},
                        })
            elif method == "turn/completed":
                status = ((event.get("params") or {}).get("turn") or {}).get("status")
                if status not in {"completed", "interrupted" if exhausted else "completed"}:
                    return CodexDelegateResult(
                        tokens=tokens, raw_tokens=raw_tokens,
                        error="Codex delegate turn failed",
                    )
                completed = True
            elif event_id is not None and method:
                _send(process, {
                    "jsonrpc": "2.0", "id": event_id,
                    "error": {"code": -32601, "message": "unsupported governed request"},
                })
        if not completed and not exhausted:
            return CodexDelegateResult(
                tokens=tokens, raw_tokens=raw_tokens,
                error="Codex delegate ended without completion",
            )
        if tokens < 1:
            return CodexDelegateResult(
                raw_tokens=raw_tokens, error="Codex delegate returned no usage",
            )
        if exhausted:
            return CodexDelegateResult(
                tokens=tokens, raw_tokens=raw_tokens, budget_exhausted=True,
            )
        return CodexDelegateResult(
            texts=[text for text in texts if text], tokens=tokens, raw_tokens=raw_tokens,
        )
    except TimeoutError:
        if process:
            process.kill()
        return CodexDelegateResult(
            tokens=tokens, raw_tokens=raw_tokens, error="Codex delegate timed out",
        )
    except OSError:
        return CodexDelegateResult(
            tokens=tokens, raw_tokens=raw_tokens,
            error="Codex delegate could not start",
        )
    finally:
        if process:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        on_process(None)
