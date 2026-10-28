"""Stop a child process together with everything it started, and confirm they are gone."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

GRACE_SECONDS = 10.0
KILL_WAIT_SECONDS = 5.0
ORPHAN_GRACE_SECONDS = 2.0
POLL_SECONDS = 0.25

Table = dict[int, tuple[int, str]]


def _process_table() -> Optional[Table]:
    """pid -> (ppid, state) from one ps snapshot; None when ps fails, which is never 'no processes'."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,stat="],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"ps failed; process tree unknown: {exc}")
        return None
    table = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2])
    return table


def descendants(pid: int, table: Optional[Table] = None) -> list[int]:
    """Every descendant of pid, parents before children."""
    table = _process_table() if table is None else table
    children: dict[int, list[int]] = {}
    for child, (parent, _) in (table or {}).items():
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    queue = [pid]
    while queue:
        for child in children.get(queue.pop(0), []):
            if child not in found:
                found.append(child)
                queue.append(child)
    return found


def living(pids: list[int], table: Table) -> list[int]:
    """The pids still running; a zombie has exited and only waits to be reaped."""
    return [pid for pid in pids if pid in table and not table[pid][1].startswith("Z")]


def _signal_all(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


class _Tree:
    """The pids a stop is responsible for: the snapshot, plus whatever its members fork later."""

    def __init__(self, proc: subprocess.Popen, table: Callable[[], Optional[Table]]) -> None:
        self.proc = proc
        self._table = table
        snapshot = table()
        self.seen_whole = snapshot is not None
        self.pids = descendants(proc.pid, snapshot) if snapshot is not None else []

    def alive(self) -> Optional[list[int]]:
        """Descendants still running, adopting children forked since the last look; None when ps failed."""
        current = self._table()
        if current is None:
            return None
        roots = [self.proc.pid] if self.proc.poll() is None else []
        for pid in roots + living(self.pids, current):
            for child in descendants(pid, current):
                if child not in self.pids:
                    self.pids.append(child)
        return living(self.pids, current)


def stop_process_tree(
    proc: subprocess.Popen,
    *,
    grace: float = GRACE_SECONDS,
    kill_wait: float = KILL_WAIT_SECONDS,
    table: Callable[[], Optional[Table]] = _process_table,
    send: Callable[[list[int], int], None] = _signal_all,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """SIGTERM proc and its descendants, SIGKILL whatever outlives the grace period.

    True only once proc is reaped and every descendant seen is gone. An unreadable process
    table is never read as an empty tree: the stop still happens but is reported unconfirmed.
    """
    if proc.poll() is not None:
        # Already reaped: the pid may belong to another process now, and orphans are not findable.
        return True
    tree = _Tree(proc, table)

    def wait_until(deadline: float) -> bool:
        while True:
            try:
                proc.wait(timeout=max(0.0, min(POLL_SECONDS, deadline - clock())))
            except subprocess.TimeoutExpired:
                pass
            if proc.poll() is not None and tree.alive() == []:
                return True
            if clock() >= deadline:
                return False
            if proc.poll() is not None:
                sleep(POLL_SECONDS)

    send([proc.pid], signal.SIGTERM)
    send(tree.pids, signal.SIGTERM)
    if not wait_until(clock() + grace):
        alive = tree.alive()
        survivors = list(tree.pids) if alive is None else alive
        if proc.poll() is None:
            survivors = [proc.pid] + survivors
        logger.warning(f"pid={proc.pid}: {len(survivors)} process(es) outlived SIGTERM; sending SIGKILL")
        send(survivors, signal.SIGKILL)
        if not wait_until(clock() + kill_wait):
            logger.warning(f"pid={proc.pid}: could not confirm the process tree exited after SIGKILL")
            return False
    if not tree.seen_whole:
        logger.warning(f"pid={proc.pid}: exited, but its descendants could not be listed")
    return tree.seen_whole


def close_process_tree(
    proc: subprocess.Popen,
    close: Callable[[], None],
    *,
    wait: float,
    table: Callable[[], Optional[Table]] = _process_table,
    send: Callable[[list[int], int], None] = _signal_all,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Ask proc to exit by closing its input, then stop the tree if it does not or left children behind."""
    if proc.poll() is not None:
        return True
    tree = _Tree(proc, table)
    try:
        close()
    except OSError:
        pass
    try:
        proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        return stop_process_tree(proc, table=table, send=send, clock=clock, sleep=sleep)
    left = tree.alive()
    if left:
        logger.warning(f"pid={proc.pid}: exited and left {len(left)} process(es) running; stopping them")
        send(left, signal.SIGTERM)
        deadline = clock() + ORPHAN_GRACE_SECONDS
        while left and clock() < deadline:
            sleep(POLL_SECONDS)
            left = tree.alive()
        if left:
            send(left, signal.SIGKILL)
            sleep(POLL_SECONDS)
            left = tree.alive()
    return tree.seen_whole and left == []
