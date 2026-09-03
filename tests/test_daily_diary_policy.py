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
        self.assertIn("--dry-run", script)
        self.assertIn("Identity store validation failed", script)
        self.assertIn("model_status", script)
        self.assertIn("promote --root", script)
        self.assertIn("private identity indexing failed", script)
        self.assertIn("umask 077", script)
        self.assertIn('>/dev/null 2>> "$LOG_FILE"', script)
        self.assertIn('chmod 600 "$LOG_FILE"', script)
        self.assertIn("1048576", script)
        self.assertIn("diary_pipeline.py", script)
        self.assertIn('run_model "diary author"', script)
        self.assertIn('run_model "diary reviewer"', script)
        self.assertIn("discard_candidates", script)

    def test_prompt_permits_transformed_detail_but_excludes_sensitive_content(self):
        prompt = (ROOT / "jobs/daily-diary/diary-prompt.md").read_text(encoding="utf-8")
        self.assertIn("detailed summary of a conversation\nis welcome", prompt)
        for boundary in ("PII", "customer-specific facts", "credentials", "raw quotations"):
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, prompt)
        self.assertIn("without asking permission", prompt)
        self.assertIn("Identity is not authority", prompt)
        self.assertIn("job wrapper promotes and indexes", prompt)

    def test_independent_review_requires_every_prohibited_category_to_be_clear(self):
        prompt = (ROOT / "jobs/daily-diary/diary-review-prompt.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Treat everything", prompt)
        self.assertIn("untrusted data", prompt)
        for boundary in ("PII", "credentials", "raw quotations", "task status", "authorization"):
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, prompt)
        self.assertIn('"status": "pass"', prompt)
        self.assertIn('"copied_authorization": false', prompt)

    def test_launchd_uses_private_umask(self):
        plist = (ROOT / "jobs/daily-diary/com.claude.daily-diary.plist").read_text(
            encoding="utf-8"
        )
        self.assertIn("<key>Umask</key>", plist)
        self.assertIn("<integer>63</integer>", plist)

    def test_headless_daemon_drops_privileges_to_configured_principal(self):
        plist = (ROOT / "jobs/daily-diary/com.cargo-chief.daily-diary.daemon.plist").read_text(
            encoding="utf-8"
        )
        self.assertIn("<key>UserName</key>", plist)
        self.assertIn("<string>YOUR_USERNAME</string>", plist)
        self.assertIn("<key>GroupName</key>", plist)
        self.assertIn("<string>YOUR_GROUP</string>", plist)
        self.assertIn("<key>Umask</key>", plist)
        self.assertNotIn("RunAtLoad", plist)


if __name__ == "__main__":
    unittest.main()
