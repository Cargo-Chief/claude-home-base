import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "search" / "agent_search.py"
SPEC = importlib.util.spec_from_file_location("agent_search", MODULE_PATH)
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)


class CargoChiefSearchPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.workspace = self.base / "cargo_chief"
        self.docs = self.workspace / "docs"
        self.docs.mkdir(parents=True)
        self.runtime = self.base / "runtime"
        self.cfg = {
            "mode": "cargo-chief-docs",
            "docs_root": str(self.docs),
            "database": str(self.runtime / "knowledge.db"),
            "directories": [
                {"path": str(self.docs), "name": "docs", "type": "markdown", "include": []}
            ],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_accepts_docs_only_external_database(self):
        search._validate_cargo_chief_config(self.cfg, self.workspace, self.runtime)
        self.assertEqual(str(self.docs.resolve()), self.cfg["docs_root"])

    def test_rejects_jsonl_and_outside_sources(self):
        self.cfg["directories"][0]["type"] = "jsonl"
        with self.assertRaisesRegex(ValueError, "markdown sources only"):
            search._validate_cargo_chief_config(self.cfg, self.workspace, self.runtime)

        outside = self.base / "outside"
        outside.mkdir()
        self.cfg["directories"][0].update(path=str(outside), type="markdown")
        with self.assertRaisesRegex(ValueError, "outside docs_root"):
            search._validate_cargo_chief_config(self.cfg, self.workspace, self.runtime)

    def test_rejects_database_inside_workspace_and_symlink_source(self):
        self.cfg["database"] = str(self.workspace / "knowledge.db")
        with self.assertRaisesRegex(ValueError, "outside the Cargo Chief workspace"):
            search._validate_cargo_chief_config(self.cfg, self.workspace, self.runtime)

        self.cfg["database"] = str(self.runtime / "knowledge.db")
        real = self.docs / "real"
        real.mkdir()
        linked = self.docs / "linked"
        linked.symlink_to(real, target_is_directory=True)
        self.cfg["directories"][0]["path"] = str(linked)
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            search._validate_cargo_chief_config(self.cfg, self.workspace, self.runtime)

    def test_requires_canonical_docs_and_private_search_directory(self):
        other_docs = self.workspace / "other-docs"
        other_docs.mkdir()
        self.cfg["docs_root"] = str(other_docs)
        self.cfg["directories"][0]["path"] = str(other_docs)
        with self.assertRaisesRegex(ValueError, "canonical"):
            search._validate_cargo_chief_config(self.cfg, self.workspace, self.runtime)

        self.cfg["docs_root"] = str(self.docs)
        self.cfg["directories"][0]["path"] = str(self.docs)
        self.cfg["database"] = str(self.base / "other-runtime" / "knowledge.db")
        with self.assertRaisesRegex(ValueError, "CARGO_CHIEF_SEARCH_DIR"):
            search._validate_cargo_chief_config(self.cfg, self.workspace, self.runtime)

    def test_enumerator_is_positive_and_skips_symlink_files(self):
        (self.docs / "keep.md").write_text("keep")
        (self.docs / "drop.md").write_text("drop")
        (self.docs / "note.txt").write_text("note")
        (self.docs / "linked.md").symlink_to(self.docs / "keep.md")
        source = {"path": str(self.docs), "include": ["keep.md", "linked.md"], "exclude": []}
        self.assertEqual([str((self.docs / "keep.md").resolve())], search.enumerate_markdown_files(source))
        source["include"] = []
        self.assertNotIn(
            str((self.docs / "note.txt").resolve()),
            search.enumerate_markdown_files(source, markdown_only=True),
        )

    def test_duplicate_source_ownership_is_refused(self):
        path = str(self.docs / "same.md")
        with self.assertRaisesRegex(ValueError, "multiple sources"):
            search.validate_source_file_ownership({"a": {path}, "b": {path}})

    def test_reconcile_removes_missing_files_and_removed_sources(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, source TEXT, file_path TEXT)")
        db.execute("CREATE TABLE documents_vec (rowid INTEGER)")
        db.executemany(
            "INSERT INTO documents VALUES (?, ?, ?)",
            [(1, "docs", "/keep.md"), (2, "docs", "/gone.md"), (3, "old", "/old.md")],
        )
        db.executemany("INSERT INTO documents_vec VALUES (?)", [(1,), (2,), (3,)])
        self.assertEqual(2, search.reconcile_sources(db, {"docs": {"/keep.md"}}))
        self.assertEqual([("/keep.md",)], db.execute("SELECT file_path FROM documents").fetchall())
        self.assertEqual([(1,)], db.execute("SELECT rowid FROM documents_vec").fetchall())
        db.close()

    def test_decision_metadata_keeps_only_lifecycle_fields(self):
        record = self.docs / "decisions" / "ADR-0001.md"
        record.parent.mkdir()
        record.write_text(
            "---\ndecision_type: architecture\ndecision_status: superseded\n"
            "superseded_by: [ADR-0002]\nsecret: never-copy\n---\n# Decision\n"
        )
        fake_yaml = types.SimpleNamespace(
            safe_load=lambda _: {
                "decision_type": "architecture",
                "decision_status": "superseded",
                "superseded_by": ["ADR-0002"],
                "secret": "never-copy",
            }
        )
        previous = sys.modules.get("yaml")
        sys.modules["yaml"] = fake_yaml
        try:
            metadata = json.loads(search.extract_markdown_metadata(record))
        finally:
            if previous is None:
                del sys.modules["yaml"]
            else:
                sys.modules["yaml"] = previous
        self.assertEqual("superseded", metadata["decision_status"])
        self.assertNotIn("secret", metadata)

    def test_purge_removes_database_and_sidecars_but_refuses_symlink(self):
        self.runtime.mkdir()
        database = self.runtime / "knowledge.db"
        for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
            path.write_text("x")
        removed = search.purge_index({"database": str(database)})
        self.assertEqual(3, len(removed))
        self.assertFalse(database.exists())

        target = self.runtime / "target"
        target.write_text("keep")
        database.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            search.purge_index({"database": str(database)})
        self.assertEqual("keep", target.read_text())

    def test_runtime_check_reports_missing_extension_support(self):
        fake_connection = types.SimpleNamespace(close=lambda: None)
        original = search.sqlite3.connect
        search.sqlite3.connect = lambda _: fake_connection
        try:
            with self.assertRaisesRegex(RuntimeError, "cannot load extensions"):
                search.check_runtime()
        finally:
            search.sqlite3.connect = original

    def test_private_identity_mode_accepts_only_own_profile_and_diary(self):
        identity = self.base / "identity"
        diary = identity / "diary"
        diary.mkdir(parents=True)
        runtime = self.base / "identity-search"
        cfg = {
            "mode": "agent-identity",
            "identity_root": str(identity),
            "database": str(runtime / "identity.db"),
            "directories": [
                {"path": str(identity), "name": "profile", "type": "markdown", "include": [
                    "principles.md", "identity.md", "origin.md", "voice.md", "relationships.md",
                ]},
                {"path": str(diary), "name": "diary", "type": "markdown"},
            ],
        }
        search._validate_agent_identity_config(cfg, identity, runtime)
        cfg["directories"][1]["type"] = "jsonl"
        with self.assertRaisesRegex(ValueError, "profile and diary"):
            search._validate_agent_identity_config(cfg, identity, runtime)

    def test_private_identity_mode_rejects_cross_principal_source(self):
        identity = self.base / "identity"
        (identity / "diary").mkdir(parents=True)
        other = self.base / "other-agent"
        other.mkdir()
        runtime = self.base / "identity-search"
        cfg = {
            "mode": "agent-identity",
            "identity_root": str(identity),
            "database": str(runtime / "identity.db"),
            "directories": [
                {"path": str(identity), "name": "profile", "type": "markdown", "include": [
                    "principles.md", "identity.md", "origin.md", "voice.md", "relationships.md",
                ]},
                {"path": str(other), "name": "diary", "type": "markdown"},
            ],
        }
        with self.assertRaisesRegex(ValueError, "escapes"):
            search._validate_agent_identity_config(cfg, identity, runtime)

    def test_private_identity_corpus_is_markdown_only(self):
        identity = self.base / "identity"
        diary = identity / "diary"
        diary.mkdir(parents=True)
        (diary / "entry.md").write_text("reflection")
        (diary / "raw.txt").write_text("not admitted")
        files = search.enumerate_markdown_files(
            {"path": str(diary), "name": "diary", "type": "markdown"},
            markdown_only=True,
        )
        self.assertEqual([str((diary / "entry.md").resolve())], files)

    def test_failed_refresh_rolls_back_file_and_fails_closed(self):
        document = self.docs / "record.md"
        document.write_text("# Replacement\n\nnew content\n")
        database = self.runtime / "transaction.db"
        self.runtime.mkdir()
        db = sqlite3.connect(database)
        db.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, file_path TEXT, source TEXT, "
            "title TEXT, chunk_index INTEGER, content TEXT, file_hash TEXT, indexed_at TEXT, metadata TEXT)"
        )
        db.execute("CREATE TABLE documents_vec (rowid INTEGER, embedding BLOB)")
        db.execute(
            "INSERT INTO documents VALUES (1, ?, 'docs', 'Old', 0, 'old content', 'old-hash', 'then', '{}')",
            (str(document.resolve()),),
        )
        db.execute("INSERT INTO documents_vec VALUES (1, X'00')")
        db.commit()
        db.close()

        cfg = dict(self.cfg)
        cfg["directories"] = [
            {"path": str(self.docs), "name": "docs", "type": "markdown", "include": ["record.md"]}
        ]
        original_get_db = search.get_db
        original_embed = search.embed_texts
        search.get_db = lambda _: sqlite3.connect(database)
        search.embed_texts = lambda _: (_ for _ in ()).throw(RuntimeError("embedding failed"))
        try:
            with self.assertRaisesRegex(RuntimeError, "refresh failed"):
                search.index_all(cfg, force=True)
        finally:
            search.get_db = original_get_db
            search.embed_texts = original_embed

        db = sqlite3.connect(database)
        self.assertEqual(
            [(1, "old content", "old-hash")],
            db.execute("SELECT id, content, file_hash FROM documents").fetchall(),
        )
        self.assertEqual([(1,)], db.execute("SELECT rowid FROM documents_vec").fetchall())
        db.close()

    def test_curated_search_refuses_partial_retrieval(self):
        class BrokenDatabase:
            def execute(self, *_args, **_kwargs):
                raise sqlite3.OperationalError("broken index")

            def close(self):
                pass

        original_get_db = search.get_db
        original_embed = search.embed_texts
        search.get_db = lambda _: BrokenDatabase()
        search.embed_texts = lambda _: [[0.0] * 384]
        try:
            with self.assertRaisesRegex(RuntimeError, "FTS, vector"):
                search.search({"mode": "cargo-chief-docs"}, "anything")
        finally:
            search.get_db = original_get_db
            search.embed_texts = original_embed


if __name__ == "__main__":
    unittest.main()
