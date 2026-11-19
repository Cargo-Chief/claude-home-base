#!/usr/bin/env python3
"""Start the pinned Notion MCP server with this principal's integration token.

Usage: notion_mcp.py --node <node> --cli <cli.mjs> --env-file <notion.env>

The harness declares this launcher, not the server, so the token never appears in the claude
command line or the claude process environment. It is read here from the principal's own env file
(`NOTION_TOKEN=...`, optionally prefixed with `export `), which must be owned by this user and
unreadable by anyone else, and handed only to the server process.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

TOKEN_KEY = "NOTION_TOKEN"
# The server process gets only these from the inherited environment, so nothing such as BASE_URL,
# a proxy or NODE_OPTIONS can redirect or alter the requests that carry the token.
INHERITED_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")


class LaunchError(Exception):
    pass


def _value(raw: str) -> str:
    """A shell-style value: quoted text, or the first word before any inline comment."""
    text = raw.strip()
    if text[:1] in ("'", '"'):
        end = text.find(text[0], 1)
        return text[1:end] if end > 0 else ""
    return text.split()[0] if text.split() else ""


def read_token(env_file: Path) -> str:
    """NOTION_TOKEN from a private env file; the error never contains the file's contents."""
    try:
        info = env_file.stat()
    except OSError as exc:
        raise LaunchError(f"{env_file} is not readable: {exc.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise LaunchError(f"{env_file} is not a regular file")
    if info.st_uid != os.getuid():
        raise LaunchError(f"{env_file} is not owned by this user")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise LaunchError(f"{env_file} is readable by others; chmod 600 it")
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise LaunchError(f"{env_file} could not be read") from None
    token = None
    for line in lines:
        text = line.strip()
        if text.startswith("export "):
            text = text[len("export "):].lstrip()
        key, sep, value = text.partition("=")
        if sep and key.strip() == TOKEN_KEY:
            token = _value(value)
    if not token:
        raise LaunchError(f"{TOKEN_KEY} is not set in {env_file}")
    return token


def env_file_problem(env_file: Path) -> str | None:
    """Why the launcher would refuse this file, or None; never returns the token."""
    try:
        read_token(env_file)
    except LaunchError as exc:
        return str(exc)
    return None


def server_env(base: dict[str, str], token: str) -> dict[str, str]:
    env = {key: base[key] for key in INHERITED_KEYS if key in base}
    env[TOKEN_KEY] = token
    return env


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--node", required=True)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--env-file", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print("usage: notion_mcp.py --node <node> --cli <cli.mjs> --env-file <notion.env>", file=sys.stderr)
        return 2
    try:
        token = read_token(Path(args.env_file))
    except LaunchError as exc:
        print(f"notion MCP not started: {exc}", file=sys.stderr)
        return 2
    command = [args.node, args.cli, "--transport", "stdio"]
    try:
        os.execve(args.node, command, server_env(dict(os.environ), token))
    except OSError as exc:
        print(f"notion MCP not started: cannot run {args.node}: {exc.strerror}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
