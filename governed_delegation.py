"""Provider-neutral, fail-closed delegation launcher for Cargo Chief threads."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Mapping
import uuid

from codex_delegation import run_codex_delegate


DEFAULT_TOKEN_BUDGET = 250_000
BUDGET_UNIT = "generation_tokens_v1"
LEGACY_BUDGET_UNIT = "raw_tokens_legacy"
USAGE_RECEIPT_SCHEMA = "cargo-chief/delegation-usage-receipt/v1"
MAX_REQUEST_BYTES = 64 * 1024
IMPLEMENTATION_SECTIONS = (
    ("## Blocking Product Questions",),
    ("## 1. Data Flow Diagram",),
    ("## 2. Affected Components Inventory",),
    ("## 6. Testing Requirements",),
    ("## 7. Deployment Order", "## 7. Dependency and deployment order"),
    ("## 8. Rollback Plan",),
    ("## Required authorizations",),
)

ROUTES = {
    "implementation": {
        "claude": ("claude-opus-5[1m]", "medium"),
        "openai": ("gpt-5.6-sol", "medium"),
    },
    "bounded": {
        "claude": ("claude-opus-5[1m]", "medium"),
        "openai": ("gpt-5.6-sol", "medium"),
    },
    "mechanical": {
        "claude": ("claude-sonnet-5", "high"),
        "openai": ("gpt-5.6-terra", "high"),
    },
    "explore": {
        "claude": ("claude-haiku-4-5-20251001", "medium"),
        "openai": ("gpt-5.6-luna", "medium"),
    },
}


class DelegationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DelegationRequest:
    tier: str
    prompt: str
    mutation: bool
    budget_unit: str
    planned_tokens: int


@dataclass
class DelegateResult:
    text: str = ""
    tokens: int = 0
    raw_tokens: int = 0
    tool_uses: int = 0
    error: str | None = None
    budget_exhausted: bool = False


def delegation_request_id(request: DelegationRequest) -> str:
    """Return a content-free correlation id for the exact governed request."""
    payload = {
        "tier": request.tier,
        "prompt": request.prompt,
        "mutation": request.mutation,
        "budget_unit": request.budget_unit,
        "planned_tokens": request.planned_tokens,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_one_shot(path: Path, *, max_bytes: int = MAX_REQUEST_BYTES) -> str:
    try:
        stat = path.lstat()
        if path.is_symlink() or not path.is_file() or stat.st_size > max_bytes:
            raise DelegationError("one-shot delegation input is missing or unsafe")
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DelegationError("one-shot delegation input is missing or unsafe") from exc
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def load_request(path: Path) -> DelegationRequest:
    try:
        value = json.loads(_read_one_shot(path))
    except json.JSONDecodeError as exc:
        raise DelegationError("delegation request is invalid JSON") from exc
    budget_keys = {"tier", "prompt", "mutation", "budget_unit", "planned_tokens"}
    if not isinstance(value, dict) or set(value) != budget_keys:
        raise DelegationError("delegation request contains unsupported keys")
    tier = value.get("tier")
    prompt = value.get("prompt")
    mutation = value.get("mutation")
    if (tier not in ROUTES or not isinstance(prompt, str) or not prompt.strip()
            or not isinstance(mutation, bool)):
        raise DelegationError("delegation request has an unsupported tier or empty prompt")
    if tier == "explore" and mutation:
        raise DelegationError("explore delegation must remain read-only")
    budget_unit = value.get("budget_unit")
    planned_tokens = value.get("planned_tokens")
    if (
        budget_unit != BUDGET_UNIT
        or isinstance(planned_tokens, bool)
        or not isinstance(planned_tokens, int)
        or planned_tokens < 1
    ):
        raise DelegationError("delegation request budget contract is invalid")
    return DelegationRequest(
        tier=tier, prompt=prompt.strip(), mutation=mutation,
        budget_unit=budget_unit, planned_tokens=planned_tokens,
    )


def _section(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ims)^{re.escape(heading)}\s*$\n(.*?)(?=^##\s|\Z)"
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def validate_implementation_plan(root: Path, claim_file: Path) -> Path:
    claimed = _read_one_shot(claim_file, max_bytes=4096).strip()
    if not claimed or "\n" in claimed:
        raise DelegationError("implementation claim must contain one absolute plan path")
    plan = Path(claimed)
    if not plan.is_absolute() or plan.is_symlink() or not plan.is_file():
        raise DelegationError("implementation plan is missing or unsafe")
    resolved = plan.resolve()
    worktrees = (root.resolve() / "worktrees").resolve()
    try:
        relative = resolved.relative_to(worktrees)
    except ValueError as exc:
        raise DelegationError("implementation plan must live in a docs worktree") from exc
    parts = relative.parts
    if len(parts) < 4 or parts[1] != "docs" or parts[2] != "plans" or resolved.suffix != ".md":
        raise DelegationError("implementation plan must live in a docs worktree plans directory")
    docs_worktree = worktrees / parts[0] / "docs"
    try:
        common = subprocess.run(
            ["git", "-C", str(docs_worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            text=True, capture_output=True, check=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise DelegationError("implementation plan is not in a valid docs worktree") from exc
    if Path(common).resolve() != (root.resolve() / "docs" / ".git").resolve():
        raise DelegationError("implementation plan does not belong to the canonical docs clone")
    text = resolved.read_text(encoding="utf-8")
    frontmatter = re.match(r"(?s)\A---\s*\n(.*?)\n---\s*\n", text)
    if not frontmatter or not re.search(
        r"(?m)^readiness:\s*implementation-ready\s*$", frontmatter.group(1)
    ):
        raise DelegationError("implementation plan is not implementation-ready")
    missing = [
        headings[0] for headings in IMPLEMENTATION_SECTIONS
        if not any(_section(text, heading) for heading in headings)
    ]
    if missing:
        raise DelegationError("implementation plan is missing required planning sections")
    questions = _section(text, "## Blocking Product Questions")
    if not re.match(r"(?is)\ANone\.(?:\s|$)", questions):
        raise DelegationError("implementation plan has unresolved blocking product questions")
    authorizations = _section(text, "## Required authorizations")
    if not re.search(r"(?im)^Status:\s*clear\b", authorizations):
        raise DelegationError("implementation authorization is not clear")
    return resolved


def _validated_budget_state(value: object) -> dict:
    if not isinstance(value, dict):
        raise DelegationError("delegation budget state is invalid")
    keys = set(value)
    if keys == {"limit", "used"}:
        unit = LEGACY_BUDGET_UNIT
    elif keys == {"limit", "used", "unit"} and value.get("unit") == BUDGET_UNIT:
        unit = BUDGET_UNIT
    else:
        raise DelegationError("delegation budget state is invalid")
    limit, used = value.get("limit"), value.get("used")
    if (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        or isinstance(used, bool) or not isinstance(used, int) or used < 0
    ):
        raise DelegationError("delegation budget state is invalid")
    return {"limit": limit, "used": used, "unit": unit}


def initialize_budget(path: Path) -> None:
    """Atomically create new-unit state without reading or rewriting existing state."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.writing")
    descriptor = None
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(
                {"limit": DEFAULT_TOKEN_BUDGET, "used": 0, "unit": BUDGET_UNIT},
                handle, separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            pass
    except OSError as exc:
        raise DelegationError("delegation budget state could not be initialized") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def budget_status(path: Path) -> dict:
    if path.is_symlink():
        raise DelegationError("delegation budget state is invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read()
        if raw:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DelegationError("delegation budget state is invalid") from exc
            return _validated_budget_state(value)
        state = {"limit": DEFAULT_TOKEN_BUDGET, "used": 0, "unit": BUDGET_UNIT}
        handle.seek(0)
        json.dump(state, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fchmod(handle.fileno(), 0o600)
        return state


def update_budget(path: Path, *, add_tokens: int = 0, limit: int | None = None, reset: bool = False) -> dict:
    if add_tokens < 0 or (limit is not None and limit < 1):
        raise DelegationError("delegation budget update is invalid")
    if path.is_symlink():
        raise DelegationError("delegation budget state is invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read()
        if raw:
            try:
                state = json.loads(raw)
            except json.JSONDecodeError as exc:
                if not reset:
                    raise DelegationError("delegation budget state is invalid") from exc
                state = {"limit": DEFAULT_TOKEN_BUDGET, "used": 0, "unit": BUDGET_UNIT}
        else:
            state = {"limit": DEFAULT_TOKEN_BUDGET, "used": 0, "unit": BUDGET_UNIT}
        try:
            state = _validated_budget_state(state)
        except DelegationError:
            if not reset:
                raise
            state = {"limit": DEFAULT_TOKEN_BUDGET, "used": 0, "unit": BUDGET_UNIT}
        migrating_legacy_unit = state["unit"] != BUDGET_UNIT
        current_limit = state["limit"]
        current_used = state["used"]
        if migrating_legacy_unit and not reset:
            raise DelegationError(
                "legacy raw-token delegation budget must be reset by a named approver"
            )
        state = {
            "limit": (
                limit if limit is not None
                else DEFAULT_TOKEN_BUDGET if migrating_legacy_unit
                else current_limit
            ),
            "used": 0 if reset else current_used + add_tokens,
            "unit": BUDGET_UNIT,
        }
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fchmod(handle.fileno(), 0o600)
        return state


def _total_tokens(usage: Mapping[str, object]) -> int:
    direct = usage.get("total_tokens")
    if isinstance(direct, int) and direct >= 0:
        return direct
    return sum(
        value for key, value in usage.items()
        if key.endswith("tokens") and isinstance(value, int) and value >= 0
    )


def _generation_tokens(usage: Mapping[str, object]) -> int | None:
    """Return generated/reasoning tokens without charging input or prompt-cache reads."""
    for key in ("output_tokens", "outputTokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _stream_reader(stream, output: queue.Queue) -> None:
    try:
        for line in stream:
            output.put(line)
    finally:
        output.put(None)


def run_claude_delegate(
    command: list[str], prompt: str, *, cwd: str, env: Mapping[str, str],
    token_limit: int, timeout: int,
    on_process: Callable[[subprocess.Popen | None], None],
) -> DelegateResult:
    """Run Claude incrementally and stop before another call after the allowance is spent."""
    if token_limit < 1:
        return DelegateResult(error="Claude delegate budget is exhausted")
    process = None
    events: queue.Queue = queue.Queue()
    tokens = 0
    raw_tokens = 0
    tool_uses = 0
    result_text = ""
    seen_messages: set[str] = set()
    completed = False
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=cwd, env=dict(env), text=True, bufsize=1,
        )
        on_process(process)
        threading.Thread(target=_stream_reader, args=(process.stdout, events), daemon=True).start()
        process.stdin.write(prompt)
        process.stdin.close()
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
            if event.get("type") == "assistant":
                message = event.get("message") or {}
                message_id = message.get("id")
                usage = message.get("usage") or {}
                if not isinstance(message_id, str) or not isinstance(usage, dict):
                    return DelegateResult(
                        tokens=tokens, raw_tokens=raw_tokens,
                        error="Claude delegate returned invalid usage",
                    )
                if message_id not in seen_messages:
                    seen_messages.add(message_id)
                    generated = _generation_tokens(usage)
                    raw = _total_tokens(usage)
                    if generated is None or raw < generated:
                        return DelegateResult(
                            tokens=tokens, raw_tokens=raw_tokens,
                            error="Claude delegate returned invalid usage",
                        )
                    tokens += generated
                    raw_tokens += raw
                    content = message.get("content") or []
                    tool_uses += sum(
                        1 for item in content
                        if isinstance(item, dict) and item.get("type") == "tool_use"
                    )
                    if tokens >= token_limit:
                        process.kill()
                        return DelegateResult(
                            tokens=tokens, raw_tokens=raw_tokens,
                            tool_uses=tool_uses, budget_exhausted=True,
                        )
            elif event.get("type") == "result":
                usage = event.get("usage") or {}
                if not isinstance(usage, dict):
                    return DelegateResult(
                        tokens=tokens, raw_tokens=raw_tokens,
                        error="Claude delegate returned invalid usage",
                    )
                generated = _generation_tokens(usage)
                raw = _total_tokens(usage)
                if generated is None or raw < generated:
                    return DelegateResult(
                        tokens=tokens, raw_tokens=raw_tokens,
                        error="Claude delegate returned invalid usage",
                    )
                # Claude Code's result usage is authoritative for the complete turn.
                # Assistant usage remains useful for interrupting between turns.
                tokens = generated
                raw_tokens = raw
                text = event.get("result")
                if not isinstance(text, str):
                    return DelegateResult(
                        tokens=tokens, raw_tokens=raw_tokens,
                        error="Claude delegate returned invalid output",
                    )
                result_text = text.strip()
                completed = True
        if not completed:
            return DelegateResult(
                tokens=tokens, raw_tokens=raw_tokens,
                error="Claude delegate ended without completion",
            )
        process.wait(timeout=5)
        if process.returncode != 0:
            return DelegateResult(
                tokens=tokens, raw_tokens=raw_tokens, error="Claude delegate failed",
            )
        if tokens < 1:
            return DelegateResult(
                raw_tokens=raw_tokens,
                error="Claude delegate returned no usage",
            )
        if tokens >= token_limit:
            return DelegateResult(
                tokens=tokens, raw_tokens=raw_tokens,
                tool_uses=tool_uses, budget_exhausted=True,
            )
        return DelegateResult(
            text=result_text, tokens=tokens, raw_tokens=raw_tokens, tool_uses=tool_uses,
        )
    except (TimeoutError, subprocess.TimeoutExpired):
        if process:
            process.kill()
        return DelegateResult(
            tokens=tokens, raw_tokens=raw_tokens, error="Claude delegate timed out",
        )
    except OSError:
        return DelegateResult(
            tokens=tokens, raw_tokens=raw_tokens,
            error="Claude delegate could not start",
        )
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        on_process(None)


def _write_pid(path: Path, process: subprocess.Popen | None) -> None:
    if process is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.write_text(str(process.pid) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _append_audit(path: Path, fields: Mapping[str, object]) -> None:
    safe = " | ".join(f"{key}:{value}" for key, value in fields.items())
    with path.open("a", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(time.strftime("%Y-%m-%d %H:%M:%S") + " | DELEGATION | " + safe + "\n")


def delegation_audit_path(root: Path) -> Path:
    """Return the workspace-writable, operator-readable delegation audit log."""
    path = root.resolve() / "work" / "home-base" / "delegation-audit.log"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


@contextmanager
def _verification_marker_lock(path: Path):
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock.open("a", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _delegation_verification_status_unlocked(path: Path) -> str | None:
    try:
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return "invalid"
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(metadata, dict):
        return "invalid"
    status = metadata.get("status")
    return status if status in {"pending", "allocation_exhausted", "budget_exhausted"} else "invalid"


def delegation_verification_status(path: Path) -> str | None:
    """Read only the fail-closed status of a pending delegation marker."""
    with _verification_marker_lock(path):
        return _delegation_verification_status_unlocked(path)


def consume_budget_exhaustion(path: Path) -> bool:
    """Consume one valid exhaustion notice without weakening verification failures."""
    with _verification_marker_lock(path):
        if _delegation_verification_status_unlocked(path) != "budget_exhausted":
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True


def consume_allocation_exhaustion(path: Path) -> bool:
    """Consume one valid stage-allocation stop without changing the thread budget."""
    with _verification_marker_lock(path):
        if _delegation_verification_status_unlocked(path) != "allocation_exhausted":
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True


def prepare_owner_delegation_state(budget_path: Path, verification_path: Path) -> bool:
    """Initialize budget state and clear only a stale per-call allocation stop.

    A stopped or evicted owner may never observe the result event that normally
    consumes this marker. Pending verification and thread-budget exhaustion are
    durable gates and must survive owner restarts.
    """
    initialize_budget(budget_path)
    return consume_allocation_exhaustion(verification_path)


def parse_budget_command(text: str) -> tuple[str, int | None] | None:
    """Parse the two supported exact spellings of a delegation-budget command."""
    normalized = re.sub(r"<@[A-Z0-9]+>", "", text).strip().lower()
    match = re.fullmatch(
        r"(?:delegation|delegate) budget (status|reset|set\s+([1-9][0-9]{0,17}))",
        normalized,
    )
    if not match:
        return None
    action = match.group(1)
    return action, int(match.group(2)) if match.group(2) else None


def _launch_from_environment_unlocked(env: Mapping[str, str]) -> int:
    env = dict(env or os.environ)
    required = (
        "CARGO_CHIEF_ROOT", "CARGO_CHIEF_DELEGATION_REQUEST_FILE",
        "CARGO_CHIEF_IMPLEMENTATION_CLAIM_FILE", "CARGO_CHIEF_DELEGATION_BUDGET_FILE",
        "CARGO_CHIEF_DELEGATE_PID_FILE", "CARGO_CHIEF_AUDIT_LOG",
        "CARGO_CHIEF_DELEGATE_VERIFICATION_FILE",
        "CARGO_CHIEF_OWNER_PROVIDER", "CARGO_CHIEF_OWNER_MODEL", "CARGO_CHIEF_OWNER_EFFORT",
        "CLAUDE_THREAD_TS", "CLAUDE_CHANNEL_ID", "CARGO_CHIEF_CURRENT_USER",
    )
    if any(not env.get(key) for key in required):
        raise DelegationError("governed delegation environment is incomplete")
    root = Path(env["CARGO_CHIEF_ROOT"]).resolve()
    request = load_request(Path(env["CARGO_CHIEF_DELEGATION_REQUEST_FILE"]))
    request_id = delegation_request_id(request)
    claim_file = Path(env["CARGO_CHIEF_IMPLEMENTATION_CLAIM_FILE"])
    plan_gate = "not-required"
    if request.mutation:
        validate_implementation_plan(root, claim_file)
        plan_gate = "passed"
    elif claim_file.exists():
        _read_one_shot(claim_file, max_bytes=4096)
        raise DelegationError("implementation claim supplied for a non-implementation tier")
    provider = env["CARGO_CHIEF_OWNER_PROVIDER"]
    if provider not in {"claude", "openai"}:
        raise DelegationError("owner provider is unsupported")
    model, effort = ROUTES[request.tier][provider]
    budget_file = Path(env["CARGO_CHIEF_DELEGATION_BUDGET_FILE"])
    before = budget_status(budget_file)
    if before["unit"] != BUDGET_UNIT:
        raise DelegationError(
            "legacy raw-token delegation budget must be reset by a named approver"
        )
    if before["used"] >= before["limit"]:
        raise DelegationError("thread delegation budget is exhausted")
    remaining_tokens = before["limit"] - before["used"]
    if request.planned_tokens > remaining_tokens:
        raise DelegationError(
            "planned delegation does not fit the remaining generation-token budget"
        )
    call_token_limit = request.planned_tokens
    prompt = (
        "You are a governed Cargo Chief delegate. Perform only the supplied bounded task. "
        "Do not delegate again, access credentials, touch production, commit, push, or widen scope. "
        + ("You may edit only within the supplied scope. " if request.mutation else
           "This is read-only: do not edit or create files. ")
        + "Return concise evidence for independent owner verification.\n\n" + request.prompt
    )
    timeout = int(env.get("CARGO_CHIEF_DELEGATE_TIMEOUT", "1800"))
    pid_file = Path(env["CARGO_CHIEF_DELEGATE_PID_FILE"])
    started = time.monotonic()
    if provider == "openai":
        command = [
            "codex", "-c", "features.multi_agent=false", "app-server", "--stdio",
        ]
        result_raw = run_codex_delegate(
            command, prompt, cwd=os.getcwd(), env=env, model=model, effort=effort,
            read_only=not request.mutation, token_limit=call_token_limit, timeout=timeout,
            on_process=lambda process: _write_pid(pid_file, process),
        )
        result = DelegateResult(
            text="\n\n".join(result_raw.texts),
            tokens=result_raw.tokens,
            raw_tokens=result_raw.raw_tokens,
            error=result_raw.error,
            budget_exhausted=result_raw.budget_exhausted,
        )
    else:
        command = [
            "claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model,
            "--effort", effort, "--permission-mode", "auto",
        ]
        if not request.mutation:
            command.extend(["--allowedTools", "Read,Grep,Glob"])
        result = run_claude_delegate(
            command, prompt, cwd=os.getcwd(), env=env, token_limit=call_token_limit,
            timeout=timeout,
            on_process=lambda process: _write_pid(pid_file, process),
        )
    state = update_budget(budget_file, add_tokens=result.tokens)
    thread_exhausted = state["used"] >= state["limit"]
    allocation_exhausted = result.budget_exhausted and not thread_exhausted
    status = (
        "failed" if result.error else
        "allocation_exhausted" if allocation_exhausted else
        "budget_exhausted" if thread_exhausted else
        "completed"
    )
    _append_audit(Path(env["CARGO_CHIEF_AUDIT_LOG"]), {
        "USER": env["CARGO_CHIEF_CURRENT_USER"],
        "CHANNEL": env["CLAUDE_CHANNEL_ID"],
        "THREAD": env["CLAUDE_THREAD_TS"],
        "TIER": request.tier,
        "OWNER_PROVIDER": provider,
        "OWNER_MODEL": env["CARGO_CHIEF_OWNER_MODEL"],
        "OWNER_EFFORT": env["CARGO_CHIEF_OWNER_EFFORT"],
        "PROVIDER": provider,
        "MODEL": model,
        "EFFORT": effort,
        "PLAN_GATE": plan_gate,
        "BUDGET_UNIT": BUDGET_UNIT,
        "BUDGET_TOKENS": result.tokens,
        "RAW_TOKENS": result.raw_tokens,
        "DURATION": f"{time.monotonic() - started:.1f}s",
        "STATUS": status,
    })
    if result.error:
        raise DelegationError(result.error)
    verification_file = Path(env["CARGO_CHIEF_DELEGATE_VERIFICATION_FILE"])
    if allocation_exhausted:
        with _verification_marker_lock(verification_file):
            verification_file.write_text(json.dumps({
                "status": "allocation_exhausted", "tier": request.tier,
                "provider": provider, "model": model, "effort": effort,
                "budget_unit": BUDGET_UNIT, "tokens": result.tokens,
                "raw_tokens": result.raw_tokens,
                "request_id": request_id,
            }, separators=(",", ":")) + "\n", encoding="utf-8")
            verification_file.chmod(0o600)
        raise DelegationError(
            "delegation stage generation-token allocation exhausted; delegate return withheld"
        )
    if thread_exhausted:
        with _verification_marker_lock(verification_file):
            verification_file.write_text(json.dumps({
                "status": "budget_exhausted", "tier": request.tier,
                "provider": provider, "model": model, "effort": effort,
                "budget_unit": BUDGET_UNIT, "tokens": result.tokens,
                "raw_tokens": result.raw_tokens,
                "request_id": request_id,
            }, separators=(",", ":")) + "\n", encoding="utf-8")
            verification_file.chmod(0o600)
        raise DelegationError("thread delegation budget exhausted; delegate return withheld")
    with _verification_marker_lock(verification_file):
        verification_file.write_text(json.dumps({
            "status": "pending", "tier": request.tier, "provider": provider, "model": model,
            "effort": effort, "budget_unit": BUDGET_UNIT, "tokens": result.tokens,
            "raw_tokens": result.raw_tokens, "mutation": request.mutation,
            "request_id": request_id,
        }, separators=(",", ":")) + "\n", encoding="utf-8")
        verification_file.chmod(0o600)
    print(result.text)
    return 0


def launch_from_environment(env: Mapping[str, str] | None = None) -> int:
    """Serialize all delegation for one thread before validating or spending."""
    values = dict(env or os.environ)
    pid_name = values.get("CARGO_CHIEF_DELEGATE_PID_FILE")
    if not pid_name:
        raise DelegationError("governed delegation environment is incomplete")
    lock_path = Path(pid_name).with_suffix(".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DelegationError("another delegate is already active in this thread") from exc
        os.fchmod(handle.fileno(), 0o600)
        return _launch_from_environment_unlocked(values)


def verify_from_environment(env: Mapping[str, str] | None = None) -> int:
    values = dict(env or os.environ)
    required = (
        "CARGO_CHIEF_DELEGATE_VERIFICATION_FILE", "CARGO_CHIEF_AUDIT_LOG",
        "CARGO_CHIEF_CURRENT_USER", "CLAUDE_CHANNEL_ID", "CLAUDE_THREAD_TS",
    )
    if any(not values.get(key) for key in required):
        raise DelegationError("governed verification environment is incomplete")
    marker = Path(values["CARGO_CHIEF_DELEGATE_VERIFICATION_FILE"])
    with _verification_marker_lock(marker):
        try:
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
                raise DelegationError("no safe pending delegate verification exists")
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise DelegationError("pending delegate verification is invalid")
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise DelegationError("no safe pending delegate verification exists") from exc
        if metadata.get("status") == "allocation_exhausted":
            raise DelegationError(
                "delegation stage generation-token allocation was exhausted; "
                "no thread-budget reset is required"
            )
        if metadata.get("status") != "pending":
            raise DelegationError("thread delegation budget must be reset by a named approver")
        tokens = metadata.get("tokens")
        request_id = metadata.get("request_id")
        if (
            metadata.get("budget_unit") != BUDGET_UNIT
            or isinstance(tokens, bool)
            or not isinstance(tokens, int)
            or tokens < 1
            or not isinstance(request_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", request_id) is None
        ):
            raise DelegationError("pending delegate verification usage is invalid")
        marker.unlink()
    _append_audit(Path(values["CARGO_CHIEF_AUDIT_LOG"]), {
        "USER": values["CARGO_CHIEF_CURRENT_USER"],
        "CHANNEL": values["CLAUDE_CHANNEL_ID"],
        "THREAD": values["CLAUDE_THREAD_TS"],
        "TIER": metadata.get("tier", "unknown"),
        "MODEL": metadata.get("model", "unknown"),
        "OWNER_VERIFY_TOOLS": 1,
        "STATUS": "verified",
    })
    print(json.dumps({
        "schema_version": USAGE_RECEIPT_SCHEMA,
        "budget_unit": metadata["budget_unit"],
        "actual_tokens": tokens,
        "request_id": request_id,
    }, separators=(",", ":")))
    return 0


def main() -> int:
    try:
        if sys.argv[1:] == ["--verify"]:
            return verify_from_environment()
        if sys.argv[1:]:
            raise DelegationError("unsupported governed delegation arguments")
        return launch_from_environment()
    except DelegationError as exc:
        print(f"DELEGATION_REFUSED: {exc}", file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
