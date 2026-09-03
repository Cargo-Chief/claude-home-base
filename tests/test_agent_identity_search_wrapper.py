import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
WRAPPER = ROOT / "search" / "agent_identity_search.sh"


class AgentIdentitySearchWrapperTest(unittest.TestCase):
    def test_explicit_index_records_revision_and_avoids_repeat_refresh(self):
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            identity = home / ".local/share/cargo-chief/identity"
            (identity / "diary").mkdir(parents=True)
            search_dir = home / ".local/state/cargo-chief/identity-search"
            capture = home / "calls"
            fake_python = home / "python"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == */agent_identity.py ]]; then\n"
                "  [[ \"${@: -1}\" == revision ]] && printf 'identity-revision\\n'\n"
                "  exit 0\n"
                "fi\n"
                "printf 'CALL\\n' >> \"$CAPTURE\"\n"
                "printf '%s\\n' \"$@\" >> \"$CAPTURE\"\n"
            )
            fake_python.chmod(0o700)
            env = os.environ | {
                "HOME": str(home),
                "CARGO_CHIEF_IDENTITY_DIR": str(identity),
                "CARGO_CHIEF_IDENTITY_SEARCH_DIR": str(search_dir),
                "CARGO_CHIEF_SEARCH_PYTHON": str(fake_python),
                "CAPTURE": str(capture),
            }

            subprocess.run(["bash", str(WRAPPER), "index"], env=env, check=True)
            revision = search_dir / "index-revision"
            self.assertTrue(revision.is_file())
            self.assertEqual(0o600, revision.stat().st_mode & 0o777)

            subprocess.run(
                ["bash", str(WRAPPER), "search", "personal principle"],
                env=env,
                check=True,
            )
            calls = capture.read_text().split("CALL\n")[1:]
            parsed = [call.splitlines() for call in calls]
            common = [
                str(ROOT / "search" / "agent_search.py"),
                "--config",
                str(ROOT / "search" / "config.agent-identity.yaml.example"),
            ]
            self.assertEqual(common + ["index"], parsed[0])
            self.assertEqual(common + ["search", "personal principle"], parsed[1])


if __name__ == "__main__":
    unittest.main()
