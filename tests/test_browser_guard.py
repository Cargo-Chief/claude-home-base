import io
import json
import unittest
from unittest import mock

import browser_guard


class BrowserGuardTest(unittest.TestCase):
    def run_guard(self, event):
        stdin = io.StringIO(event if isinstance(event, str) else json.dumps(event))
        stderr = io.StringIO()
        with mock.patch("sys.stdin", stdin), mock.patch("sys.stderr", stderr):
            return browser_guard.main(), stderr.getvalue()

    def test_allows_calls_without_file_arguments(self):
        code, _ = self.run_guard({"tool_name": "mcp__playwright__browser_navigate", "tool_input": {"url": "http://localhost:4200"}})
        self.assertEqual(0, code)
        code, _ = self.run_guard({"tool_input": {"type": "png", "filename": ""}})
        self.assertEqual(0, code)

    def test_refuses_named_files_and_paths(self):
        for tool_input in (
            {"filename": ".claude/settings.json"},
            {"paths": ["/repo/.envrc"]},
            {"part": "response-body", "outputFile": "CLAUDE.md"},
            {"filePath": "x"},
        ):
            code, message = self.run_guard({"tool_input": tool_input})
            self.assertEqual(2, code, tool_input)
            self.assertIn("may not name files", message)

    def test_fails_closed_on_malformed_input(self):
        self.assertEqual(2, self.run_guard("not json")[0])
        self.assertEqual(2, self.run_guard({"tool_input": "x"})[0])
        self.assertEqual(2, self.run_guard([])[0])


if __name__ == "__main__":
    unittest.main()
