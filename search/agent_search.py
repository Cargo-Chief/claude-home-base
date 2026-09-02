#!/usr/bin/env python3
"""
Agent Search — hybrid FTS5 + vector search over local files.

Usage:
    python agent_search.py index          # Index all configured directories
    python agent_search.py search "query" # Search across all indexed content
    python agent_search.py search "query" --source diary  # Filter by source
    python agent_search.py status         # Show index stats
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config(path=CONFIG_PATH):
    import yaml

    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["database"] = os.path.expandvars(os.path.expanduser(cfg["database"]))
    for d in cfg["directories"]:
        d["path"] = os.path.expandvars(os.path.expanduser(d["path"]))
    if cfg.get("mode") == "cargo-chief-docs":
        _validate_cargo_chief_config(cfg)
    elif cfg.get("mode") == "agent-identity":
        _validate_agent_identity_config(cfg)
    return cfg


def _is_within(path, root):
    try:
        Path(path).relative_to(Path(root))
        return True
    except ValueError:
        return False


def _validate_cargo_chief_config(cfg, workspace_root=None, search_root=None):
    """Fail closed when the curated Cargo Chief corpus crosses its docs boundary."""
    docs_value = cfg.get("docs_root", "")
    if not docs_value:
        raise ValueError("cargo-chief-docs mode requires docs_root")

    docs_path = Path(os.path.expandvars(os.path.expanduser(docs_value)))
    if not docs_path.is_absolute() or not docs_path.is_dir() or docs_path.is_symlink():
        raise ValueError("docs_root must be an existing absolute non-symlink directory")
    docs_root = docs_path.resolve()
    cfg["docs_root"] = str(docs_root)

    if workspace_root is None:
        workspace_value = os.environ.get("CARGO_CHIEF_ROOT")
        if not workspace_value:
            raise ValueError("cargo-chief-docs mode requires CARGO_CHIEF_ROOT")
        workspace_root = Path(workspace_value)
    workspace_root = Path(workspace_root).resolve()
    if docs_root != workspace_root / "docs":
        raise ValueError("docs_root must be the canonical CARGO_CHIEF_ROOT/docs directory")

    database = Path(cfg["database"])
    if not database.is_absolute():
        raise ValueError("database must be absolute in cargo-chief-docs mode")
    database = database.resolve(strict=False)
    if _is_within(database, workspace_root):
        raise ValueError("database must live outside the Cargo Chief workspace")
    if search_root is None:
        search_value = os.environ.get("CARGO_CHIEF_SEARCH_DIR")
        if not search_value:
            raise ValueError("cargo-chief-docs mode requires CARGO_CHIEF_SEARCH_DIR")
        search_root = Path(search_value)
    search_root = Path(search_root).resolve()
    if database.parent != search_root:
        raise ValueError("database must live directly in CARGO_CHIEF_SEARCH_DIR")
    cfg["database"] = str(database)

    names = set()
    for source in cfg.get("directories", []):
        name = source.get("name", "").strip()
        if not name or name in names:
            raise ValueError("each curated source needs a unique non-empty name")
        names.add(name)
        if source.get("type") != "markdown":
            raise ValueError("cargo-chief-docs mode permits markdown sources only")
        source_path = Path(source["path"])
        if not source_path.is_absolute() or not source_path.exists() or source_path.is_symlink():
            raise ValueError(f"source {name} must be an existing absolute non-symlink path")
        resolved = source_path.resolve()
        if not _is_within(resolved, docs_root):
            raise ValueError(f"source {name} is outside docs_root")
        source["path"] = str(resolved)
        includes = source.get("include", [])
        if not isinstance(includes, list) or any(not isinstance(v, str) or not v for v in includes):
            raise ValueError(f"source {name} include must be a list of non-empty patterns")


def _validate_agent_identity_config(cfg, identity_root=None, search_root=None):
    """Restrict the private index to one principal's identity and diary Markdown."""
    configured = cfg.get("identity_root", "")
    if not configured:
        raise ValueError("agent-identity mode requires identity_root")
    configured_root = Path(os.path.expandvars(os.path.expanduser(configured)))
    if identity_root is None:
        env_root = os.environ.get("CARGO_CHIEF_IDENTITY_DIR")
        if not env_root:
            raise ValueError("agent-identity mode requires CARGO_CHIEF_IDENTITY_DIR")
        identity_root = Path(env_root)
    expected_path = Path(identity_root).expanduser().absolute()
    if expected_path.is_symlink() or not expected_path.is_dir():
        raise ValueError("identity_root must be a non-symlink directory")
    expected = expected_path.resolve()
    if configured_root.is_symlink() or configured_root.resolve() != expected:
        raise ValueError("identity_root must match the configured per-principal identity directory")
    cfg["identity_root"] = str(expected)

    database = Path(cfg["database"]).resolve(strict=False)
    if search_root is None:
        value = os.environ.get("CARGO_CHIEF_IDENTITY_SEARCH_DIR")
        if not value:
            raise ValueError("agent-identity mode requires CARGO_CHIEF_IDENTITY_SEARCH_DIR")
        search_root = Path(value)
    search_root = Path(search_root).expanduser().resolve()
    if database.parent != search_root or database.name != "identity.db":
        raise ValueError("identity database must be identity.db in CARGO_CHIEF_IDENTITY_SEARCH_DIR")
    cfg["database"] = str(database)

    allowed = {
        "profile": (expected, ("identity.md", "origin.md", "voice.md", "relationships.md")),
        "diary": (expected / "diary", ()),
    }
    seen = set()
    for source in cfg.get("directories", []):
        name = source.get("name", "")
        if name not in allowed or name in seen or source.get("type") != "markdown":
            raise ValueError("agent-identity sources must be unique Markdown profile and diary sources")
        seen.add(name)
        source_value = Path(source["path"])
        if source_value.is_symlink():
            raise ValueError(f"source {name} must not be a symlink")
        source_path = source_value.resolve()
        wanted_path, wanted_include = allowed[name]
        if source_path != wanted_path:
            raise ValueError(f"source {name} escapes the per-principal identity directory")
        if wanted_include and tuple(source.get("include", [])) != wanted_include:
            raise ValueError("profile source must include only the four identity files")
        source["path"] = str(source_path)
    if seen != set(allowed):
        raise ValueError("agent-identity mode requires profile and diary sources")


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def get_db(cfg):
    import sqlite_vec

    check_runtime()
    db_path = cfg["database"]
    os.makedirs(os.path.dirname(db_path), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(db_path), 0o700)
    if not os.path.exists(db_path):
        fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    db = sqlite3.connect(db_path)
    os.chmod(db_path, 0o600)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.execute("PRAGMA journal_mode=WAL")
    _create_tables(db)
    return db


def check_runtime():
    """Verify the Python SQLite build can load sqlite-vec before touching the database."""
    if sys.version_info < (3, 12):
        raise RuntimeError("knowledge search requires Python 3.12+")
    connection = sqlite3.connect(":memory:")
    try:
        if not hasattr(connection, "enable_load_extension"):
            raise RuntimeError(
                "this Python SQLite build cannot load extensions; use Homebrew Python 3.12+ "
                "or rebuild pyenv Python with loadable SQLite extension support"
            )
    finally:
        connection.close()
    return {
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "loadable_extensions": True,
    }


def _create_tables(db):
    # Main documents table
    db.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            source TEXT NOT NULL,
            title TEXT,
            chunk_index INTEGER DEFAULT 0,
            content TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            metadata TEXT
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_documents_path
        ON documents(file_path, chunk_index)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_documents_source
        ON documents(source)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_documents_hash
        ON documents(file_hash)
    """)

    # FTS5 virtual table
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
        USING fts5(title, content, content=documents, content_rowid=id)
    """)

    # Triggers to keep FTS in sync
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts(rowid, title, content)
            VALUES (new.id, new.title, new.content);
        END
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, title, content)
            VALUES ('delete', old.id, old.title, old.content);
        END
    """)
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, title, content)
            VALUES ('delete', old.id, old.title, old.content);
            INSERT INTO documents_fts(rowid, title, content)
            VALUES (new.id, new.title, new.content);
        END
    """)

    # Vector table — 384 dimensions for bge-small-en-v1.5
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_vec
        USING vec0(embedding float[384])
    """)

    db.commit()


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------

def chunk_text(text, chunk_size=1000, overlap=200):
    """Split text into overlapping chunks by character count."""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size

        # Try to break at a paragraph or sentence boundary
        if end < len(text):
            # Look for paragraph break
            para_break = text.rfind("\n\n", start + chunk_size // 2, end)
            if para_break > start:
                end = para_break + 2
            else:
                # Look for sentence break
                sent_break = text.rfind(". ", start + chunk_size // 2, end)
                if sent_break > start:
                    end = sent_break + 2

        chunks.append(text[start:end].strip())
        start = end - overlap

    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------

def file_hash(path):
    """Quick hash of file content for change detection."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8192), b""):
            h.update(block)
    return h.hexdigest()


def extract_markdown(path):
    """Extract title and content from a markdown file."""
    with open(path, "r", errors="replace") as f:
        text = f.read()

    # Extract title from first heading or filename
    title = Path(path).stem
    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()

    # Strip frontmatter
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            text = text[end + 3:].strip()

    return title, text


def extract_markdown_metadata(path):
    """Expose decision lifecycle fields without retaining arbitrary frontmatter."""
    if "decisions" not in Path(path).parts:
        return None
    import yaml

    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end < 0:
        return None
    value = yaml.safe_load(text[3:end]) or {}
    allowed = ("decision_type", "decision_status", "date", "owners", "supersedes", "superseded_by")
    metadata = {key: value[key] for key in allowed if key in value}
    return json.dumps(metadata, default=str) if metadata else None


def extract_jsonl_conversations(path):
    """Extract conversations from a Claude Code JSONL session file."""
    docs = []
    messages = []
    session_id = Path(path).stem

    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = entry.get("type", "")

                # Extract human messages
                if msg_type == "queue-operation":
                    content = entry.get("content", "")
                    if content:
                        messages.append(f"Human: {content}")

                # Extract assistant messages
                elif msg_type == "assistant":
                    message = entry.get("message", {})
                    if isinstance(message, dict):
                        content_parts = message.get("content", [])
                        if isinstance(content_parts, list):
                            text_parts = []
                            for part in content_parts:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    text_parts.append(part.get("text", ""))
                            if text_parts:
                                messages.append(f"Assistant: {' '.join(text_parts)}")

    except Exception as e:
        print(f"  Warning: error reading {path}: {e}")
        return []

    if not messages:
        return []

    # Combine all messages into one document per session
    full_text = "\n\n".join(messages)
    timestamp = None
    try:
        with open(path, "r") as f:
            first_line = f.readline()
            first_entry = json.loads(first_line)
            timestamp = first_entry.get("timestamp", "")
    except Exception:
        pass

    title = f"Conversation {session_id[:8]}"
    if timestamp:
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            title = f"Conversation {dt.strftime('%Y-%m-%d %H:%M')}"
        except Exception:
            pass

    return [(title, full_text, json.dumps({"session_id": session_id, "timestamp": timestamp}))]


def should_exclude(path, excludes):
    """Check if a file path matches any exclusion patterns."""
    parts = Path(path).parts
    for exc in excludes:
        if exc in parts:
            return True
    return False


def enumerate_markdown_files(source, markdown_only=False):
    """Return regular, non-symlink Markdown/text files admitted by one source."""
    root = Path(source["path"])
    includes = source.get("include", [])
    excludes = source.get("exclude", [])
    candidates = [root] if root.is_file() else root.rglob("*")
    files = []
    for candidate in candidates:
        allowed_suffixes = (".md",) if markdown_only else (".md", ".txt")
        if candidate.suffix not in allowed_suffixes:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        relative = candidate.name if root.is_file() else candidate.relative_to(root).as_posix()
        if includes and not any(fnmatch.fnmatch(relative, pattern) for pattern in includes):
            continue
        if should_exclude(candidate, excludes):
            continue
        files.append(str(candidate.resolve()))
    return sorted(files)


def validate_source_file_ownership(source_files):
    owners = {}
    for source, files in source_files.items():
        for file_path in files:
            previous = owners.setdefault(file_path, source)
            if previous != source:
                raise ValueError(
                    f"file is admitted by multiple sources: {file_path} ({previous}, {source})"
                )


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

_model = None


def get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        print("Loading embedding model...")
        _model = TextEmbedding("BAAI/bge-small-en-v1.5")
    return _model


def embed_texts(texts, batch_size=64):
    """Generate embeddings for a list of texts."""
    model = get_model()
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        embeddings = list(model.embed(batch))
        all_embeddings.extend(embeddings)
    return all_embeddings


def serialize_vec(vec):
    """Pack a numpy array into bytes for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

def index_all(cfg, force=False):
    """Index all configured directories."""
    db = get_db(cfg)
    chunk_size = cfg.get("chunk_size", 1000)
    chunk_overlap = cfg.get("chunk_overlap", 200)

    total_new = 0
    total_skipped = 0
    total_errors = 0
    source_files = {}

    if cfg.get("mode") in ("cargo-chief-docs", "agent-identity"):
        for source in cfg["directories"]:
            source_files[source["name"]] = set(enumerate_markdown_files(source, markdown_only=True))
        validate_source_file_ownership(source_files)
        removed = reconcile_sources(db, source_files)
        if removed:
            print(f"Removed {removed} stale chunk(s) for missing or excluded sources")

    for dir_cfg in cfg["directories"]:
        dir_path = dir_cfg["path"]
        source = dir_cfg["name"]
        file_type = dir_cfg["type"]
        excludes = dir_cfg.get("exclude", [])

        if not os.path.exists(dir_path):
            print(f"Skipping {source}: {dir_path} does not exist")
            continue

        print(f"\nIndexing [{source}] from {dir_path}")

        if file_type == "markdown":
            files = (sorted(source_files[source]) if source in source_files
                     else enumerate_markdown_files(dir_cfg))

            for fpath in files:
                try:
                    db.execute("SAVEPOINT index_file")
                    fhash = file_hash(fpath)
                    if not force:
                        existing = db.execute(
                            "SELECT file_hash FROM documents WHERE file_path = ? LIMIT 1",
                            (fpath,)
                        ).fetchone()
                        if existing and existing[0] == fhash:
                            db.execute("RELEASE index_file")
                            total_skipped += 1
                            continue

                    # Remove old entries for this file
                    _delete_file_entries(db, fpath)

                    title, text = extract_markdown(fpath)
                    metadata = extract_markdown_metadata(fpath)
                    if not text.strip():
                        db.execute("RELEASE index_file")
                        continue

                    chunks = chunk_text(text, chunk_size, chunk_overlap)
                    texts_to_embed = []
                    rows = []

                    for i, chunk in enumerate(chunks):
                        rows.append((fpath, source, title, i, chunk, fhash,
                                     datetime.now().isoformat(), metadata))
                        texts_to_embed.append(chunk)

                    # Insert document rows
                    doc_ids = []
                    for row in rows:
                        cursor = db.execute(
                            """INSERT INTO documents
                               (file_path, source, title, chunk_index, content, file_hash, indexed_at, metadata)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            row
                        )
                        doc_ids.append(cursor.lastrowid)

                    # Generate and insert embeddings
                    embeddings = embed_texts(texts_to_embed)
                    for doc_id, emb in zip(doc_ids, embeddings):
                        db.execute(
                            "INSERT INTO documents_vec(rowid, embedding) VALUES (?, ?)",
                            (doc_id, serialize_vec(emb))
                        )

                    db.execute("RELEASE index_file")
                    total_new += len(chunks)
                    print(f"  + {Path(fpath).name} ({len(chunks)} chunks)")

                except Exception as e:
                    try:
                        db.execute("ROLLBACK TO index_file")
                        db.execute("RELEASE index_file")
                    except sqlite3.Error:
                        pass
                    total_errors += 1
                    print(f"  ! Error indexing {fpath}: {e}")

        elif file_type == "jsonl":
            files = [os.path.join(dir_path, f) for f in os.listdir(dir_path)
                     if f.endswith(".jsonl")]

            for fpath in files:
                try:
                    db.execute("SAVEPOINT index_file")
                    fhash = file_hash(fpath)
                    if not force:
                        existing = db.execute(
                            "SELECT file_hash FROM documents WHERE file_path = ? LIMIT 1",
                            (fpath,)
                        ).fetchone()
                        if existing and existing[0] == fhash:
                            db.execute("RELEASE index_file")
                            total_skipped += 1
                            continue

                    _delete_file_entries(db, fpath)

                    conversations = extract_jsonl_conversations(fpath)
                    file_chunks = 0
                    for title, text, metadata in conversations:
                        if not text.strip():
                            continue

                        chunks = chunk_text(text, chunk_size, chunk_overlap)
                        texts_to_embed = []
                        rows = []

                        for i, chunk in enumerate(chunks):
                            rows.append((fpath, source, title, i, chunk, fhash,
                                         datetime.now().isoformat(), metadata))
                            texts_to_embed.append(chunk)

                        doc_ids = []
                        for row in rows:
                            cursor = db.execute(
                                """INSERT INTO documents
                                   (file_path, source, title, chunk_index, content, file_hash, indexed_at, metadata)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                row
                            )
                            doc_ids.append(cursor.lastrowid)

                        embeddings = embed_texts(texts_to_embed)
                        for doc_id, emb in zip(doc_ids, embeddings):
                            db.execute(
                                "INSERT INTO documents_vec(rowid, embedding) VALUES (?, ?)",
                                (doc_id, serialize_vec(emb))
                            )

                        file_chunks += len(chunks)

                    db.execute("RELEASE index_file")
                    total_new += file_chunks
                    print(f"  + {Path(fpath).name}")

                except Exception as e:
                    try:
                        db.execute("ROLLBACK TO index_file")
                        db.execute("RELEASE index_file")
                    except sqlite3.Error:
                        pass
                    total_errors += 1
                    print(f"  ! Error indexing {fpath}: {e}")

    print(f"\nDone! {total_new} chunks indexed, {total_skipped} unchanged files skipped, {total_errors} errors")
    db.close()
    if total_errors and cfg.get("mode") == "cargo-chief-docs":
        raise RuntimeError(f"curated index refresh failed with {total_errors} error(s)")
    return {"indexed": total_new, "skipped": total_skipped, "errors": total_errors}


def _delete_file_entries(db, file_path):
    """Remove all entries for a file (before re-indexing)."""
    ids = db.execute("SELECT id FROM documents WHERE file_path = ?", (file_path,)).fetchall()
    for (doc_id,) in ids:
        db.execute("DELETE FROM documents_vec WHERE rowid = ?", (doc_id,))
    db.execute("DELETE FROM documents WHERE file_path = ?", (file_path,))


def reconcile_sources(db, source_files):
    """Remove chunks whose source or file is no longer admitted by configuration."""
    configured = set(source_files)
    rows = db.execute("SELECT DISTINCT source, file_path FROM documents").fetchall()
    stale_paths = {
        file_path
        for source, file_path in rows
        if source not in configured or file_path not in source_files[source]
    }
    removed = 0
    for file_path in stale_paths:
        count = db.execute(
            "SELECT COUNT(*) FROM documents WHERE file_path = ?", (file_path,)
        ).fetchone()[0]
        _delete_file_entries(db, file_path)
        removed += count
    db.commit()
    return removed


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search(cfg, query, source=None, limit=10):
    """Hybrid search: FTS5 (BM25) + vector similarity, merged."""
    db = get_db(cfg)

    source_filter = ""
    source_params = ()
    if source:
        source_filter = "AND d.source = ?"
        source_params = (source,)

    # --- FTS5 search ---
    fts_results = {}
    search_errors = []
    try:
        fts_query = _build_fts_query(query)
        rows = db.execute(f"""
            SELECT d.id, d.file_path, d.source, d.title, d.chunk_index,
                   snippet(documents_fts, 1, '>>>', '<<<', '...', 40) as snippet,
                   bm25(documents_fts) as score, d.metadata
            FROM documents_fts f
            JOIN documents d ON d.id = f.rowid
            WHERE documents_fts MATCH ?
            {source_filter}
            ORDER BY score
            LIMIT ?
        """, (fts_query, *source_params, limit * 2)).fetchall()

        for row in rows:
            doc_id = row[0]
            fts_results[doc_id] = {
                "id": doc_id,
                "file_path": row[1],
                "source": row[2],
                "title": row[3],
                "chunk_index": row[4],
                "snippet": row[5],
                "fts_score": -row[6],  # BM25 returns negative scores
                "metadata": row[7],
            }
    except Exception as e:
        print(f"FTS search error: {e}")
        search_errors.append(("FTS", e))

    # --- Vector search ---
    vec_results = {}
    try:
        query_embedding = embed_texts([query])[0]
        query_bytes = serialize_vec(query_embedding)

        rows = db.execute(f"""
            SELECT v.rowid, v.distance,
                   d.file_path, d.source, d.title, d.chunk_index,
                   substr(d.content, 1, 300) as snippet, d.metadata
            FROM documents_vec v
            JOIN documents d ON d.id = v.rowid
            WHERE embedding MATCH ?
            AND k = ?
            {source_filter}
            ORDER BY distance
        """, (query_bytes, limit * 2, *source_params)).fetchall()

        for row in rows:
            doc_id = row[0]
            vec_results[doc_id] = {
                "id": doc_id,
                "distance": row[1],
                "file_path": row[2],
                "source": row[3],
                "title": row[4],
                "chunk_index": row[5],
                "snippet": row[6],
                "vec_score": 1 - row[1],  # Convert distance to similarity
                "metadata": row[7],
            }
    except Exception as e:
        print(f"Vector search error: {e}")
        search_errors.append(("vector", e))

    if search_errors and cfg.get("mode") == "cargo-chief-docs":
        db.close()
        kinds = ", ".join(kind for kind, _ in search_errors)
        raise RuntimeError(f"curated search failed in {kinds} retrieval")

    # --- Merge results ---
    all_ids = set(fts_results.keys()) | set(vec_results.keys())
    merged = []

    # Normalize scores
    max_fts = max((r["fts_score"] for r in fts_results.values()), default=1) or 1
    max_vec = max((r["vec_score"] for r in vec_results.values()), default=1) or 1

    for doc_id in all_ids:
        fts = fts_results.get(doc_id, {})
        vec = vec_results.get(doc_id, {})

        fts_norm = fts.get("fts_score", 0) / max_fts
        vec_norm = vec.get("vec_score", 0) / max_vec

        # Weighted combination: slight preference for vector similarity
        combined = 0.4 * fts_norm + 0.6 * vec_norm

        result = {
            "id": doc_id,
            "file_path": fts.get("file_path") or vec.get("file_path"),
            "source": fts.get("source") or vec.get("source"),
            "title": fts.get("title") or vec.get("title"),
            "chunk_index": fts.get("chunk_index") or vec.get("chunk_index", 0),
            "snippet": fts.get("snippet") or vec.get("snippet", ""),
            "combined_score": combined,
            "fts_score": fts.get("fts_score", 0),
            "vec_score": vec.get("vec_score", 0),
            "match_type": _match_type(fts, vec),
        }
        metadata = fts.get("metadata") or vec.get("metadata")
        if metadata:
            try:
                result["decision"] = json.loads(metadata)
            except json.JSONDecodeError:
                pass
        merged.append(result)

    merged.sort(key=lambda x: x["combined_score"], reverse=True)
    db.close()
    return merged[:limit]


def _build_fts_query(query):
    """Build an FTS5 query from natural language — OR between terms for recall."""
    words = re.findall(r'\w+', query.lower())
    if not words:
        return query
    return " OR ".join(words)


def _match_type(fts, vec):
    if fts and vec:
        return "hybrid"
    elif fts:
        return "keyword"
    else:
        return "semantic"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def index_status(cfg):
    db = get_db(cfg)
    total = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    sources = db.execute(
        "SELECT source, COUNT(*) FROM documents GROUP BY source ORDER BY source"
    ).fetchall()
    files = db.execute("SELECT COUNT(DISTINCT file_path) FROM documents").fetchone()[0]
    vec_count = db.execute("SELECT COUNT(*) FROM documents_vec").fetchone()[0]

    result = {
        "database": cfg["database"],
        "mode": cfg.get("mode", "standard"),
        "total_chunks": total,
        "total_files": files,
        "vector_count": vec_count,
        "sources": {},
    }
    for source, count in sources:
        file_count = db.execute(
            "SELECT COUNT(DISTINCT file_path) FROM documents WHERE source = ?",
            (source,)
        ).fetchone()[0]
        result["sources"][source] = {"chunks": count, "files": file_count}
    db.close()
    return result


def show_status(cfg, as_json=False):
    result = index_status(cfg)
    if as_json:
        print(json.dumps(result, indent=2))
        return
    print("Agent Search Index Status")
    print(f"{'='*40}")
    print(f"Mode:          {result['mode']}")
    print(f"Total chunks:  {result['total_chunks']}")
    print(f"Total files:   {result['total_files']}")
    print(f"Vector count:  {result['vector_count']}")
    print(f"\nBy source:")
    for source, values in result["sources"].items():
        print(f"  {source:20s} {values['chunks']:5d} chunks from {values['files']} files")


def purge_index(cfg):
    """Remove the configured database and SQLite sidecars without following symlinks."""
    database = Path(cfg["database"])
    removed = []
    for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if path.is_symlink():
            raise ValueError(f"refusing to purge symlink: {path}")
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def format_results(results):
    """Pretty-print search results."""
    if not results:
        print("No results found.")
        return

    for i, r in enumerate(results, 1):
        score_parts = []
        if r["fts_score"]:
            score_parts.append(f"kw:{r['fts_score']:.2f}")
        if r["vec_score"]:
            score_parts.append(f"vec:{r['vec_score']:.3f}")
        scores = ", ".join(score_parts)

        print(f"\n{'─'*60}")
        print(f"[{i}] {r['title']}  ({r['match_type']})")
        print(f"    Source: {r['source']} | Score: {r['combined_score']:.3f} ({scores})")
        print(f"    File: {r['file_path']}")
        decision = r.get("decision", {})
        if decision:
            successor = decision.get("superseded_by") or []
            suffix = f" | Superseded by: {', '.join(successor)}" if successor else ""
            print(f"    Decision: {decision.get('decision_status', 'unknown')}{suffix}")
        snippet = r['snippet'].replace('\n', ' ')[:200]
        print(f"    {snippet}")


def main():
    parser = argparse.ArgumentParser(description="Agent Search")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Configuration file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Index command
    idx_parser = subparsers.add_parser("index", help="Index configured directories")
    idx_parser.add_argument("--force", action="store_true", help="Re-index all files")

    subparsers.add_parser("rebuild", help="Purge and rebuild the configured index")

    # Search command
    search_parser = subparsers.add_parser("search", help="Search indexed content")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--source", "-s", help="Filter by source name")
    search_parser.add_argument("--limit", "-n", type=int, default=10, help="Max results")
    search_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # Status command
    status_parser = subparsers.add_parser("status", help="Show index stats")
    status_parser.add_argument("--json", action="store_true", help="Output as JSON")
    subparsers.add_parser("purge", help="Delete the configured index and SQLite sidecars")
    subparsers.add_parser("doctor", help="Validate config and SQLite extension support")

    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.command == "index":
        index_all(cfg, force=args.force)
    elif args.command == "rebuild":
        purge_index(cfg)
        index_all(cfg, force=True)
    elif args.command == "search":
        results = search(cfg, args.query, source=args.source, limit=args.limit)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            format_results(results)
    elif args.command == "status":
        show_status(cfg, as_json=args.json)
    elif args.command == "purge":
        removed = purge_index(cfg)
        print(json.dumps({"removed": removed}))
    elif args.command == "doctor":
        result = check_runtime()
        result.update({"mode": cfg.get("mode", "standard"), "config": str(args.config)})
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
