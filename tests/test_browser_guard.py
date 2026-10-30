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

    def test_refuses_non_web_urls_including_wrapped_file_schemes(self):
        for url in ("view-source:file:///etc/hosts", "file:///etc/hosts", "chrome://settings", "javascript:alert(1)", "localhost:4200"):
            code, message = self.run_guard({"tool_input": {"url": url}})
            self.assertEqual(2, code, url)
            self.assertIn("must be http, https or about:blank", message)
        for url in ("http://localhost:4200/x", "https://dev.example.com", "about:blank", " HTTPS://x.test"):
            self.assertEqual(0, self.run_guard({"tool_input": {"url": url}})[0], url)

    def test_checks_nested_arguments(self):
        for tool_input in ({"fields": [{"name": "a", "filePath": "x"}]}, {"options": {"url": "file:///x"}}):
            self.assertEqual(2, self.run_guard({"tool_input": tool_input})[0], tool_input)
        self.assertEqual(0, self.run_guard({"tool_input": {"fields": [{"name": "a", "value": "b"}]}})[0])

    def test_any_failure_refuses(self):
        deep = "[" * 200000 + "]" * 200000
        code, message = self.run_guard('{"tool_input": {"filename": "CLAUDE.md", "x": ' + deep + "}}")
        self.assertEqual(2, code)
        with mock.patch("browser_guard.refusal", side_effect=RuntimeError("boom")):
            self.assertEqual(2, self.run_guard({"tool_input": {}})[0])

    def test_fails_closed_on_malformed_input(self):
        self.assertEqual(2, self.run_guard("not json")[0])
        self.assertEqual(2, self.run_guard({"tool_input": "x"})[0])
        self.assertEqual(2, self.run_guard([])[0])


if __name__ == "__main__":
    unittest.main()
