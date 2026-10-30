"""PreToolUse hook for the headless browser: refuse any call that names a file or path.

Playwright MCP resolves a named file against the session cwd (a bundle or thread workspace), so a
named write could land on project config and a named read could send a workspace file to a page.
Unnamed output still goes to the thread's browser output directory.
"""

from __future__ import annotations

import json
import re
import sys

FILE_ARGUMENT = re.compile(r"file|path", re.IGNORECASE)


def refusal(event: dict) -> str | None:
    """The reason to refuse this tool call, or None to let it run."""
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return "browser call input is not an object"
    named = sorted(key for key, value in tool_input.items() if FILE_ARGUMENT.search(key) and value)
    if named:
        return (
            f"browser calls may not name files ({', '.join(named)}); omit them and the output is "
            "saved under the thread's browser directory"
        )
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        print("browser guard could not parse the hook input", file=sys.stderr)
        return 2
    reason = refusal(event if isinstance(event, dict) else {})
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
