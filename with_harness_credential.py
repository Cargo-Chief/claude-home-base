#!/usr/bin/env python3
"""Run one command with the harness's Claude credential in its environment.

Usage: with_harness_credential.py -- <command> [args...]

Processes the bot starts inherit the token it loads from its secret env file. A job started some
other way, such as the nightly diary, does not, and its `claude` would fall back to an interactive
login that expires. This reads the token through the same validated loader the governed launcher
uses, sets it only on the child, and refuses to run without it rather than fall back.
"""

from __future__ import annotations

import os
import sys

from governed_delegation import DELEGATE_CREDENTIAL_KEY, DelegationError, _delegate_credential_env


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "--":
        print("usage: with_harness_credential.py -- <command> [args...]", file=sys.stderr)
        return 2
    command = argv[1:]
    try:
        env = _delegate_credential_env(os.environ)
    except DelegationError as exc:
        print(f"harness credential refused: {exc}", file=sys.stderr)
        return 2
    if not env.get(DELEGATE_CREDENTIAL_KEY):
        print(f"harness credential missing: {DELEGATE_CREDENTIAL_KEY} is not configured", file=sys.stderr)
        return 2
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        print(f"cannot run {command[0]}: {exc.strerror}", file=sys.stderr)
        return 127
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
