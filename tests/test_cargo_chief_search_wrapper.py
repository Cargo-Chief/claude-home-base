import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
WRAPPER = ROOT / "search" / "cargo_chief_search.sh"


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
        capture = base / "calls"
        fake_python = base / "python"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'CALL %s\\n' \"$(ps -o nice= -p $$ | tr -d ' ')\" >> \"$CAPTURE\"\n"
            "printf '%s\\n' \"$@\" >> \"$CAPTURE\"\n"
        )
        fake_python.chmod(0o700)
        return capture, os.environ | {
            "CARGO_CHIEF_ROOT": str(workspace),
            "CARGO_CHIEF_SEARCH_DIR": str(base / "state"),
            "CARGO_CHIEF_SEARCH_PYTHON": str(fake_python),
            "CAPTURE": str(capture),
        } | extra

    def _calls(self, capture):
        """Return (niceness, args) per fake-python call, args minus the program and config."""
        if not capture.exists():
            return []
        calls = []
        for call in capture.read_text().split("CALL ")[1:]:
            lines = call.splitlines()
            calls.append((int(lines[0]), lines[4:]))
        return calls

    def _wrapper(self, env, *args):
        return subprocess.run(
            ["bash", str(WRAPPER), *args], env=env, capture_output=True, text=True,
        )

    def _commit_docs_change(self, docs, name="README.md", text="changed\n"):
        (docs / name).write_text(text)
        subprocess.run(["git", "-C", str(docs), "add", name], check=True)
        subprocess.run(["git", "-C", str(docs), "commit", "-qm", f"change {name}"], check=True)

    def test_search_without_an_index_fails_fast_and_never_builds_inline(self):
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            workspace = base / "cargo_chief"
            self._initialize_docs_repo(workspace)
            capture, env = self._environment(base, workspace)

            result = self._wrapper(env, "search", "durable decisions", "--json")

            self.assertEqual(3, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("cargo_chief_search.sh index", result.stderr)
            self.assertEqual([], self._calls(capture))

    def test_index_records_revision_so_search_goes_straight_to_retrieval(self):
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            workspace = base / "cargo_chief"
            self._initialize_docs_repo(workspace)
            capture, env = self._environment(base, workspace)

            self.assertEqual(0, self._wrapper(env, "index").returncode)
            self.assertEqual(0o600, (base / "state" / "index-revision").stat().st_mode & 0o777)
            result = self._wrapper(env, "search", "durable decisions", "--json")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                [["index"], ["search", "durable decisions", "--json"]],
                [args for _, args in self._calls(capture)],
            )

    def test_small_docs_change_refreshes_inline_at_low_priority(self):
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            workspace = base / "cargo_chief"
            docs = self._initialize_docs_repo(workspace)
            capture, env = self._environment(base, workspace)
            self._wrapper(env, "index")
            self._commit_docs_change(docs)

            result = self._wrapper(env, "search", "after docs change", "--json")

            self.assertEqual(0, result.returncode, result.stderr)
            calls = self._calls(capture)
            self.assertEqual(["index"], calls[1][1])
            self.assertGreaterEqual(calls[1][0], 10)
            self.assertEqual(["search", "after docs change", "--json"], calls[2][1])

            self._wrapper(env, "search", "again")
            self.assertEqual(["search", "again"], self._calls(capture)[3][1])

    def test_large_docs_change_searches_stale_index_with_a_warning(self):
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            workspace = base / "cargo_chief"
            docs = self._initialize_docs_repo(workspace)
            capture, env = self._environment(base, workspace, CARGO_CHIEF_SEARCH_INLINE_LIMIT="1")
            self._wrapper(env, "index")
            marker = (base / "state" / "index-revision").read_text()
            self._commit_docs_change(docs, "a.md", "a\n")
            self._commit_docs_change(docs, "b.md", "b\n")

            result = self._wrapper(env, "search", "query", "--json")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("2 changed files", result.stderr)
            self.assertEqual([["index"], ["search", "query", "--json"]], [a for _, a in self._calls(capture)])
            self.assertEqual(marker, (base / "state" / "index-revision").read_text())

    def test_unknown_indexed_revision_searches_with_a_warning(self):
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            workspace = base / "cargo_chief"
            self._initialize_docs_repo(workspace)
            capture, env = self._environment(base, workspace)
            state = base / "state"
            state.mkdir()
            (state / "index-revision").write_text("0" * 40 + ":stale\n")

            result = self._wrapper(env, "search", "query")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("unknown changed files", result.stderr)
            self.assertEqual([["search", "query"]], [a for _, a in self._calls(capture)])

    def test_purge_clears_the_revision_marker(self):
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            workspace = base / "cargo_chief"
            self._initialize_docs_repo(workspace)
            capture, env = self._environment(base, workspace)
            self._wrapper(env, "index")

            self.assertEqual(0, self._wrapper(env, "purge").returncode)

            self.assertFalse((base / "state" / "index-revision").exists())
            self.assertEqual(3, self._wrapper(env, "search", "query").returncode)

    def test_inline_limit_must_be_an_integer(self):
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            workspace = base / "cargo_chief"
            self._initialize_docs_repo(workspace)
            capture, env = self._environment(base, workspace, CARGO_CHIEF_SEARCH_INLINE_LIMIT="many")

            result = self._wrapper(env, "status")

            self.assertEqual(2, result.returncode)
            self.assertIn("CARGO_CHIEF_SEARCH_INLINE_LIMIT", result.stderr)

if __name__ == "__main__":
    unittest.main()
