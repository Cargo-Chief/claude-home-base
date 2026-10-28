from pathlib import Path
import os
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))

from process_control import (
    _process_table,
    close_process_tree,
    descendants,
    living,
    stop_process_tree,
)


def _alive(pids):
    return living(pids, _process_table() or {})


class FakeProc:
    """A parent that exits on SIGTERM, driven by a scripted process table."""

    def __init__(self, pid=100):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("claude", timeout)
        return self.returncode


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ProcessTreeUnitTest(unittest.TestCase):
    def run_stop(self, proc, tables, *, exits_on=signal.SIGTERM, **kwargs):
        clock, sent = Clock(), []
        tables = list(tables)

        def table():
            clock.now += 0.25
            return tables.pop(0) if len(tables) > 1 else tables[0]

        def send(pids, sig):
            sent.append((list(pids), sig))
            if proc.pid in pids and sig == exits_on:
                proc.returncode = -sig

        stopped = stop_process_tree(
            proc, grace=1, kill_wait=1, table=table, send=send, clock=clock, sleep=clock.sleep, **kwargs
        )
        return stopped, sent

    def test_descendants_walk_the_whole_tree(self):
        table = {10: (1, "S"), 11: (10, "S"), 12: (11, "S"), 13: (10, "S"), 99: (1, "S")}
        self.assertEqual([11, 13, 12], descendants(10, table))

    def test_zombies_and_missing_pids_are_not_living(self):
        table = {11: (10, "Z"), 12: (10, "S+")}
        self.assertEqual([12], living([11, 12, 13], table))

    def test_an_unreadable_process_table_is_never_a_confirmed_stop(self):
        stopped, sent = self.run_stop(FakeProc(), [None])
        self.assertFalse(stopped)
        self.assertEqual(([100], signal.SIGTERM), sent[0])

    def test_a_table_lost_mid_stop_does_not_read_as_all_gone(self):
        tables = [{100: (1, "S"), 101: (100, "S")}, None]
        stopped, sent = self.run_stop(FakeProc(), tables)
        self.assertFalse(stopped)
        self.assertIn(([101], signal.SIGKILL), sent)

    def test_children_forked_after_the_snapshot_are_killed_and_checked(self):
        before = {100: (1, "S"), 101: (100, "S")}
        forked = {101: (1, "S"), 102: (101, "S")}
        stopped, sent = self.run_stop(FakeProc(), [before, forked])
        self.assertFalse(stopped)
        self.assertIn(([101, 102], signal.SIGKILL), sent)

    def test_an_already_reaped_process_is_never_walked(self):
        proc = FakeProc()
        proc.returncode = 0
        stopped, sent = self.run_stop(proc, [{100: (1, "S"), 101: (100, "S")}])
        self.assertTrue(stopped)
        self.assertEqual([], sent)


class ProcessTreeLiveTest(unittest.TestCase):
    def spawn(self, script, children=2):
        proc = subprocess.Popen(["/bin/sh", "-c", script])
        self.addCleanup(self.reap, proc)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(descendants(proc.pid)) < children:
            time.sleep(0.05)
        self.tree = descendants(proc.pid)
        self.assertEqual(children, len(self.tree))
        return proc

    def reap(self, proc):
        for pid in [proc.pid] + getattr(self, "tree", []):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        proc.wait()

    def test_stops_the_parent_and_its_children(self):
        proc = self.spawn("sleep 60 & sleep 60 & wait")
        self.assertTrue(stop_process_tree(proc, grace=5))
        self.assertIsNotNone(proc.poll())
        self.assertEqual([], _alive(self.tree))

    def test_escalates_to_sigkill_for_children_that_ignore_sigterm(self):
        proc = self.spawn("trap '' TERM; sleep 60 & sleep 60 & wait")
        self.assertTrue(stop_process_tree(proc, grace=0.5, kill_wait=5))
        self.assertEqual([], _alive(self.tree))

    def test_reports_failure_when_the_tree_survives(self):
        proc = self.spawn("sleep 60 & sleep 60 & wait")
        stopped = stop_process_tree(proc, grace=0.2, kill_wait=0.2, send=lambda pids, sig: None)
        self.assertFalse(stopped)
        self.assertEqual(2, len(_alive(self.tree)))

    def test_closing_input_stops_children_left_behind(self):
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & read line"], stdin=subprocess.PIPE)
        self.addCleanup(self.reap, proc)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not descendants(proc.pid):
            time.sleep(0.05)
        self.tree = descendants(proc.pid)
        self.assertEqual(1, len(self.tree))

        self.assertTrue(close_process_tree(proc, proc.stdin.close, wait=5))
        self.assertEqual([], _alive(self.tree))

    def test_an_exited_process_is_already_stopped(self):
        proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
        proc.wait()
        self.assertTrue(stop_process_tree(proc, grace=1))


if __name__ == "__main__":
    unittest.main()
