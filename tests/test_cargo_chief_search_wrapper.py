import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
WRAPPER = ROOT / "search" / "cargo_chief_search.sh"
INHERITED_KNOBS = ("CARGO_CHIEF_SEARCH_INLINE_LIMIT", "CARGO_CHIEF_INDEX_LOCK_TIMEOUT")


class CargoChiefSearchWrapperTest(unittest.TestCase):
    def _initialize_docs_repo(self, workspace):
        docs = workspace / "docs"
        docs.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(docs)], check=True)
        subprocess.run(["git", "-C", str(docs), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(docs), "config", "user.email", "test@example.com"], check=True)
        (docs / "README.md").write_text("initial\n")
        subprocess.run(["git", "-C", str(docs), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(docs), "commit", "-qm", "initial"], check=True)
        return docs

    def test_wrapper_supplies_private_cache_and_curated_config(self):
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            workspace = base / "cargo_chief"
            self._initialize_docs_repo(workspace)
            state = base / "state"
            capture = base / "capture"
            fake_python = base / "python"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" > \"$CAPTURE/args\"\n"
                "printf '%s\\n' \"$CARGO_CHIEF_SEARCH_DIR\" \"$XDG_CACHE_HOME\" "
                "\"$HF_HOME\" \"$FASTEMBED_CACHE_PATH\" > \"$CAPTURE/env\"\n"
            )
            fake_python.chmod(0o700)
            capture.mkdir()
            env = os.environ | {
                "CARGO_CHIEF_ROOT": str(workspace),
                "CARGO_CHIEF_SEARCH_DIR": str(state),
                "CARGO_CHIEF_SEARCH_PYTHON": str(fake_python),
                "CAPTURE": str(capture),
            }
            subprocess.run(["bash", str(WRAPPER), "status", "--json"], env=env, check=True)

            args = (capture / "args").read_text().splitlines()
            self.assertEqual(str(ROOT / "search" / "agent_search.py"), args[0])
            self.assertEqual("--config", args[1])
            self.assertEqual(str(ROOT / "search" / "config.cargo-chief.yaml.example"), args[2])
            self.assertEqual(["status", "--json"], args[3:])
            self.assertEqual(
                [str(state), str(state / "cache"), str(state / "cache" / "huggingface"), str(state / "model")],
                (capture / "env").read_text().splitlines(),
            )
            self.assertEqual(0o700, state.stat().st_mode & 0o777)

    def _environment(self, base, workspace, **extra):
        """Fake agent_search: logs each call, answers `pending` from FAKE_PENDING, fails on FAKE_FAIL."""
        capture = base / "calls"
        fake_python = base / "python"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "{ printf 'CALL %s %s\\n' \"$(ps -o nice= -p $$ | tr -d ' ')\" \"${CARGO_CHIEF_INDEX_LOCK_TIMEOUT:-unset}\"\n"
            "  printf '%s\\n' \"${@:4}\"; } >> \"$CAPTURE\"\n"
            "[[ \"$4\" == \"${FAKE_FAIL:-}\" ]] && exit 1\n"
            "[[ \"$4\" == pending ]] && printf '%s\\n' \"$FAKE_PENDING\"\n"
            "exit 0\n"
        )
        fake_python.chmod(0o700)
        inherited = {k: v for k, v in os.environ.items() if k not in INHERITED_KNOBS}
        return capture, inherited | {
            "CARGO_CHIEF_ROOT": str(workspace),
            "CARGO_CHIEF_SEARCH_DIR": str(base / "state"),
            "CARGO_CHIEF_SEARCH_PYTHON": str(fake_python),
            "CAPTURE": str(capture),
            "FAKE_PENDING": '{"files": 5, "indexed_files": 5, "to_embed": 1}',
        } | extra

    def _calls(self, capture):
        """Return (niceness, lock timeout, args after the program and config) per fake call."""
        if not capture.exists():
            return []
        calls = []
        for call in capture.read_text().split("CALL ")[1:]:
            header, *args = call.splitlines()
            nice, lock_timeout = header.split()
            calls.append((int(nice), lock_timeout, args))
        return calls

    def _args(self, capture):
        return [args for _, _, args in self._calls(capture)]

    def _wrapper(self, variables, *args):
        return subprocess.run(
            ["/bin/bash", str(WRAPPER), *args], env=variables, capture_output=True, text=True,
        )

    def _commit_docs_change(self, docs, name="README.md", text="changed\n"):
        (docs / name).write_text(text)
        subprocess.run(["git", "-C", str(docs), "add", name], check=True)
        subprocess.run(["git", "-C", str(docs), "commit", "-qm", f"change {name}"], check=True)

    def _workspace(self, value, **extra):
        base = Path(value)
        workspace = base / "cargo_chief"
        docs = self._initialize_docs_repo(workspace)
        capture, variables = self._environment(base, workspace, **extra)
        return base, docs, capture, variables

    def test_search_without_an_index_fails_fast_and_never_builds_inline(self):
        with tempfile.TemporaryDirectory() as value:
            _, _, capture, variables = self._workspace(
                value, FAKE_PENDING='{"files": 290, "indexed_files": 0, "to_embed": 290}')

            result = self._wrapper(variables, "search", "durable decisions", "--json")

            self.assertEqual(3, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("cargo_chief_search.sh index", result.stderr)
            self.assertEqual([["pending"]], self._args(capture))

    def test_index_records_revision_so_search_goes_straight_to_retrieval(self):
        with tempfile.TemporaryDirectory() as value:
            base, _, capture, variables = self._workspace(value)

            self.assertEqual(0, self._wrapper(variables, "index").returncode)
            self.assertEqual(0o600, (base / "state" / "index-revision").stat().st_mode & 0o777)
            result = self._wrapper(variables, "search", "durable decisions", "--json")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([["index"], ["search", "durable decisions", "--json"]], self._args(capture))

    def test_existing_index_without_a_marker_refreshes_instead_of_reporting_missing(self):
        with tempfile.TemporaryDirectory() as value:
            base, _, capture, variables = self._workspace(
                value, FAKE_PENDING='{"files": 290, "indexed_files": 290, "to_embed": 0}')

            result = self._wrapper(variables, "search", "query")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([["pending"], ["index"], ["search", "query"]], self._args(capture))
            self.assertTrue((base / "state" / "index-revision").exists())

    def test_small_gap_refreshes_inline_at_low_priority_without_waiting_for_the_lock(self):
        with tempfile.TemporaryDirectory() as value:
            _, docs, capture, variables = self._workspace(value)
            self._wrapper(variables, "index")
            self._commit_docs_change(docs)

            result = self._wrapper(variables, "search", "after docs change", "--json")

            self.assertEqual(0, result.returncode, result.stderr)
            calls = self._calls(capture)
            self.assertEqual(["pending"], calls[1][2])
            nice, lock_timeout, args = calls[2]
            self.assertEqual(["index"], args)
            self.assertGreaterEqual(nice, 10)
            self.assertEqual("0", lock_timeout)
            self.assertEqual(["search", "after docs change", "--json"], calls[3][2])

            self._wrapper(variables, "search", "again")
            self.assertEqual(["search", "again"], self._args(capture)[4])

    def test_large_gap_searches_stale_index_with_a_warning(self):
        with tempfile.TemporaryDirectory() as value:
            base, docs, capture, variables = self._workspace(
                value, CARGO_CHIEF_SEARCH_INLINE_LIMIT="1",
                FAKE_PENDING='{"files": 290, "indexed_files": 280, "to_embed": 2}')
            self._wrapper(variables, "index")
            marker = (base / "state" / "index-revision").read_text()
            self._commit_docs_change(docs)

            result = self._wrapper(variables, "search", "query", "--json")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("2 files to embed", result.stderr)
            self.assertEqual([["index"], ["pending"], ["search", "query", "--json"]], self._args(capture))
            self.assertEqual(marker, (base / "state" / "index-revision").read_text())

    def test_failed_inline_refresh_still_answers_from_the_stale_index(self):
        with tempfile.TemporaryDirectory() as value:
            base, docs, capture, variables = self._workspace(value)
            self._wrapper(variables, "index")
            marker = (base / "state" / "index-revision").read_text()
            self._commit_docs_change(docs)

            result = self._wrapper(variables | {"FAKE_FAIL": "index"}, "search", "query")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("refresh failed", result.stderr)
            self.assertEqual(["search", "query"], self._args(capture)[-1])
            self.assertEqual(marker, (base / "state" / "index-revision").read_text())

    def test_unmeasurable_gap_searches_with_a_warning(self):
        with tempfile.TemporaryDirectory() as value:
            _, docs, capture, variables = self._workspace(value, FAKE_FAIL="pending")
            self._wrapper(variables, "index")
            self._commit_docs_change(docs)

            result = self._wrapper(variables, "search", "query")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("unknown files to embed", result.stderr)
            self.assertEqual(["search", "query"], self._args(capture)[-1])

    def test_index_help_does_not_record_a_marker(self):
        with tempfile.TemporaryDirectory() as value:
            base, _, _, variables = self._workspace(value)

            self.assertEqual(0, self._wrapper(variables, "index", "--help").returncode)

            self.assertFalse((base / "state" / "index-revision").exists())

    def test_rebuild_clears_the_marker_before_purging(self):
        with tempfile.TemporaryDirectory() as value:
            base, _, _, variables = self._workspace(value)
            self._wrapper(variables, "index")

            result = self._wrapper(variables | {"FAKE_FAIL": "rebuild"}, "rebuild")

            self.assertNotEqual(0, result.returncode)
            self.assertFalse((base / "state" / "index-revision").exists())

    def test_purge_clears_the_marker_only_after_it_succeeds(self):
        with tempfile.TemporaryDirectory() as value:
            base, _, _, variables = self._workspace(value)
            self._wrapper(variables, "index")
            marker = base / "state" / "index-revision"

            self.assertNotEqual(0, self._wrapper(variables | {"FAKE_FAIL": "purge"}, "purge").returncode)
            self.assertTrue(marker.exists())

            self.assertEqual(0, self._wrapper(variables, "purge").returncode)
            self.assertFalse(marker.exists())

    def test_inline_limit_rejects_non_integers_and_leading_zeros(self):
        with tempfile.TemporaryDirectory() as value:
            _, _, _, variables = self._workspace(value)

            for limit in ("many", "08", "-1"):
                result = self._wrapper(variables | {"CARGO_CHIEF_SEARCH_INLINE_LIMIT": limit}, "search", "q")
                self.assertEqual(2, result.returncode, limit)
                self.assertIn("CARGO_CHIEF_SEARCH_INLINE_LIMIT", result.stderr)

            status = self._wrapper(variables | {"CARGO_CHIEF_SEARCH_INLINE_LIMIT": "many"}, "status")
            self.assertEqual(0, status.returncode, status.stderr)

    def test_non_search_commands_do_not_need_a_git_docs_repo(self):
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            workspace = base / "cargo_chief"
            (workspace / "docs").mkdir(parents=True)
            capture, variables = self._environment(base, workspace)

            for command in ("doctor", "status"):
                result = self._wrapper(variables, command)
                self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([["doctor"], ["status"]], self._args(capture))


if __name__ == "__main__":
    unittest.main()
