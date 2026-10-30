"""PreToolUse hook: refuse writes to a worktree bundle this Slack thread has not claimed.

A thread owns at most one bundle, bound through the bundle claim at the end of a turn. Writing to
another bundle by its path skips the binding, so no other thread is refused that bundle and the live
cap never counts it. Covers file edits and git commands that change a repository; other shell
writes are not parsed. Reads are always allowed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys

from cargo_chief_safety import (
    BUNDLE_NAME,
    SafetyError,
    _bound_bundles,
    _validated_bundle_path,
)

EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
GIT_WRITES = {
    "add", "am", "apply", "checkout", "cherry-pick", "commit", "merge", "mv", "pull", "push",
    "rebase", "reset", "restore", "revert", "rm", "stash", "switch", "tag",
}
GIT_VALUE_OPTIONS = {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
SEPARATORS = {";", "&&", "||", "|", "&", "\n"}


def _bundle_of(root: Path, path: Path) -> str | None:
    """The bundle directory a path lies in, or None outside a valid bundle."""
    worktrees = (root / "worktrees").resolve()
    try:
        relative = Path(os.path.realpath(path)).relative_to(worktrees)
    except ValueError:
        return None
    if not relative.parts or not BUNDLE_NAME.fullmatch(relative.parts[0]):
        return None
    try:
        _validated_bundle_path(root, relative.parts[0])
    except SafetyError:
        return None
    return relative.parts[0]


def _segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token in SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _git_write_dirs(command: str, cwd: Path) -> list[Path]:
    """The directories that git commands in this shell command would change."""
    targets = []
    for segment in _segments(command):
        if segment[0] == "cd" and len(segment) > 1:
            cwd = cwd / os.path.expanduser(segment[1])
            continue
        if Path(segment[0]).name != "git":
            continue
        where, index = cwd, 1
        while index < len(segment) and segment[index].startswith("-"):
            option = segment[index]
            if option == "-C" and index + 1 < len(segment):
                where = where / os.path.expanduser(segment[index + 1])
                index += 2
            elif option in GIT_VALUE_OPTIONS and index + 1 < len(segment):
                index += 2
            else:
                index += 1
        if index < len(segment) and segment[index] in GIT_WRITES:
            targets.append(where)
    return targets


def _claimed(root: Path, work_dir: Path, claim_file: Path | None) -> set[str]:
    """Bundles this thread may write: its binding, or a pending claim no other thread holds."""
    claimed = set()
    try:
        bound = json.loads((work_dir / "state.json").read_text(encoding="utf-8")).get("bundle")
    except (OSError, json.JSONDecodeError, AttributeError):
        bound = None
    if isinstance(bound, str):
        claimed.add(bound)
    try:
        pending = claim_file.read_text(encoding="utf-8").strip() if claim_file else ""
    except OSError:
        pending = ""
    if pending:
        holders = {key for key, bundle in _bound_bundles(root).items() if bundle == pending}
        if holders <= {work_dir.name}:
            claimed.add(pending)
    return claimed


def refusal(event: dict, env: dict) -> str | None:
    """The reason to refuse this tool call, or None to let it run."""
    work_dir, root = env.get("CARGO_CHIEF_THREAD_WORK_DIR"), env.get("CARGO_CHIEF_ROOT")
    if not work_dir or not root:
        return None
    root_path, work_path = Path(root), Path(work_dir)
    tool, tool_input = event.get("tool_name"), event.get("tool_input") or {}
    cwd = Path(event.get("cwd") or work_dir)
    if tool in EDIT_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("notebook_path")
        paths = [cwd / target] if isinstance(target, str) and target else []
    elif tool == "Bash" and isinstance(tool_input.get("command"), str):
        try:
            paths = _git_write_dirs(tool_input["command"], cwd)
        except ValueError:
            return None
    else:
        return None
    bundles = {bundle for bundle in (_bundle_of(root_path, path) for path in paths) if bundle}
    if not bundles:
        return None
    claim = env.get("CARGO_CHIEF_BUNDLE_CLAIM_FILE")
    unclaimed = sorted(bundles - _claimed(root_path, work_path, Path(claim) if claim else None))
    if not unclaimed:
        return None
    return (
        f"this thread has not claimed {', '.join(unclaimed)}. To work on it here, write its name to "
        f"{claim or 'the bundle claim file'} first; if another thread or ticket owns it, hand the "
        "work off instead"
    )


def main() -> int:
    try:
        event = json.load(sys.stdin)
        reason = refusal(event if isinstance(event, dict) else {}, dict(os.environ))
    except Exception as exc:
        # A workflow guard, not a security boundary: a guard fault must not stop every edit.
        print(f"bundle guard skipped: {type(exc).__name__}", file=sys.stderr)
        return 0
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
