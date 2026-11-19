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
# The server also accepts headers carrying a token; an inherited one must never override ours.
SCRUBBED_KEYS = (TOKEN_KEY, "OPENAPI_MCP_HEADERS")


class LaunchError(Exception):
    pass


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
            token = value.strip().strip("'\"")
    if not token:
        raise LaunchError(f"{TOKEN_KEY} is not set in {env_file}")
    return token


def server_env(base: dict[str, str], token: str) -> dict[str, str]:
    env = {key: value for key, value in base.items() if key not in SCRUBBED_KEYS}
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
