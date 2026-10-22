import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
WRAPPER = ROOT / "with_harness_credential.py"
DIARY = ROOT / "jobs" / "daily-diary" / "daily-diary.sh"

try:
    import dotenv  # noqa: F401
    HAVE_DOTENV = True
except ImportError:
    HAVE_DOTENV = False

# A stand-in `claude` that reports only whether it received the expected value, never the value.
FAKE_CLAUDE = """#!{python}
import os, sys
sys.exit(0 if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "fake-token" else 3)
"""


class WithHarnessCredentialTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.secret = self.home / "home-base.env"
        self.bin = self.home / "bin"
        self.bin.mkdir()
        claude = self.bin / "claude"
        claude.write_text(FAKE_CLAUDE.format(python=sys.executable))
        claude.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_wrapper(self, *args, env_file=True, extra_env=None, path=None):
        env = {
            key: value for key, value in os.environ.items()
            if key not in ("CLAUDE_CODE_OAUTH_TOKEN", "CARGO_CHIEF_ENV_FILE")
        }
        env["HOME"] = str(self.home)
        env["PATH"] = path if path is not None else f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
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
        for args in ((), ("--",), ("claude", "-p"), ("--check", "extra")):
            with self.subTest(args=args):
                self.assertEqual(2, self.run_wrapper(*args).returncode)

    def test_runs_nothing_but_claude(self):
        self.write_secret("CLAUDE_CODE_OAUTH_TOKEN=fake-token\n")
        result = self.run_wrapper("--", sys.executable, "-c", "print('ran')")
        self.assertEqual(2, result.returncode)
        self.assertIn("runs only claude", result.stderr)
        self.assertNotIn("ran", result.stdout)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_claude_receives_the_token_from_the_secret_file(self):
        self.write_secret("CLAUDE_CODE_OAUTH_TOKEN=fake-token\n")
        result = self.run_wrapper("--", "claude", "-p", "hello")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("fake-token", result.stdout + result.stderr)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_file_value_replaces_a_stale_inherited_token(self):
        self.write_secret("CLAUDE_CODE_OAUTH_TOKEN=fake-token\n")
        result = self.run_wrapper(
            "--", "claude", extra_env={"CLAUDE_CODE_OAUTH_TOKEN": "stale"},
        )
        self.assertEqual(0, result.returncode, result.stderr)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_an_inherited_token_alone_is_refused(self):
        result = self.run_wrapper(
            "--", "claude", env_file=False, extra_env={"CLAUDE_CODE_OAUTH_TOKEN": "fake-token"},
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("harness credential unavailable", result.stderr)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_a_file_without_the_token_is_refused_even_with_an_inherited_one(self):
        self.write_secret("OTHER=value\n")
        result = self.run_wrapper(
            "--", "claude", extra_env={"CLAUDE_CODE_OAUTH_TOKEN": "fake-token"},
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("harness credential unavailable", result.stderr)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_refuses_an_unsafe_or_missing_configured_file(self):
        self.write_secret("CLAUDE_CODE_OAUTH_TOKEN=fake-token\n", mode=0o644)
        result = self.run_wrapper("--", "claude")
        self.assertEqual(2, result.returncode)
        self.assertIn("harness credential unavailable", result.stderr)
        self.assertNotIn("fake-token", result.stderr)

        self.secret.unlink()
        result = self.run_wrapper("--", "claude")
        self.assertEqual(2, result.returncode)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_check_reports_without_running_anything(self):
        self.write_secret("CLAUDE_CODE_OAUTH_TOKEN=fake-token\n")
        self.assertEqual(0, self.run_wrapper("--check").returncode)
        self.secret.unlink()
        self.assertEqual(2, self.run_wrapper("--check").returncode)

    @unittest.skipUnless(HAVE_DOTENV, "requires python-dotenv from requirements.txt")
    def test_reports_claude_missing_from_path(self):
        self.write_secret("CLAUDE_CODE_OAUTH_TOKEN=fake-token\n")
        empty = self.home / "empty"
        empty.mkdir()
        result = self.run_wrapper("--", "claude", path=str(empty))
        self.assertEqual(127, result.returncode)


class DiaryScriptStructureTest(unittest.TestCase):
    def setUp(self):
        text = DIARY.read_text(encoding="utf-8")
        # Join backslash continuations so each command is one line.
        self.commands = text.replace("\\\n", " ").splitlines()
        self.text = text

    def test_every_claude_invocation_goes_through_the_wrapper(self):
        invocations = [line for line in self.commands if "claude -p" in line]
        self.assertTrue(invocations)
        for line in invocations:
            self.assertIn('exec "$HOME_BASE_PYTHON" "$CREDENTIAL_WRAPPER" --', line)

    def test_credential_is_checked_in_dry_run_and_after_the_idempotency_skip(self):
        dry_run = self.text.index("if $DRY_RUN; then")
        skip = self.text.index("Diary already exists, skipping.")
        checks = [i for i, _ in enumerate(self.text) if self.text.startswith("credential_available", i)]
        self.assertTrue(any(dry_run < i < skip for i in checks))
        self.assertTrue(any(i > skip for i in checks))

    def test_interpreter_is_not_overridable(self):
        self.assertNotIn("CARGO_CHIEF_HOME_BASE_PYTHON", self.text)


if __name__ == "__main__":
    unittest.main()
