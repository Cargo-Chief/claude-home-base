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

    def test_search_refreshes_index_before_query(self):
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            workspace = base / "cargo_chief"
            docs = self._initialize_docs_repo(workspace)
            capture = base / "calls"
            fake_python = base / "python"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'CALL\\n' >> \"$CAPTURE\"\n"
                "printf '%s\\n' \"$@\" >> \"$CAPTURE\"\n"
            )
            fake_python.chmod(0o700)
            env = os.environ | {
                "CARGO_CHIEF_ROOT": str(workspace),
                "CARGO_CHIEF_SEARCH_DIR": str(base / "state"),
                "CARGO_CHIEF_SEARCH_PYTHON": str(fake_python),
                "CAPTURE": str(capture),
            }
            subprocess.run(
                ["bash", str(WRAPPER), "search", "durable decisions", "--json"],
                env=env,
                check=True,
            )

            calls = (capture.read_text()).split("CALL\n")[1:]
            parsed = [call.splitlines() for call in calls]
            common = [
                str(ROOT / "search" / "agent_search.py"),
                "--config",
                str(ROOT / "search" / "config.cargo-chief.yaml.example"),
            ]
            self.assertEqual(common + ["index"], parsed[0])
            self.assertEqual(common + ["search", "durable decisions", "--json"], parsed[1])

            subprocess.run(
                ["bash", str(WRAPPER), "search", "another query", "--json"],
                env=env,
                check=True,
            )
            calls = (capture.read_text()).split("CALL\n")[1:]
            parsed = [call.splitlines() for call in calls]
            self.assertEqual(common + ["search", "another query", "--json"], parsed[2])

            (docs / "README.md").write_text("changed\n")
            subprocess.run(["git", "-C", str(docs), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(docs), "commit", "-qm", "changed"], check=True)
            subprocess.run(
                ["bash", str(WRAPPER), "search", "after docs change", "--json"],
                env=env,
                check=True,
            )
            calls = (capture.read_text()).split("CALL\n")[1:]
            parsed = [call.splitlines() for call in calls]
            self.assertEqual(common + ["index"], parsed[3])
            self.assertEqual(common + ["search", "after docs change", "--json"], parsed[4])
            self.assertEqual(0o600, (base / "state" / "index-revision").stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
