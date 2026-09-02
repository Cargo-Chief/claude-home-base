# Home Base Search

A lightweight alternative to [qmd](https://github.com/tobi/qmd) — hybrid keyword + vector semantic search over your local files, designed for AI agents that are already LLMs. Built for AI cofounders running on Claude Home Base who need to recall past conversations, notes, and project context.

## The Origin Story

This started when we found [qmd](https://github.com/tobi/qmd) — Tobi Lütke's on-device semantic search engine. It's excellent, but runs three local ML models (embedding, re-ranking, query expansion) totaling ~2.15 GB, with 3-4 GB RAM at peak. On our 8 GB MacBook Air M1, that wasn't viable alongside Chrome and Claude Code.

So we asked: what does qmd actually do, and which parts do we *genuinely* need?

**The insight:** An AI cofounder running on Claude is already an LLM. Two of qmd's three models — query expansion and re-ranking — do things the AI can do natively. The only piece it can't replace is the embedding model that converts text to vectors for semantic matching.

That reframing cut the problem from "run 3 models on a constrained machine" to "run 1 small model + smart indexing." We paired that with SQLite FTS5 for keyword search, giving us hybrid search in a 60 MB database with ~100 MB RAM at peak.

### The architecture in one sentence

SQLite does the heavy lifting (FTS5 for keywords, sqlite-vec for vectors), a tiny embedding model (bge-small-en-v1.5, 384 dims) handles semantics, and the AI cofounder compensates for everything else — expanding queries, ranking results, understanding context.

## What It Does

- **Indexes** markdown files and Claude Code JSONL conversation logs
- **Searches** using both keyword matching (BM25) and vector similarity
- **Merges** results with weighted scoring (0.4 keyword + 0.6 semantic)
- **Tracks** file changes by content hash — re-indexes only what changed

## How It Works

```
[Your files] → indexer → SQLite DB (FTS5 + sqlite-vec)
                               ↑
                         search CLI ← AI cofounder, during conversations
```

Three tables in one SQLite file:
- `documents` — file path, source name, title, content chunks, file hash
- `documents_fts` — FTS5 virtual table for BM25 keyword search
- `documents_vec` — sqlite-vec virtual table for vector similarity search

Documents are chunked (~1000 chars with 200 char overlap, breaking at paragraph/sentence boundaries) so both search methods work on focused passages rather than entire files.

## Setup

```bash
cd search/
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Requires Python 3.12+ **built with SQLite extension loading**. Homebrew Python provides it; a stock
pyenv build may not. Run `doctor` before installing or indexing. Install via
`brew install python@3.12` if needed, or rebuild pyenv Python with loadable SQLite extension support.

The embedding model (~130 MB) downloads automatically on first run.

## Configuration

Copy and edit the example config:

```bash
cp config.yaml.example config.yaml
```

Add directories to index:

```yaml
directories:
  - path: ~/diary
    name: diary
    type: markdown

  - path: ~/.claude/projects/-Users-luo
    name: conversations
    type: jsonl

  - path: ~/projects
    name: projects
    type: markdown
    exclude:
      - node_modules
      - .git
      - venv
```

Supported types:
- `markdown` — recursively scans for `.md` and `.txt` files
- `jsonl` — extracts human/assistant messages from Claude Code session logs

### Cargo Chief curated-knowledge mode

Cargo Chief does **not** use the conversation example above. Start from
`config.cargo-chief.yaml.example` and set `CARGO_CHIEF_ROOT` plus a private external
`CARGO_CHIEF_SEARCH_DIR`. In `mode: cargo-chief-docs` the program fails closed unless every source
is Markdown beneath the canonical docs root and the database is outside the Cargo Chief workspace.
It rejects symlinked sources/files, JSONL, duplicate source ownership, and paths that escape the
docs root.

Every index refresh reconciles the database against the configured corpus: changed files replace
their chunks, deleted or newly excluded files are removed, and removed source definitions are
purged. Use `rebuild` for a clean regeneration and `purge` to delete the database and its SQLite
sidecars. `status --json` exposes counts and paths without indexed content.

Install the dedicated environment once, then use the wrapper. It pins the index, model, and cache
under a private per-user state directory and supplies the canonical docs-only configuration:

```bash
bash search/install_cargo_chief_search.sh
bash search/cargo_chief_search.sh doctor
bash search/cargo_chief_search.sh index
bash search/cargo_chief_search.sh search "why did we choose this architecture?" --json
```

`CARGO_CHIEF_SEARCH_PYTHON`, `CARGO_CHIEF_SEARCH_VENV`, and `CARGO_CHIEF_SEARCH_DIR` override the
defaults for testing or a nonstandard installation. The wrapper never enables transcript search.

```bash
bash search/cargo_chief_search.sh index
bash search/cargo_chief_search.sh doctor
bash search/cargo_chief_search.sh status --json
bash search/cargo_chief_search.sh rebuild
bash search/cargo_chief_search.sh purge
```

Decision-record search results include lifecycle and supersession metadata so an obsolete decision
is not presented as current merely because it matched strongly.

## Usage

```bash
# Index all configured directories (skip unchanged files)
python3 agent_search.py index

# Force re-index everything
python3 agent_search.py index --force

# Search
python3 agent_search.py search "what did we discuss about pricing"

# Filter by source
python3 agent_search.py search "auth bug" --source conversations

# JSON output (for programmatic use)
python3 agent_search.py search "revenue strategy" --json

# Check index stats
python3 agent_search.py status
```

### Shell alias (optional)

Add to your `.zshrc`:

```bash
alias lsearch="/path/to/search/venv/bin/python3 /path/to/search/agent_search.py search"
```

Then: `lsearch "your query here"`

## Nightly Re-indexing

Use launchd (macOS) to keep the index fresh. Create a wrapper script and plist — the indexer skips unchanged files automatically, so nightly runs are fast.

## Performance

Benchmarked on MacBook Air M1 (8 GB RAM):

| Metric | Value |
|---|---|
| Initial index (18k chunks, 1,293 files) | ~30 min |
| Re-index (no changes) | seconds |
| Search query | ~3s first query (model load), <1s after |
| RAM during indexing | ~1 GB peak |
| RAM during search | ~150 MB peak |
| RAM when idle | 0 MB |
| Database size (18k chunks) | 60 MB |
| Embedding model on disk | ~130 MB |

## How Search Results Work

Each result shows:
- **Match type:** `hybrid` (both keyword and semantic matched), `keyword` (BM25 only), or `semantic` (vector only)
- **Combined score:** weighted merge of normalized keyword and semantic scores
- **Source:** which configured directory the result came from

Semantic-only results are where vector search earns its keep — finding documents where the *meaning* matches even when no keywords overlap. For example, searching "how should I approach making money" finds product marketing docs that never mention "money."

## Stack

- **Python 3.12+**
- **[fastembed](https://github.com/qdrant/fastembed)** — ONNX-based embeddings, no PyTorch dependency
- **[sqlite-vec](https://github.com/asg017/sqlite-vec)** — vector search as a SQLite extension
- **SQLite FTS5** — built-in full-text search with BM25 ranking
- **Embedding model:** [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) (384 dimensions, ~130 MB)

## Design Decisions

**Why not just vector search?** BM25 keyword search is still better for exact matches — names, error codes, specific terms. Hybrid search gets the best of both.

**Why not a bigger embedding model?** On constrained hardware, bge-small-en-v1.5 hits the sweet spot: 384 dimensions, ~100 MB RAM, and the quality gap is small because the AI cofounder handles re-ranking and query expansion itself.

**Why SQLite?** One file, zero infrastructure, battle-tested. FTS5 and sqlite-vec both run as extensions in the same database. No separate vector DB needed.

**Why not use an embedding API?** Runs fully offline, no API costs, no latency, no privacy concerns. All data stays on your machine.
