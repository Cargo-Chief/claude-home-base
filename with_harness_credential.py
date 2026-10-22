#!/usr/bin/env python3
"""Run `claude` with the harness's Claude credential in its environment.

Usage: with_harness_credential.py -- claude [args...]
       with_harness_credential.py --check

Processes the bot starts inherit the token it loads from its secret env file. A job started some
other way, such as the nightly diary, does not, and its `claude` would fall back to an interactive
login that expires. This reads the token only from that file, through the same validated loader the
governed launcher uses, and refuses to run without it. It runs nothing but `claude`, so it cannot be
used to hand the token to an arbitrary command. `--check` validates the credential and runs nothing.
"""

from __future__ import annotations

import os
import sys

from governed_delegation import DELEGATE_CREDENTIAL_KEY, DelegationError, _delegate_credential_env

ALLOWED_COMMAND = "claude"
USAGE = "usage: with_harness_credential.py -- claude [args...] | --check"


def credential_env(base: dict[str, str]) -> dict[str, str]:
    """The environment with the token from the secret file; never an inherited value."""
    inherited_removed = {key: value for key, value in base.items() if key != DELEGATE_CREDENTIAL_KEY}
    env = _delegate_credential_env(inherited_removed)
    if not env.get(DELEGATE_CREDENTIAL_KEY):
        raise DelegationError(f"{DELEGATE_CREDENTIAL_KEY} is not in the harness secret env file")
    return env


def main(argv: list[str]) -> int:
    check_only = argv == ["--check"]
    if not check_only and (len(argv) < 2 or argv[0] != "--"):
        print(USAGE, file=sys.stderr)
        return 2
    if not check_only and os.path.basename(argv[1]) != ALLOWED_COMMAND:
        print(f"with_harness_credential.py runs only {ALLOWED_COMMAND}", file=sys.stderr)
        return 2
    try:
        env = credential_env(dict(os.environ))
    except DelegationError as exc:
        print(f"harness credential unavailable: {exc}", file=sys.stderr)
        return 2
    if check_only:
        return 0
    command = argv[1:]
    try:
        os.execvpe(command[0], command, env)
    except FileNotFoundError:
        print(f"cannot run {command[0]}: not found", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"cannot run {command[0]}: {exc.strerror}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
