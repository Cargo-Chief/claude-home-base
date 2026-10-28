from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))

from process_control import descendants, living, stop_process_tree


def _spawn(script):
    proc = subprocess.Popen(["/bin/sh", "-c", script])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(descendants(proc.pid)) < 2:
        time.sleep(0.05)
    return proc


class ProcessTreeTest(unittest.TestCase):
    def test_descendants_walk_the_whole_tree(self):
        table = {10: (1, "S"), 11: (10, "S"), 12: (11, "S"), 13: (10, "S"), 99: (1, "S")}
        self.assertEqual([11, 13, 12], descendants(10, table))

    def test_zombies_and_missing_pids_are_not_living(self):
        table = {11: (10, "Z"), 12: (10, "S+")}
        self.assertEqual([12], living([11, 12, 13], table))

    def test_stops_the_parent_and_its_children(self):
        proc = _spawn("sleep 60 & sleep 60 & wait")
        children = descendants(proc.pid)
        self.assertEqual(2, len(children))

        self.assertTrue(stop_process_tree(proc, grace=5))
        self.assertIsNotNone(proc.poll())
        self.assertEqual([], living(children))

    def test_escalates_to_sigkill_for_children_that_ignore_sigterm(self):
        proc = _spawn("trap '' TERM; sleep 60 & sleep 60 & wait")
        children = descendants(proc.pid)

        started = time.monotonic()
        self.assertTrue(stop_process_tree(proc, grace=0.5, kill_wait=5))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([], living(children))

    def test_reports_failure_when_the_tree_survives(self):
        proc = _spawn("sleep 60 & sleep 60 & wait")
        children = descendants(proc.pid)
        sent = []
        try:
            stopped = stop_process_tree(
                proc, grace=0.2, kill_wait=0.2, send=lambda pids, sig: sent.append((pids, sig)),
            )
            self.assertFalse(stopped)
            self.assertEqual([signal.SIGTERM, signal.SIGTERM, signal.SIGKILL], [sig for _, sig in sent])
            self.assertIn(proc.pid, sent[-1][0])
        finally:
            stop_process_tree(proc, grace=2)
        self.assertEqual([], living(children))

    def test_an_exited_process_is_already_stopped(self):
        proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
        proc.wait()
        self.assertTrue(stop_process_tree(proc, grace=1))


if __name__ == "__main__":
    unittest.main()
