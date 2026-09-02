import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
WRAPPER = ROOT / "search" / "cargo_chief_search.sh"


class CargoChiefSearchWrapperTest(unittest.TestCase):
    def test_wrapper_supplies_private_cache_and_curated_config(self):
        with tempfile.TemporaryDirectory() as value:
            base = Path(value)
            workspace = base / "cargo_chief"
            (workspace / "docs").mkdir(parents=True)
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


if __name__ == "__main__":
    unittest.main()
