"""PreToolUse hook for the Notion MCP server: refuse calls that trash, archive or drop content.

The integration's update capability covers moving a page or block to the trash and a Markdown
rewrite that deletes child pages, and the API names no separate tool for either: each is a flag
on an ordinary update. Deleting a block, changing a database schema and moving a page are
denied by tool name instead (`NOTION_DENY_RULES`).
"""

from __future__ import annotations

import json
import sys

REMOVAL_FLAGS = {"in_trash", "archived", "is_archived", "allow_deleting_content"}


def _flags_set(value, prefix=""):
    """Dotted names of every removal flag set true, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if key in REMOVAL_FLAGS and item not in (False, None, "false", "False", 0):
                yield name
            yield from _flags_set(item, name)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _flags_set(item, f"{prefix}[{index}]")
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        # Some MCP clients pass a nested object as a JSON string.
        try:
            yield from _flags_set(json.loads(value), prefix)
        except ValueError:
            return


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
