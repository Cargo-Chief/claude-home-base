"""Stop a child process together with everything it started, and confirm they are gone."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from typing import Callable

logger = logging.getLogger(__name__)

GRACE_SECONDS = 10.0
KILL_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.2


def _process_table() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, state) from one ps snapshot; empty when ps is unavailable."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,stat="],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2])
    return table


def descendants(pid: int, table: dict[int, tuple[int, str]] | None = None) -> list[int]:
    """Every descendant of pid, parents before children."""
    table = _process_table() if table is None else table
    children: dict[int, list[int]] = {}
    for child, (parent, _) in table.items():
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    queue = [pid]
    while queue:
        for child in children.get(queue.pop(0), []):
            if child not in found:
                found.append(child)
                queue.append(child)
    return found


def living(pids: list[int], table: dict[int, tuple[int, str]] | None = None) -> list[int]:
    """The pids still running; a zombie has exited and only waits to be reaped."""
    table = _process_table() if table is None else table
    return [pid for pid in pids if pid in table and not table[pid][1].startswith("Z")]


def _signal_all(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def stop_process_tree(
    proc: subprocess.Popen,
    *,
    grace: float = GRACE_SECONDS,
    kill_wait: float = KILL_WAIT_SECONDS,
    table: Callable[[], dict[int, tuple[int, str]]] = _process_table,
    send: Callable[[list[int], int], None] = _signal_all,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """SIGTERM proc and its descendants, SIGKILL whatever outlives the grace period.

    The tree is read before the first signal, because a child whose parent has exited is
    re-parented and can no longer be found from proc. True once proc is reaped and no
    descendant is still running.
    """
    tree = descendants(proc.pid, table())

    def remaining() -> list[int]:
        return living(tree, table()) if tree else []

    def wait_until(deadline: float) -> bool:
        while True:
            try:
                proc.wait(timeout=max(0.0, min(POLL_SECONDS, deadline - clock())))
                if not remaining():
                    return True
            except subprocess.TimeoutExpired:
                pass
            if clock() >= deadline:
                return False
            sleep(POLL_SECONDS)

    if proc.poll() is None:
        send([proc.pid], signal.SIGTERM)
    send(tree, signal.SIGTERM)
    if wait_until(clock() + grace):
        return True

    survivors = remaining()
    if proc.poll() is None:
        survivors = [proc.pid] + survivors
    logger.warning(f"pid={proc.pid}: {len(survivors)} process(es) outlived SIGTERM; sending SIGKILL")
    send(survivors, signal.SIGKILL)
    if wait_until(clock() + kill_wait):
        return True
    logger.warning(f"pid={proc.pid}: still running after SIGKILL: {[proc.pid] if proc.poll() is None else remaining()}")
    return False
