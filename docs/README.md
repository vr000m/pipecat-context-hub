# Pipecat Context Hub

Local-first MCP server providing fresh Pipecat docs and examples context for Claude Code, Cursor, VS Code, and Zed.

## What It Does

When your AI coding assistant needs Pipecat context, it calls MCP tools exposed by this server. The server queries a local index (ChromaDB + SQLite FTS5) and returns relevant documentation, code examples, and snippets — all with source citations.

```
IDE/Agent  ←stdio→  pipecat-context-hub serve  ←→  Local index (~/.pipecat-context-hub/)
```

### MCP Tools

| Tool | Purpose |
|------|---------|
| `search_docs` | Search Pipecat documentation for conceptual questions and guides |
| `get_doc` | Fetch a specific doc page by chunk ID |
| `search_examples` | Find working code examples by task, modality, or component |
| `get_example` | Retrieve full example with source files and metadata |
| `get_code_snippet` | Get targeted code spans by intent, symbol, or path. Returns enriched output with dependencies (`dependency_notes`), called methods (`companion_snippets`), and interface contracts (`interface_expectations`) |
| `search_api` | Search framework internals — class definitions, method signatures, inheritance. Filter by `yields` (frame types) or `calls` (method names) |
| `get_hub_status` | Get index health: last refresh time, record counts, commit SHAs |

All responses include an `EvidenceReport` with `known`/`unknown` items, confidence scores, and suggested follow-up queries.

## Quick Start

```bash
# Install (editable, with dev deps)
uv pip install -e ".[dev]"

# Populate the local index (crawls docs + clones repos + computes embeddings)
pipecat-context-hub refresh

# Force full re-ingest, ignoring cached state
pipecat-context-hub refresh --force

# Start the MCP server
pipecat-context-hub serve
```

## Client Setup

Add the server to your IDE's MCP config. Pre-built templates are in `config/clients/`.

| Client | Guide | Config template |
|--------|-------|-----------------|
| Claude Code | [docs/setup/claude-code.md](setup/claude-code.md) | `config/clients/claude-code.json` |
| Cursor | [docs/setup/cursor.md](setup/cursor.md) | `config/clients/cursor.json` |
| VS Code | [docs/setup/vscode.md](setup/vscode.md) | `config/clients/vscode.json` |
| Zed | [docs/setup/zed.md](setup/zed.md) | `config/clients/zed.json` |

See [docs/setup/README.md](setup/README.md) for the full setup overview.

## Architecture

```
Ingestion:
  DocsCrawler (llms-full.txt)    ──┐
  GitHubRepoIngester (N repos)   ──┤→ EmbeddingIndexWriter → IndexStore
  SourceIngester (per-repo AST)  ──┤   (sentence-transformers)   (ChromaDB + FTS5)
  TaxonomyBuilder (auto-infer)   ──┘
    ↑                                         ↑
    Per-file taxonomy enrichment:             Metadata stored per chunk:
    foundational_class, capability_tags,      language, execution_mode,
    key_files, execution_mode                 line_start, line_end

Retrieval:
  MCP Tool Call → HybridRetriever → decompose_query (split on + / &)
                    ↓                     ↓
              single-concept         multi-concept (parallel per-concept)
                    ↓                     ↓
              vector + keyword      round-robin interleave + dedup
                    ↓                     ↓
                  rerank (RRF)      evidence assembly
                    ↓
                  Cited response with EvidenceReport
```

### Data Sources (v0)

- `https://docs.pipecat.ai/llms-full.txt` — primary documentation (pre-rendered markdown, 200+ pages)
- `pipecat-ai/pipecat` — framework repo (including `examples/foundational`)
  - Supports flat file layout (e.g. `01-say-one-thing.py`) and subdirectory layout
- `pipecat-ai/pipecat-examples` — project-level examples
  - Discovered via root-level directory scanning (no `examples/` dir required)
- Additional repos via `PIPECAT_HUB_EXTRA_REPOS` env var (comma-separated slugs)
  - Supports single-project repos (`src/`-layout, root-level entry scripts)
  - Repos with `src/` layouts are AST-indexed for `search_api` (class definitions, method signatures)
  - See `.env.example` for usage

### Technology

- **Embeddings:** `all-MiniLM-L6-v2` via sentence-transformers (local, no API key)
- **Vector store:** ChromaDB with cosine distance
- **Keyword index:** SQLite FTS5 with porter tokenizer
- **Reranking:** Reciprocal Rank Fusion + code-intent heuristics
- **Transport:** stdio (MCP JSON-RPC)

## Dashboard

The project includes an interactive dashboard for understanding the index — what's
in it, how chunks distribute across repos and content types, and how concepts
relate in embedding space. We built it because tuning retrieval quality requires
seeing the data: which repos dominate, where docs and source code overlap
semantically, and whether cluster boundaries match our intuition about concept
groupings.

- **Index Explorer** (`dashboard/public/index.html`) — treemap of repo × content
  type distribution, content type doughnut, AST chunk type breakdown, method
  length histogram, and chunk size comparison. All data loaded from
  `dashboard_data.json` (generated, not hardcoded).
- **Latent Space Explorer** (`dashboard/public/latent-space.html`) — 3D
  point cloud of all chunks projected from 384D embeddings to 3D via UMAP
  (cosine metric). Supports rotate/zoom/pan, content type filtering, search
  highlighting, and cluster expansion with labels. Uses Three.js with
  additive blending so overlapping content types produce mixed colours.

```bash
# Rebuild dashboard data from the current index
just dashboard-build

# Or refresh the index first, then rebuild
just dashboard-refresh

# Serve on localhost:8765
just dashboard-serve
```

## Development

A `justfile` provides common tasks. Run `just` to see all recipes.

```bash
just check    # lint + format check + typecheck
just test     # run tests
```

Or use `uv` directly:

```bash
# Install dev dependencies
uv pip install -e ".[dev]"

# Run tests
uv run pytest tests/ -v

# Type checking
uv run mypy src/ tests/

# Lint
uv run ruff check
```

## Project Structure

```
src/pipecat_context_hub/
├── cli.py                          # CLI entry point (serve + refresh)
├── shared/
│   ├── types.py                    # 25+ Pydantic models (data contracts)
│   ├── interfaces.py               # Service protocols
│   └── config.py                   # Configuration models
├── services/
│   ├── embedding.py                # EmbeddingService + EmbeddingIndexWriter
│   ├── ingest/
│   │   ├── ast_extractor.py        # Python AST analysis (classes, methods, imports, yields, calls)
│   │   ├── docs_crawler.py         # llms-full.txt ingester + markdown chunker
│   │   ├── github_ingest.py        # Git clone/fetch + code chunking
│   │   ├── source_ingest.py        # Source code chunking + module metadata
│   │   └── taxonomy.py             # Automated capability inference
│   ├── index/
│   │   ├── vector.py               # ChromaDB vector index
│   │   ├── fts.py                  # SQLite FTS5 keyword index
│   │   └── store.py                # Unified IndexStore facade
│   └── retrieval/
│       ├── decompose.py            # Multi-concept query decomposition
│       ├── hybrid.py               # HybridRetriever (7 tool methods)
│       ├── rerank.py               # RRF + code-intent reranking
│       └── evidence.py             # Citation + evidence assembly
└── server/
    ├── main.py                     # MCP server with 7 tools
    ├── transport.py                # stdio transport
    └── tools/                      # Per-tool handler modules

dashboard/
├── public/                         # Served by `just dashboard-serve`
│   ├── index.html                  # Stats dashboard (loads dashboard_data.json)
│   └── latent-space.html           # 3D embedding space explorer (Three.js)
└── scripts/                        # Data extraction pipeline
    ├── extract_embeddings.py       # ChromaDB → UMAP 3D projection
    ├── compute_clusters.py         # K-means clustering for LOD
    └── extract_dashboard.py        # Index stats extraction
```
