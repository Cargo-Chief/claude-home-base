import json
from pathlib import Path
import subprocess
import sys
import unittest

from notion_guard import refusal

GUARD = Path(__file__).resolve().parents[1] / "notion_guard.py"


class NotionGuardTest(unittest.TestCase):
    def call(self, tool_input):
        return refusal({"tool_name": "mcp__notion__API-patch-page", "tool_input": tool_input})

    def test_allows_ordinary_reads_and_updates(self):
        self.assertIsNone(self.call({"page_id": "p1"}))
        self.assertIsNone(self.call({"page_id": "p1", "properties": {"Verified": {"checkbox": True}}}))
        self.assertIsNone(self.call({"page_id": "p1", "in_trash": False, "archived": None}))
        self.assertIsNone(self.call({"type": "update_content", "update_content": {"content_updates": []}}))

    def test_refuses_trash_archive_and_content_deletion_at_any_depth(self):
        self.assertIn("in_trash", self.call({"page_id": "p1", "in_trash": True}))
        self.assertIn("archived", self.call({"block_id": "b1", "archived": True}))
        self.assertIn(
            "replace_content.allow_deleting_content",
            self.call({"type": "replace_content", "replace_content": {"new_str": "x", "allow_deleting_content": True}}),
        )
        self.assertIn("children[0].is_archived", self.call({"children": [{"is_archived": "true"}]}))

    def test_refuses_flags_inside_a_json_string_argument(self):
        self.assertIn("in_trash", self.call({"body": json.dumps({"in_trash": True})}))

    def test_refuses_flags_the_server_would_decode_from_wrapped_strings(self):
        inner = json.dumps({"new_str": "x", "allow_deleting_content": True})
        for wrapped in (json.dumps(inner), json.dumps(json.dumps(inner)), "\ufeff" + inner, inner + "\u00a0"):
            self.assertIn("allow_deleting_content", self.call({"type": "replace_content", "replace_content": wrapped}))

    def test_refuses_truthy_strings_any_case_and_erase_content(self):
        self.assertIn("in_trash", self.call({"in_trash": "TRUE"}))
        self.assertIn("In_Trash", self.call({"In_Trash": True}))
        self.assertIn("erase_content", self.call({"page_id": "p1", "erase_content": True}))

    def test_allows_database_properties_named_like_flags(self):
        self.assertIsNone(self.call({"properties": {"archived": {"checkbox": False}}}))
        self.assertIsNone(self.call({"properties": {"Archived": {"checkbox": True}}}))
        self.assertIsNone(self.call({"properties": json.dumps({"in_trash": {"checkbox": True}})}))

    def test_refuses_input_that_is_not_an_object(self):
        self.assertIsNotNone(refusal({"tool_input": "in_trash"}))
        self.assertIsNotNone(refusal({}))

    def run_guard(self, stdin):
        result = subprocess.run([sys.executable, str(GUARD)], input=stdin, capture_output=True, text=True)
        return result.returncode, result.stderr

    def test_hook_exits_2_to_refuse_and_0_to_allow(self):
        self.assertEqual(0, self.run_guard(json.dumps({"tool_input": {"page_id": "p1"}}))[0])
        code, err = self.run_guard(json.dumps({"tool_input": {"in_trash": True}}))
        self.assertEqual(2, code)
        self.assertIn("in_trash", err)

    def test_hook_refuses_when_it_cannot_parse_the_event(self):
        code, err = self.run_guard("not json")
        self.assertEqual(2, code)
        self.assertIn("could not check", err)


if __name__ == "__main__":
    unittest.main()
