import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bundle_guard


class BundleGuardTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for bundle in ("CN-1-mine", "CN-2-other"):
            (self.root / "worktrees" / bundle / "c4").mkdir(parents=True)
            (self.root / "worktrees" / bundle / "TASK.md").write_text("task\n")
        (self.root / "worktrees" / "docs-only" / "docs").mkdir(parents=True)
        self.threads = self.root / "work" / "home-base"
        self.work = self.state("mine", None)
        self.claim = self.work / "bundle-claim.txt"
        self.env = {
            "CARGO_CHIEF_ROOT": str(self.root),
            "CARGO_CHIEF_THREAD_WORK_DIR": str(self.work),
            "CARGO_CHIEF_BUNDLE_CLAIM_FILE": str(self.claim),
        }

    def state(self, key, bundle):
        path = self.threads / key
        path.mkdir(parents=True, exist_ok=True)
        (path / "state.json").write_text(json.dumps({"thread": key, **({"bundle": bundle} if bundle else {})}))
        return path

    def edit(self, path, cwd=None):
        return bundle_guard.refusal(
            {"tool_name": "Edit", "tool_input": {"file_path": path}, "cwd": cwd or str(self.work)}, self.env,
        )

    def bash(self, command, cwd=None):
        return bundle_guard.refusal(
            {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd or str(self.work)}, self.env,
        )

    def test_an_unbound_thread_may_not_edit_or_commit_in_a_bundle(self):
        bundle = self.root / "worktrees" / "CN-1-mine"
        self.assertIn("has not claimed CN-1-mine", self.edit(str(bundle / "c4" / "App.scala")))
        self.assertIn(str(self.claim), self.edit(str(bundle / "c4" / "App.scala")))
        for command in (
            f"git -C {bundle}/c4 commit -m x",
            f"cd {bundle}/c4 && git add -A && git push",
            "git -c core.x=1 push origin HEAD",
        ):
            self.assertIsNotNone(self.bash(command, cwd=str(bundle / "c4")), command)

    def test_reads_and_paths_outside_bundles_are_allowed(self):
        bundle = self.root / "worktrees" / "CN-1-mine"
        for command in (
            f"git -C {bundle}/c4 status && git -C {bundle}/c4 diff",
            f"cat {bundle}/TASK.md",
            f"git -C {self.root}/worktrees/docs-only/docs commit -m x",
            "git commit -m 'unbalanced",
        ):
            self.assertIsNone(self.bash(command), command)
        self.assertIsNone(self.edit(str(self.work / "notes.md")))
        self.assertIsNone(self.edit(str(self.root / "worktrees" / "docs-only" / "docs" / "x.md")))
        self.assertIsNone(self.edit(str(self.root / "worktrees" / "CN-3-missing" / "x")))

    def test_the_bound_bundle_and_an_unheld_pending_claim_may_be_written(self):
        mine = self.root / "worktrees" / "CN-1-mine" / "c4" / "x"
        self.state("mine", "CN-1-mine")
        self.assertIsNone(self.edit(str(mine)))
        self.assertIn("CN-2-other", self.edit(str(self.root / "worktrees" / "CN-2-other" / "x")))
        self.claim.write_text("CN-2-other\n")
        self.assertIsNone(self.edit(str(self.root / "worktrees" / "CN-2-other" / "x")))

    def test_a_pending_claim_for_a_bundle_another_thread_holds_is_refused(self):
        self.state("theirs", "CN-2-other")
        self.claim.write_text("CN-2-other")
        self.assertIn("CN-2-other", self.edit(str(self.root / "worktrees" / "CN-2-other" / "x")))

    def test_relative_paths_resolve_against_the_call_cwd(self):
        bundle = self.root / "worktrees" / "CN-1-mine"
        self.assertIsNotNone(self.edit("c4/App.scala", cwd=str(bundle)))
        self.assertIsNotNone(self.bash("cd c4 && git commit -m x", cwd=str(bundle)))
        self.assertIsNone(self.bash("cd /tmp && git commit -m x", cwd=str(bundle)))

    def test_sessions_outside_a_thread_and_other_tools_are_not_checked(self):
        path = str(self.root / "worktrees" / "CN-1-mine" / "x")
        self.assertIsNone(bundle_guard.refusal({"tool_name": "Edit", "tool_input": {"file_path": path}}, {}))
        self.assertIsNone(bundle_guard.refusal({"tool_name": "Read", "tool_input": {"file_path": path}}, self.env))

    def test_main_refuses_with_exit_2_and_skips_on_a_guard_fault(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": str(self.root / "worktrees" / "CN-1-mine" / "x")}}
        stderr = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(event))), mock.patch("sys.stderr", stderr), \
                mock.patch.dict("os.environ", self.env):
            self.assertEqual(2, bundle_guard.main())
        self.assertIn("has not claimed", stderr.getvalue())
        with mock.patch("sys.stdin", io.StringIO("{")), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(0, bundle_guard.main())


if __name__ == "__main__":
    unittest.main()
