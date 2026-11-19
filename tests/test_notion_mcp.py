import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from notion_mcp import LaunchError, read_token, server_env

LAUNCHER = Path(__file__).resolve().parents[1] / "notion_mcp.py"


class NotionLauncherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env_file = self.root / "token-file"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, text, mode=0o600):
        self.env_file.write_text(text)
        os.chmod(self.env_file, mode)

    def test_reads_plain_exported_and_quoted_tokens(self):
        for text in (
            "NOTION_TOKEN=ntn_a\n", "export NOTION_TOKEN=ntn_a\n", "# c\nNOTION_TOKEN='ntn_a'\n",
            "export NOTION_TOKEN=ntn_a  # ned integration\n", 'NOTION_TOKEN="ntn_a" # c\n',
        ):
            self.write(text)
            self.assertEqual("ntn_a", read_token(self.env_file))

    def test_refuses_a_file_others_can_read(self):
        self.write("NOTION_TOKEN=secret-value\n", mode=0o644)
        with self.assertRaisesRegex(LaunchError, "chmod 600") as caught:
            read_token(self.env_file)
        self.assertNotIn("secret-value", str(caught.exception))

    def test_refuses_a_missing_file_or_token(self):
        with self.assertRaisesRegex(LaunchError, "not readable"):
            read_token(self.env_file)
        self.write("OTHER=secret-value\n")
        with self.assertRaisesRegex(LaunchError, "is not set") as caught:
            read_token(self.env_file)
        self.assertNotIn("secret-value", str(caught.exception))

    def test_server_env_is_an_allowlist_plus_the_file_token(self):
        inherited = {
            "PATH": "/bin", "HOME": "/h", "NOTION_TOKEN": "old", "OPENAPI_MCP_HEADERS": "{}",
            "BASE_URL": "http://elsewhere", "HTTPS_PROXY": "http://p", "NODE_OPTIONS": "--require x",
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
        }
        self.assertEqual({"PATH": "/bin", "HOME": "/h", "NOTION_TOKEN": "new"}, server_env(inherited, "new"))

    def test_launcher_execs_node_with_only_the_file_token(self):
        self.write("NOTION_TOKEN=from-file\n")
        fake_node = self.root / "node"
        fake_node.write_text('#!/bin/sh\necho "$1 $2 $3 token=$NOTION_TOKEN headers=${OPENAPI_MCP_HEADERS:-none}"\n')
        os.chmod(fake_node, 0o700)
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), "--node", str(fake_node), "--cli", "/x/cli.mjs", "--env-file", str(self.env_file)],
            capture_output=True, text=True, env={**os.environ, "NOTION_TOKEN": "inherited", "OPENAPI_MCP_HEADERS": "{}"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("/x/cli.mjs --transport stdio token=from-file headers=none", result.stdout.strip())

    def test_launcher_fails_closed_without_printing_the_token(self):
        self.write("NOTION_TOKEN=secret-value\n", mode=0o640)
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), "--node", "/bin/false", "--cli", "/x", "--env-file", str(self.env_file)],
            capture_output=True, text=True,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("notion MCP not started", result.stderr)
        self.assertNotIn("secret-value", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
