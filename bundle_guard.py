"""PreToolUse hook: refuse writes to a worktree bundle this Slack thread has not claimed.

A thread owns at most one bundle, bound through the bundle claim at the end of a turn. Writing to
another bundle by its path skips the binding, so no other thread is refused that bundle and the live
cap never counts it. Covers file edits and git commands that change a repository; other shell
writes are not checked.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import sys

EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
GIT_WRITES = {
    "add", "am", "apply", "bisect", "branch", "checkout", "cherry-pick", "clean", "commit", "config",
    "fetch", "gc", "init", "merge", "mv", "notes", "prune", "pull", "push", "rebase", "remote", "reset",
    "restore", "revert", "rm", "stash", "submodule", "switch", "tag", "update-ref", "worktree",
}
# Subcommands whose listing forms only read; any other argument makes them writes.
GIT_READ_FORMS = {
    "branch": {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--list", "-l", "--show-current", "--contains", "--merged", "--no-merged"},
    "config": {"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin"},
    "notes": {"list", "show"},
    "remote": {"-v", "show", "get-url"},
    "stash": {"list", "show"},
    "submodule": {"status"},
    "tag": {"-l", "--list", "--contains", "--points-at", "-n"},
    "worktree": {"list"},
}
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
SHELL_BREAKS = {";", "&&", "||", "|", "&", "(", ")", "{", "}", "<", ">", ">>", "2>", "&>", "\n"}
CLAIM_MAX_BYTES = 256
SHELL_VARIABLE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")


def _tokens(command: str) -> list[str]:
    """Shell words, with nested `bash -c '...'` strings split again; comments are kept as words."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.commenters = ""
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        words = command.split()
    tokens = []
    for word in words:
        tokens.append(word)
        if any(space in word for space in " \n\t"):
            tokens.extend(_tokens(word))
    return tokens


def _is_git_write(tokens: list[str]) -> bool:
    for start, token in enumerate(tokens):
        if os.path.basename(token) != "git":
            continue
        index = start + 1
        while index < len(tokens) and tokens[index].startswith("-"):
            index += 2 if tokens[index] in GIT_VALUE_OPTIONS else 1
        if index >= len(tokens) or tokens[index] not in GIT_WRITES:
            continue
        subcommand, rest = tokens[index], []
        for argument in tokens[index + 1:]:
            if argument in SHELL_BREAKS:
                break
            rest.append(argument)
        reads = GIT_READ_FORMS.get(subcommand)
        if reads is not None and (not rest or rest[0] in reads):
            continue
        return True
    return False


def _paths(tokens: list[str], cwd: Path, env: dict) -> list[Path]:
    """Every word that could name a directory, expanded and resolved against the call cwd."""
    paths = [cwd]
    for token in tokens:
        value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
        value = SHELL_VARIABLE.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), value)
        value = os.path.expanduser(value)
        if "/" in value:
            paths.append(cwd / value)
    return paths


def _bundle_of(root: Path, path: Path) -> str | None:
    """The bundle directory a path lies in, or None outside a valid bundle."""
    try:
        relative = Path(os.path.realpath(path)).relative_to((root / "worktrees").resolve())
    except ValueError:
        return None
    from cargo_chief_safety import SafetyError, _validated_bundle_path

    try:
        _validated_bundle_path(root, relative.parts[0])
    except (IndexError, SafetyError):
        return None
    return relative.parts[0]


def _read(path: Path) -> str:
    """A small private file's text, or "" when it is missing, oversized or not text."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def _claimed(root: Path, work_dir: Path, claim_file: Path | None, cap: int) -> set[str]:
    """Bundles this thread may write: its binding, or a pending claim the turn-end check would accept."""
    from cargo_chief_safety import SafetyError, _bound_bundles, _check_bundle_binding

    bound = _bound_bundles(root)
    others = {bundle for key, bundle in bound.items() if key != work_dir.name}
    claimed = set()
    own = bound.get(work_dir.name)
    if own and own not in others:
        claimed.add(own)
    pending = _read(claim_file) if claim_file else ""
    if pending and len(pending.encode()) <= CLAIM_MAX_BYTES and "\n" not in pending.strip():
        pending = pending.strip()
        rivals = {
            path.parent.name
            for path in work_dir.parent.glob(f"*/{claim_file.name}")
            if path.parent != work_dir and _read(path).strip() == pending
        }
        try:
            _check_bundle_binding(root, pending, max_live_bundles=cap, replacing=work_dir.name)
        except SafetyError:
            rivals.add("refused")
        if not rivals:
            claimed.add(pending)
    return claimed


def refusal(event: dict, env: dict) -> str | None:
    """The reason to refuse this tool call, or None to let it run."""
    work_dir, root = env.get("CARGO_CHIEF_THREAD_WORK_DIR"), env.get("CARGO_CHIEF_ROOT")
    if not work_dir or not root:
        return None
    tool, tool_input = event.get("tool_name"), event.get("tool_input") or {}
    cwd = Path(event.get("cwd") or work_dir)
    if tool in EDIT_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(target, str) or not target:
            return None
        paths = [cwd / target]
    elif tool == "Bash" and isinstance(tool_input.get("command"), str):
        command = tool_input["command"]
        if "git" not in command:
            return None
        tokens = _tokens(command)
        if not _is_git_write(tokens):
            return None
        paths = _paths(tokens, cwd, env)
    else:
        return None
    root_path, work_path = Path(root), Path(work_dir)
    bundles = {bundle for bundle in (_bundle_of(root_path, path) for path in paths) if bundle}
    if not bundles:
        return None
    claim = env.get("CARGO_CHIEF_BUNDLE_CLAIM_FILE")
    try:
        cap = int(env.get("CARGO_CHIEF_MAX_LIVE_BUNDLES", "3"))
    except ValueError:
        cap = 3
    unclaimed = sorted(bundles - _claimed(root_path, work_path, Path(claim) if claim else None, cap))
    if not unclaimed:
        return None
    return (
        f"this thread has not claimed {', '.join(unclaimed)}. If this thread owns that ticket, write "
        f"the bundle name to {claim or 'the bundle claim file'} first (a claim replaces any bundle the "
        "thread holds, and is refused while another thread holds it or the live cap is reached); "
        "otherwise hand the work off"
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
