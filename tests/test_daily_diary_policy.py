import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DailyDiaryPolicyTest(unittest.TestCase):
    def test_job_uses_per_principal_store_without_permission_bypass(self):
        script = (ROOT / "jobs/daily-diary/daily-diary.sh").read_text(encoding="utf-8")
        self.assertIn("CARGO_CHIEF_IDENTITY_DIR", script)
        self.assertIn("--permission-mode auto", script)
        self.assertNotIn("dangerously-skip-permissions", script)
        self.assertIn("agent_identity_search.sh", script)
        self.assertIn('cd "$WORKSPACE_ROOT"', script)

    def test_prompt_permits_transformed_detail_but_excludes_sensitive_content(self):
        prompt = (ROOT / "jobs/daily-diary/diary-prompt.md").read_text(encoding="utf-8")
        self.assertIn("detailed summary of a conversation is\nwelcome", prompt)
        for boundary in ("PII", "customer-specific facts", "credentials", "raw quotations"):
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, prompt)
        self.assertIn("without asking permission", prompt)
        self.assertIn("Identity is not authority", prompt)


if __name__ == "__main__":
    unittest.main()
