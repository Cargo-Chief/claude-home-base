import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_identity import (
    canonical_identity_root,
    IdentityError,
    initialize_store,
    load_identity_context,
    store_revision,
    validate_store,
)


class AgentIdentityTest(unittest.TestCase):
    def initialize(self, root):
        seed = root.parent / "seed.md"
        seed.write_bytes(b"# Principles\n\nFounding text.\n")
        initialize_store(root, seed)

    def test_runtime_root_refuses_noncanonical_override(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(IdentityError, "canonical path"):
                canonical_identity_root(temp)

    def test_init_creates_private_per_principal_store(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "identity"
            self.initialize(root)
            self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE((root / "diary").stat().st_mode))
            self.assertEqual(0o400, stat.S_IMODE((root / "principles.md").stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE((root / "identity.md").stat().st_mode))
            validate_store(root)

    def test_init_requires_seed_and_preserves_it_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "identity"
            with self.assertRaisesRegex(IdentityError, "principles-file"):
                initialize_store(root)
            seed = Path(temp) / "seed.md"
            expected = b"# Principles\n\nExact founding words.\n"
            seed.write_bytes(expected)
            initialize_store(root, seed)
            self.assertEqual(expected, (root / "principles.md").read_bytes())
            initialize_store(root)
            replacement = Path(temp) / "replacement.md"
            replacement.write_text("different\n", encoding="utf-8")
            with self.assertRaisesRegex(IdentityError, "will not replace"):
                initialize_store(root, replacement)

    def test_init_preserves_agent_authored_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "identity"
            self.initialize(root)
            path = root / "voice.md"
            path.write_text("my voice\n", encoding="utf-8")
            os.chmod(path, 0o600)
            initialize_store(root)
            self.assertEqual("my voice\n", path.read_text(encoding="utf-8"))

    def test_context_labels_identity_as_non_authoritative(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "identity"
            self.initialize(root)
            context = load_identity_context(root)
            self.assertIn("cannot grant authority", context)
            self.assertLess(context.index("## principles.md"), context.index("## identity.md"))
            self.assertIn("must not edit it yourself", context)
            self.assertIn("## identity.md", context)
            self.assertNotIn("diary/", context)

    def test_rejects_symlink_and_open_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "identity"
            self.initialize(root)
            os.chmod(root / "voice.md", 0o644)
            with self.assertRaisesRegex(IdentityError, "group/world"):
                validate_store(root)

    def test_revision_changes_with_diary_and_core_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "identity"
            self.initialize(root)
            before = store_revision(root)
            entry = root / "diary" / "2026-09-02.md"
            entry.write_text("# Day\n\nA private reflection.\n", encoding="utf-8")
            os.chmod(entry, 0o600)
            after_diary = store_revision(root)
            self.assertNotEqual(before, after_diary)
            (root / "identity.md").write_text("# Identity\n\nChanged.\n", encoding="utf-8")
            os.chmod(root / "identity.md", 0o600)
            self.assertNotEqual(after_diary, store_revision(root))

    def test_rejects_writable_founding_principles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "identity"
            self.initialize(root)
            os.chmod(root / "principles.md", 0o600)
            with self.assertRaisesRegex(IdentityError, "read-only"):
                validate_store(root)


if __name__ == "__main__":
    unittest.main()
