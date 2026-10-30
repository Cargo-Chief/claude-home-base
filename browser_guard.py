"""PreToolUse hook for the headless browser: refuse calls that name a file or leave the web.

Playwright MCP resolves a named file against the session cwd (a bundle or thread workspace), so a
named write could land on project config and a named read could send a workspace file to a page.
Its file: block checks only a URL's outer scheme, so a wrapped scheme such as view-source: can
still open a local file. Unnamed output still goes to the thread's browser output directory.
"""

from __future__ import annotations

import json
import re
import sys
from urllib.parse import urlsplit

FILE_ARGUMENT = re.compile(r"file|path", re.IGNORECASE)
URL_ARGUMENT = re.compile(r"url", re.IGNORECASE)
WEB_SCHEMES = {"http", "https"}


def _arguments(value, prefix=""):
    """Every (dotted key, value) pair in the input, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            yield name, str(key), item
            yield from _arguments(item, name)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _arguments(item, f"{prefix}[{index}]")


def _web_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value.strip() == "about:blank":
        return True
    try:
        return urlsplit(value.strip()).scheme.lower() in WEB_SCHEMES
    except ValueError:
        return False


def refusal(event: dict) -> str | None:
    """The reason to refuse this tool call, or None to let it run."""
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return "browser call input is not an object"
    named, off_web = [], []
    for name, key, value in _arguments(tool_input):
        if FILE_ARGUMENT.search(key) and value:
            named.append(name)
        elif URL_ARGUMENT.search(key) and value and not _web_url(value):
            off_web.append(name)
    if named:
        return (
            f"browser calls may not name files ({', '.join(sorted(named))}); omit them and the output "
            "is saved under the thread's browser directory"
        )
    if off_web:
        return f"browser URLs must be http, https or about:blank ({', '.join(sorted(off_web))})"
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        reason = refusal(event if isinstance(event, dict) else {})
    except BaseException:
        # Any exit other than 2 lets the call run, so every failure refuses it.
        print("browser guard could not check this call", file=sys.stderr)
        return 2
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
