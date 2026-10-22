import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
WRAPPER = ROOT / "with_harness_credential.py"

try:
    import dotenv  # noqa: F401
    HAVE_DOTENV = True
except ImportError:
    HAVE_DOTENV = False

# The child reports only whether it received the expected value, never the value itself.
PROBE = (
    "import os,sys; "
    "sys.exit(0 if os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') == 'fake-token' else 3)"
)


class WithHarnessCredentialTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.secret = self.home / "home-base.env"

    def tearDown(self):
        self.temp.cleanup()

    def run_wrapper(self, *args, env_file=True, extra_env=None):
        env = {
            key: value for key, value in os.environ.items()
            if key not in ("CLAUDE_CODE_OAUTH_TOKEN", "CARGO_CHIEF_ENV_FILE")
        }
        env["HOME"] = str(self.home)
        if env_file:
            env["CARGO_CHIEF_ENV_FILE"] = str(self.secret)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(WRAPPER), *args],
            capture_output=True, text=True, env=env, timeout=30,
        )

    def write_secret(self, text, mode=0o600):
        self.secret.write_text(text)
        self.secret.chmod(mode)

    def test_usage_requires_a_separator_and_command(self):
        for args in ((), ("--",), ("claude", "-p")):
            with self.subTest(args=args):
                self.assertEqual(2, self.run_wrapper(*args).returncode)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_child_receives_the_token_from_the_secret_file(self):
        self.write_secret("CLAUDE_CODE_OAUTH_TOKEN=fake-token\n")
        result = self.run_wrapper("--", sys.executable, "-c", PROBE)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("fake-token", result.stdout + result.stderr)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_file_value_replaces_a_stale_inherited_token(self):
        self.write_secret("CLAUDE_CODE_OAUTH_TOKEN=fake-token\n")
        result = self.run_wrapper(
            "--", sys.executable, "-c", PROBE,
            extra_env={"CLAUDE_CODE_OAUTH_TOKEN": "stale"},
        )
        self.assertEqual(0, result.returncode, result.stderr)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_refuses_when_no_credential_is_configured(self):
        result = self.run_wrapper("--", sys.executable, "-c", PROBE, env_file=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("harness credential missing", result.stderr)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_refuses_a_file_without_the_token(self):
        self.write_secret("OTHER=value\n")
        result = self.run_wrapper("--", sys.executable, "-c", PROBE)
        self.assertEqual(2, result.returncode)
        self.assertIn("harness credential missing", result.stderr)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_refuses_an_unsafe_or_missing_configured_file(self):
        self.write_secret("CLAUDE_CODE_OAUTH_TOKEN=fake-token\n", mode=0o644)
        result = self.run_wrapper("--", sys.executable, "-c", PROBE)
        self.assertEqual(2, result.returncode)
        self.assertIn("harness credential refused", result.stderr)
        self.assertNotIn("fake-token", result.stderr)

        self.secret.unlink()
        result = self.run_wrapper("--", sys.executable, "-c", PROBE)
        self.assertEqual(2, result.returncode)
        self.assertIn("harness credential refused", result.stderr)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_reports_a_missing_command(self):
        self.write_secret("CLAUDE_CODE_OAUTH_TOKEN=fake-token\n")
        result = self.run_wrapper("--", "no-such-command-for-this-test")
        self.assertEqual(127, result.returncode)


class DiaryScriptStructureTest(unittest.TestCase):
    def test_diary_runs_claude_through_the_credential_wrapper(self):
        script = (ROOT / "jobs" / "daily-diary" / "daily-diary.sh").read_text(encoding="utf-8")
        self.assertIn('"$HOME_BASE_PYTHON" "$CREDENTIAL_WRAPPER" -- \\\n        claude -p', script)
        self.assertIn('if ! "$HOME_BASE_PYTHON" "$CREDENTIAL_WRAPPER" -- true; then', script)
        bare = [
            line for line in script.splitlines()
            if "claude -p" in line and "CREDENTIAL_WRAPPER" not in line
            and not line.lstrip().startswith("claude -p")
        ]
        self.assertEqual([], bare)


if __name__ == "__main__":
    unittest.main()
