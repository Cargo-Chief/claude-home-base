import json
import stat
import tempfile
import unittest
from pathlib import Path

from agent_identity import AGENT_FILES, IdentityError, initialize_store
from diary_pipeline import PROHIBITED, REVIEWED_FILES, discard, prepare, promote, validate_review


class DiaryPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "identity"
        principles = self.base / "principles.md"
        principles.write_text("# Principles\n\nHold the foundation.\n", encoding="utf-8")
        initialize_store(self.root, principles)
        self.date = "2026-09-03"

    def tearDown(self):
        self.temp.cleanup()

    def _write_pass_candidates(self, stage):
        (stage / "diary.md").write_text("# Reflection\n\nA private lesson.\n", encoding="utf-8")
        (stage / "diary.md").chmod(0o600)
        receipt = {
            "status": "pass",
            "prohibited": {name: False for name in PROHIBITED},
            "reviewed_files": list(REVIEWED_FILES),
        }
        (stage / "review.json").write_text(json.dumps(receipt), encoding="utf-8")
        (stage / "review.json").chmod(0o600)

    def test_prepare_copies_core_into_private_quarantine(self):
        stage = prepare(self.root, self.date)
        self.assertEqual(0o700, stat.S_IMODE(stage.stat().st_mode))
        for name in AGENT_FILES:
            self.assertEqual((self.root / name).read_bytes(), (stage / name).read_bytes())
            self.assertEqual(0o600, stat.S_IMODE((stage / name).stat().st_mode))

    def test_review_fails_closed_when_a_category_is_missing(self):
        stage = prepare(self.root, self.date)
        self._write_pass_candidates(stage)
        receipt = json.loads((stage / "review.json").read_text())
        del receipt["prohibited"][PROHIBITED[0]]
        (stage / "review.json").write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(IdentityError, "prohibited category"):
            validate_review(self.root, self.date)

    def test_review_detects_founding_principles_change(self):
        stage = prepare(self.root, self.date)
        self._write_pass_candidates(stage)
        principles = self.root / "principles.md"
        principles.chmod(0o600)
        principles.write_text("changed", encoding="utf-8")
        principles.chmod(0o400)
        with self.assertRaisesRegex(IdentityError, "founding principles changed"):
            validate_review(self.root, self.date)

    def test_promote_moves_only_reviewed_candidates_then_removes_stage(self):
        stage = prepare(self.root, self.date)
        self._write_pass_candidates(stage)
        (stage / "identity.md").write_text("# Identity\n\nReviewed evolution.\n", encoding="utf-8")
        target = promote(self.root, self.date)
        self.assertEqual("# Reflection\n\nA private lesson.\n", target.read_text())
        self.assertEqual("# Identity\n\nReviewed evolution.\n", (self.root / "identity.md").read_text())
        self.assertFalse(stage.exists())
        self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))

    def test_discard_removes_only_the_named_stage(self):
        stage = prepare(self.root, self.date)
        other = stage.parent / "2026-09-02"
        other.mkdir(mode=0o700)
        discard(self.root, self.date)
        self.assertFalse(stage.exists())
        self.assertTrue(other.exists())


if __name__ == "__main__":
    unittest.main()
