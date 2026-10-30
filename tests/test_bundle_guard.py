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
        for bundle in ("CN-1-mine", "CN-2-other", "CN-3-third"):
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
            "CARGO_CHIEF_MAX_LIVE_BUNDLES": "3",
        }
        self.mine = self.root / "worktrees" / "CN-1-mine"

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
        self.assertIn("has not claimed CN-1-mine", self.edit(str(self.mine / "c4" / "App.scala")))
        self.assertIn(str(self.claim), self.edit(str(self.mine / "c4" / "App.scala")))
        for command in (
            f"git -C {self.mine}/c4 commit -m x",
            f"cd {self.mine}/c4 && git add -A && git push",
            "git -c core.x=1 push origin HEAD",
        ):
            self.assertIsNotNone(self.bash(command, cwd=str(self.mine / "c4")), command)

    def test_ordinary_shell_forms_do_not_hide_a_git_write(self):
        bundle = f"{self.mine}/c4"
        for command in (
            f"# commit the fix\ngit -C {bundle} commit -am fix",
            f"git -C {bundle} log --grep=#12 && git -C {bundle} push",
            f"GIT_EDITOR=true git -C {bundle} commit",
            f"python3 docs/scripts/ops/with_github_app_token.py -- git -C {bundle} push",
            f"env git -C {bundle} commit -m x",
            f"(cd {bundle} && git commit -m x)",
            f"bash -c 'cd {bundle} && git push'",
            "git -C $CARGO_CHIEF_ROOT/worktrees/CN-1-mine/c4 commit -m x",
            "cd ${CARGO_CHIEF_ROOT}/worktrees/CN-1-mine/c4; git push",
            f"git --git-dir={bundle}/.git --work-tree={bundle} restore x",
            f"git -C {bundle} push>/dev/null",
            f"git -C {bundle} clean -fdx",
            f"git -C {bundle} branch -D feature",
            f"git -C {bundle} worktree remove --force .",
            f"git -C {bundle} stash pop",
        ):
            self.assertIsNotNone(self.bash(command), command)

    def test_reads_and_paths_outside_bundles_are_allowed(self):
        bundle = f"{self.mine}/c4"
        for command in (
            f"git -C {bundle} status && git -C {bundle} diff",
            f"cat {self.mine}/TASK.md",
            f"git -C {bundle} stash list",
            f"git -C {bundle} branch -a",
            f"git -C {bundle} tag -l",
            f"git -C {bundle} worktree list",
            f"git -C {bundle} config --get user.email",
            f"git -C {self.root}/worktrees/docs-only/docs commit -m x",
            "git commit -m x",
        ):
            self.assertIsNone(self.bash(command), command)
        self.assertIsNone(self.edit(str(self.work / "notes.md")))
        self.assertIsNone(self.edit(str(self.root / "worktrees" / "docs-only" / "docs" / "x.md")))
        self.assertIsNone(self.edit(str(self.root / "worktrees" / "CN-9-missing" / "x")))

    def test_the_bound_bundle_and_an_acceptable_pending_claim_may_be_written(self):
        self.state("mine", "CN-1-mine")
        self.assertIsNone(self.edit(str(self.mine / "c4" / "x")))
        self.assertIsNone(self.bash(f"cd {self.mine}/c4 && git commit -m x"))
        other = str(self.root / "worktrees" / "CN-2-other" / "x")
        self.assertIn("CN-2-other", self.edit(other))
        self.claim.write_text("CN-2-other\n")
        self.assertIsNone(self.edit(other))

    def test_a_pending_claim_is_refused_where_the_turn_end_check_would_refuse_it(self):
        other = str(self.root / "worktrees" / "CN-2-other" / "x")
        self.claim.write_text("CN-2-other")
        self.state("theirs", "CN-2-other")
        self.assertIn("CN-2-other", self.edit(other))
        self.state("theirs", None)
        rival = self.state("rival", None)
        (rival / "bundle-claim.txt").write_text("CN-2-other\n")
        self.assertIn("CN-2-other", self.edit(other))
        (rival / "bundle-claim.txt").unlink()
        self.state("a", "CN-1-mine")
        self.state("b", "CN-3-third")
        self.env["CARGO_CHIEF_MAX_LIVE_BUNDLES"] = "2"
        self.assertIn("CN-2-other", self.edit(other))
        self.env["CARGO_CHIEF_MAX_LIVE_BUNDLES"] = "3"
        self.assertIsNone(self.edit(other))

    def test_a_binding_another_thread_also_holds_is_not_trusted(self):
        self.state("mine", "CN-2-other")
        self.state("theirs", "CN-2-other")
        self.assertIn("CN-2-other", self.edit(str(self.root / "worktrees" / "CN-2-other" / "x")))

    def test_undecodable_claim_or_state_refuses_rather_than_disabling_the_guard(self):
        self.claim.write_bytes(b"\xff")
        self.assertIn("CN-1-mine", self.edit(str(self.mine / "x")))
        (self.threads / "other").mkdir()
        (self.threads / "other" / "state.json").write_bytes(b"\xff")
        self.assertIn("CN-1-mine", self.edit(str(self.mine / "x")))

    def test_relative_paths_resolve_against_the_call_cwd(self):
        self.assertIsNotNone(self.edit("c4/App.scala", cwd=str(self.mine)))
        self.assertIsNotNone(self.bash("cd c4 && git commit -m x", cwd=str(self.mine)))
        self.assertIsNone(self.bash("git -C ../.. status", cwd=str(self.mine)))

    def test_sessions_outside_a_thread_and_other_tools_are_not_checked(self):
        path = str(self.mine / "x")
        self.assertIsNone(bundle_guard.refusal({"tool_name": "Edit", "tool_input": {"file_path": path}}, {}))
        self.assertIsNone(bundle_guard.refusal({"tool_name": "Read", "tool_input": {"file_path": path}}, self.env))

    def test_main_refuses_with_exit_2_and_skips_on_a_guard_fault(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": str(self.mine / "x")}}
        stderr = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(event))), mock.patch("sys.stderr", stderr), \
                mock.patch.dict("os.environ", self.env):
            self.assertEqual(2, bundle_guard.main())
        self.assertIn("has not claimed", stderr.getvalue())
        with mock.patch("sys.stdin", io.StringIO("{")), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(0, bundle_guard.main())


if __name__ == "__main__":
    unittest.main()
