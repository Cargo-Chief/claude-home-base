"""Provider-neutral, fail-closed delegation launcher for Cargo Chief threads."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, Mapping

from openai_fallback import run_codex_turn


DEFAULT_TOKEN_BUDGET = 250_000
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


@dataclass
class DelegateResult:
    text: str = ""
    tokens: int = 0
    tool_uses: int = 0
    error: str | None = None


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
    if not isinstance(value, dict) or set(value) != {"tier", "prompt", "mutation"}:
        raise DelegationError("delegation request must contain only tier, prompt, and mutation")
    tier = value.get("tier")
    prompt = value.get("prompt")
    mutation = value.get("mutation")
    if (tier not in ROUTES or not isinstance(prompt, str) or not prompt.strip()
            or not isinstance(mutation, bool)):
        raise DelegationError("delegation request has an unsupported tier or empty prompt")
    if tier == "explore" and mutation:
        raise DelegationError("explore delegation must remain read-only")
    return DelegationRequest(tier=tier, prompt=prompt.strip(), mutation=mutation)


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


def budget_status(path: Path) -> dict:
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
            limit, used = value.get("limit"), value.get("used")
            if not isinstance(limit, int) or limit < 1 or not isinstance(used, int) or used < 0:
                raise DelegationError("delegation budget state is invalid")
            return {"limit": limit, "used": used}
        return {"limit": DEFAULT_TOKEN_BUDGET, "used": 0}


def update_budget(path: Path, *, add_tokens: int = 0, limit: int | None = None, reset: bool = False) -> dict:
    if add_tokens < 0 or (limit is not None and limit < 1):
        raise DelegationError("delegation budget update is invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read()
        if raw:
            try:
                state = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DelegationError("delegation budget state is invalid") from exc
        else:
            state = {"limit": DEFAULT_TOKEN_BUDGET, "used": 0}
        current_limit = state.get("limit")
        current_used = state.get("used")
        if not isinstance(current_limit, int) or current_limit < 1 or not isinstance(current_used, int) or current_used < 0:
            raise DelegationError("delegation budget state is invalid")
        state = {
            "limit": limit if limit is not None else current_limit,
            "used": 0 if reset else current_used + add_tokens,
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


def run_claude_delegate(command: list[str], prompt: str, *, cwd: str, env: Mapping[str, str], timeout: int, on_process: Callable[[subprocess.Popen | None], None]) -> DelegateResult:
    process = None
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=dict(env), text=True,
        )
        on_process(process)
        stdout, _stderr = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        if process:
            process.kill()
            process.communicate()
        return DelegateResult(error="Claude delegate timed out")
    except OSError:
        return DelegateResult(error="Claude delegate could not start")
    finally:
        on_process(None)
    if process is None or process.returncode != 0:
        return DelegateResult(error="Claude delegate failed")
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return DelegateResult(error="Claude delegate returned invalid output")
    text = value.get("result")
    usage = value.get("usage") or {}
    if not isinstance(text, str) or not isinstance(usage, dict):
        return DelegateResult(error="Claude delegate returned invalid output")
    return DelegateResult(text=text.strip(), tokens=_total_tokens(usage))


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


def delegation_verification_status(path: Path) -> str | None:
    """Read only the fail-closed status of a pending delegation marker."""
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
    return status if status in {"pending", "budget_exhausted"} else "invalid"


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
    if before["used"] >= before["limit"]:
        raise DelegationError("thread delegation budget is exhausted")
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
            "codex", "--profile", "cargo-chief", "-c", f'model_reasoning_effort="{effort}"',
            "-c", "features.multi_agent=false",
            "exec", "--json", "--model", model, "--cd", os.getcwd(),
            "--skip-git-repo-check", "-",
        ]
        if not request.mutation:
            command[command.index("exec") + 1:command.index("exec") + 1] = ["--sandbox", "read-only"]
        result_raw = run_codex_turn(
            command, prompt, cwd=os.getcwd(), env=env, timeout=timeout,
            on_process=lambda process: _write_pid(pid_file, process),
        )
        result = DelegateResult(
            text="\n\n".join(result_raw.texts),
            tokens=_total_tokens(result_raw.usage),
            error=result_raw.error,
        )
    else:
        command = [
            "claude", "-p", "--output-format", "json", "--model", model,
            "--effort", effort, "--permission-mode", "auto",
        ]
        if not request.mutation:
            command.extend(["--allowedTools", "Read,Grep,Glob"])
        result = run_claude_delegate(
            command, prompt, cwd=os.getcwd(), env=env, timeout=timeout,
            on_process=lambda process: _write_pid(pid_file, process),
        )
    state = update_budget(budget_file, add_tokens=result.tokens)
    exhausted = state["used"] >= state["limit"]
    status = "failed" if result.error else "budget_exhausted" if exhausted else "completed"
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
        "TOKENS": result.tokens,
        "DURATION": f"{time.monotonic() - started:.1f}s",
        "STATUS": status,
    })
    if result.error:
        raise DelegationError(result.error)
    verification_file = Path(env["CARGO_CHIEF_DELEGATE_VERIFICATION_FILE"])
    if exhausted:
        verification_file.write_text(json.dumps({
            "status": "budget_exhausted", "tier": request.tier,
            "provider": provider, "model": model, "effort": effort,
            "tokens": result.tokens,
        }, separators=(",", ":")) + "\n", encoding="utf-8")
        verification_file.chmod(0o600)
        raise DelegationError("thread delegation budget exhausted; delegate return withheld")
    verification_file.write_text(json.dumps({
        "status": "pending", "tier": request.tier, "provider": provider, "model": model,
        "effort": effort, "tokens": result.tokens, "mutation": request.mutation,
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
    try:
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
            raise DelegationError("no safe pending delegate verification exists")
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise DelegationError("pending delegate verification is invalid")
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise DelegationError("no safe pending delegate verification exists") from exc
    if metadata.get("status") != "pending":
        raise DelegationError("thread delegation budget must be reset by a named approver")
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
    print("VERIFICATION_RECORDED")
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
