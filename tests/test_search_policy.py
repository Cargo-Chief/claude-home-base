import contextlib
import fcntl
import io
import importlib.util
import os
import json
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
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

    def test_search_never_serves_chunks_of_deleted_files(self):
        live = self.docs / "live.md"
        live.write_text("live\n")
        gone = str(self.docs / "gone.md")

        class StaleDatabase:
            def execute(self, sql, *_args):
                if "documents_vec" in sql:
                    rows = [(1, 0.1, str(live), "docs", "Live", 0, "live", None),
                            (2, 0.2, gone, "docs", "Gone", 0, "gone", None)]
                else:
                    rows = [(2, gone, "docs", "Gone", 0, "gone", -3.0, None)]
                return mock.Mock(fetchall=lambda: rows)

            def close(self):
                pass

        with mock.patch.object(search, "get_db", return_value=StaleDatabase()), \
             mock.patch.object(search, "embed_texts", return_value=[[0.0] * 384]), \
             mock.patch.object(search, "serialize_vec", return_value=b""):
            results = search.search({"mode": "cargo-chief-docs"}, "anything")

        self.assertEqual([str(live)], [r["file_path"] for r in results])

    def test_pending_counts_new_and_changed_files_without_a_database(self):
        (self.docs / "a.md").write_text("a\n")
        (self.docs / "b.md").write_text("b\n")

        self.assertEqual(
            {"files": 2, "indexed_files": 0, "to_embed": 2},
            search.pending_files(self.cfg),
        )

    def test_pending_skips_files_whose_hash_is_indexed(self):
        a = self.docs / "a.md"
        b = self.docs / "b.md"
        a.write_text("a\n")
        b.write_text("b\n")
        self.runtime.mkdir()
        db = sqlite3.connect(self.cfg["database"])
        db.execute("CREATE TABLE documents (file_path TEXT, file_hash TEXT)")
        db.executemany("INSERT INTO documents VALUES (?, ?)", [
            (str(a.resolve()), search.file_hash(str(a.resolve()))),
            (str(b.resolve()), "outdated"),
            (str(self.docs / "deleted.md"), "x"),
        ])
        db.commit()
        db.close()

        self.assertEqual(
            {"files": 2, "indexed_files": 3, "to_embed": 1},
            search.pending_files(self.cfg),
        )

    def test_pending_ignores_files_with_no_indexable_body(self):
        (self.docs / "stub.md").write_text("---\ntitle: Stub\n---\n")

        self.assertEqual(0, search.pending_files(self.cfg)["to_embed"])

    def test_pending_prints_the_single_line_the_wrapper_parses(self):
        (self.docs / "a.md").write_text("a\n")
        stdout = io.StringIO()
        with mock.patch.object(search, "load_config", return_value=self.cfg), \
             contextlib.redirect_stdout(stdout):
            search.main(["pending"])

        line = stdout.getvalue()
        self.assertEqual(1, line.count("\n"))
        self.assertRegex(line, r'"to_embed": 1')
        self.assertRegex(line, r'"indexed_files": 0')

    def test_main_releases_embedding_model_after_success(self):
        model = object()
        search._model = model
        with mock.patch.object(search, "load_config", return_value={}), \
             mock.patch.object(search, "show_status"), \
             mock.patch.object(search.gc, "collect") as collect:
            search.main(["status"])
        self.assertIsNone(search._model)
        collect.assert_called_once_with()

    def test_model_loading_progress_stays_off_stdout(self):
        fastembed = types.ModuleType("fastembed")
        fastembed.TextEmbedding = lambda name: object()
        stdout, stderr = io.StringIO(), io.StringIO()
        search._model = None
        try:
            with mock.patch.dict(sys.modules, {"fastembed": fastembed}), \
                 contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                search.get_model()
        finally:
            search._model = None
        self.assertEqual("", stdout.getvalue())
        self.assertIn("Loading embedding model", stderr.getvalue())

    def test_main_releases_embedding_model_after_command_failure(self):
        search._model = object()
        with mock.patch.object(search, "load_config", return_value=self.cfg), \
             mock.patch.object(search, "index_all", side_effect=RuntimeError("index failed")), \
             mock.patch.object(search.gc, "collect") as collect:
            with self.assertRaisesRegex(RuntimeError, "index failed"):
                search.main(["index"])
        self.assertIsNone(search._model)
        collect.assert_called_once_with()


    def _hold_lock(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        fd = os.open(f"{self.cfg['database']}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def test_index_waits_out_a_concurrent_run_then_gives_up(self):
        fd = self._hold_lock()
        try:
            with mock.patch.object(search, "load_config", return_value=self.cfg), \
                 mock.patch.object(search, "index_all") as index_all, \
                 mock.patch.dict(os.environ, {search.INDEX_LOCK_TIMEOUT_ENV: "0"}):
                for command in (["index"], ["rebuild"], ["purge"]):
                    with self.subTest(command=command):
                        with self.assertRaisesRegex(RuntimeError, "another index run"):
                            search.main(command)
                index_all.assert_not_called()
        finally:
            os.close(fd)

    def test_index_runs_once_the_other_run_releases(self):
        fd = self._hold_lock()
        release = threading.Timer(0.5, os.close, args=(fd,))
        release.start()
        try:
            with mock.patch.object(search, "load_config", return_value=self.cfg), \
                 mock.patch.object(search, "index_all") as index_all, \
                 mock.patch.dict(os.environ, {search.INDEX_LOCK_TIMEOUT_ENV: "10"}):
                search.main(["index"])
        finally:
            release.join()
        index_all.assert_called_once_with(self.cfg, force=False)

    def test_lock_timeout_must_be_a_finite_non_negative_number(self):
        for raw in ("", "30m", "nan", "inf", "-1"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {search.INDEX_LOCK_TIMEOUT_ENV: raw}):
                with self.assertRaisesRegex(ValueError, search.INDEX_LOCK_TIMEOUT_ENV):
                    with search.index_lock(self.cfg):
                        pass
        self.assertFalse(self.runtime.exists())

    def test_purge_of_a_missing_index_creates_nothing(self):
        with mock.patch.object(search, "load_config", return_value=self.cfg):
            search.main(["purge"])
        self.assertFalse(self.runtime.exists())

    def test_lock_tightens_the_directory_and_works_on_a_read_only_lock_file(self):
        self.runtime.mkdir(mode=0o755)
        lock_path = Path(f"{self.cfg['database']}.lock")
        lock_path.write_text("")
        lock_path.chmod(0o400)
        with search.index_lock(self.cfg):
            self.assertEqual(0o700, self.runtime.stat().st_mode & 0o777)

    def test_read_commands_do_not_wait_for_the_index_lock(self):
        fd = self._hold_lock()
        try:
            with mock.patch.object(search, "load_config", return_value=self.cfg), \
                 mock.patch.object(search, "show_status") as show_status, \
                 mock.patch.dict(os.environ, {search.INDEX_LOCK_TIMEOUT_ENV: "0"}):
                search.main(["status"])
            show_status.assert_called_once()
        finally:
            os.close(fd)

    def test_index_lock_refuses_a_symlinked_lock_file(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        target = self.base / "elsewhere"
        target.write_text("")
        os.symlink(target, f"{self.cfg['database']}.lock")
        with self.assertRaises(OSError):
            with search.index_lock(self.cfg):
                pass

if __name__ == "__main__":
    unittest.main()
