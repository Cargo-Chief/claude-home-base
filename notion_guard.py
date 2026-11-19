"""PreToolUse hook for the Notion MCP server: refuse calls that trash, archive or drop content.

The integration's update capability covers moving a page or block to the trash and a Markdown
rewrite that deletes child pages, and the API names no separate tool for either: each is a flag
on an ordinary update. Deleting a block, changing a database schema and moving a page are
denied by tool name instead (`NOTION_DENY_RULES`).
"""

from __future__ import annotations

import json
import sys

# erase_content is not in the pinned spec, but the server forwards undeclared body keys.
REMOVAL_FLAGS = {"in_trash", "archived", "is_archived", "allow_deleting_content", "erase_content"}
# The server JS-trims a string argument and JSON-decodes it up to 3 times; decode at least as far.
MAX_DECODE_PASSES = 8
TRIMMED = " \t\n\r\f\v                 　﻿"


def _decoded(value: str):
    """The value as the server would deserialize it: unwrapped JSON, or the original string."""
    current = value
    for _ in range(MAX_DECODE_PASSES):
        text = current.strip(TRIMMED)
        if not (text.startswith(("{", "[")) or (len(text) >= 2 and text[0] == text[-1] == '"')):
            return current
        try:
            current = json.loads(text)
        except ValueError:
            return current
        if not isinstance(current, str):
            return current
    return current


def _flag_set(value) -> bool:
    """A flag is set by a true scalar; an object under the same key is a database property value."""
    if isinstance(value, str):
        decoded = _decoded(value)
        if decoded is not value:
            return _flag_set(decoded)
        return value.strip(TRIMMED).lower() not in ("", "false", "0", "null", "none")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _flags_set(value, prefix=""):
    """Dotted names of every removal flag set true, at any depth."""
    if isinstance(value, str):
        decoded = _decoded(value)
        if decoded is not value:
            yield from _flags_set(decoded, prefix)
    elif isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if str(key).lower() in REMOVAL_FLAGS and _flag_set(item):
                yield name
            yield from _flags_set(item, name)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _flags_set(item, f"{prefix}[{index}]")


def refusal(event: dict) -> str | None:
    """The reason to refuse this tool call, or None to let it run."""
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return "notion call input is not an object"
    flags = sorted(set(_flags_set(tool_input)))
    if flags:
        return (
            f"notion calls may not trash, archive or delete content ({', '.join(flags)}); "
            "ask a person to remove it in Notion"
        )
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        reason = refusal(event if isinstance(event, dict) else {})
    except BaseException:
        # Any exit other than 2 lets the call run, so every failure refuses it.
        print("notion guard could not check this call", file=sys.stderr)
        return 2
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
