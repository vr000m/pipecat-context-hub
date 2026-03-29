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
| `get_doc` | Fetch a specific doc page by chunk ID or path (e.g. `/guides/learn/transports`) |
| `search_examples` | Find working code examples by task, modality, or component. Filter by `language` (python, typescript), `domain` (backend, frontend, config, infra), `tags`, or `repo` |
| `get_example` | Retrieve full example with source files and metadata |
| `get_code_snippet` | Get targeted code spans by intent, symbol, or path. Returns enriched output with dependencies (`dependency_notes`), called methods (`companion_snippets`), related type definitions (`related_type_defs`), and interface contracts (`interface_expectations`) |
| `search_api` | Search framework internals — class definitions, method signatures, type definitions, inheritance. Filter by `module`, `class_name` (prefix match), `chunk_type` (including `type_definition` for dict schemas), `yields` (frame types), or `calls` (method names) |
| `get_hub_status` | Get index health: last refresh time, record counts, commit SHAs |

All responses include an `EvidenceReport` with `known`/`unknown` items, confidence scores, and suggested follow-up queries.

## Quick Start

```bash
# Install the project and dev tooling from the lockfile
uv sync --extra dev --group dev

# Populate the local index (crawls docs + clones repos + computes embeddings)
uv run pipecat-context-hub refresh

# Force full re-ingest, ignoring cached state
uv run pipecat-context-hub refresh --force

# Recover from an unhealthy local Chroma index and rebuild from scratch
uv run pipecat-context-hub refresh --force --reset-index

# Start the MCP server
uv run pipecat-context-hub serve
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

## Security

The MCP server threat model and trust-boundary review live in
[docs/security/threat-model.md](security/threat-model.md).

Local upstream denylisting is available when a repo or release is suspected to
be tainted:

- `PIPECAT_HUB_TAINTED_REPOS` skips a repo entirely
- `PIPECAT_HUB_TAINTED_REFS` skips specific `org/repo@ref` entries where `ref`
  is a tag or commit SHA/prefix

## Architecture

```
Ingestion:
  DocsCrawler (llms-full.txt)    ──┐
  GitHubRepoIngester (N repos)   ──┤→ EmbeddingIndexWriter → IndexStore
  SourceIngester (per-repo AST)  ──┤   (sentence-transformers)   (ChromaDB + FTS5)
  TaxonomyBuilder (auto-infer)   ──┘
    ↑                                         ↑
    Per-file taxonomy enrichment:             Metadata stored per chunk:
    foundational_class, capability_tags,      language, domain, execution_mode,
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
- `daily-co/daily-python` — Daily Python SDK (`.pyi` type stub AST-indexed for `search_api`)
  - Indexes `CallClient`, `EventHandler`, 87 types, all method signatures via `daily.pyi`
  - Indexes type definitions from `docs/src/types.rst` (72 dict schemas, enums, aliases) as `type_definition` chunks for `search_api`
  - Demos indexed as code examples
- Additional repos via `PIPECAT_HUB_EXTRA_REPOS` env var (comma-separated slugs)
  - Supports single-project repos (`src/`-layout, root-level entry scripts)
  - Repos with `src/` layouts are AST-indexed for `search_api` (class definitions, method signatures)
  - Repos with `.pyi` stubs at root (no Python in `src/`) are also AST-indexed
  - See `.env.example` for usage and copy-ready curated repo bundles

### Technology

- **Embeddings:** `all-MiniLM-L6-v2` via sentence-transformers (local, no API key)
- **Vector store:** ChromaDB with cosine distance
- **Keyword index:** SQLite FTS5 with porter tokenizer
- **Reranking:** Reciprocal Rank Fusion + code-intent heuristics + cross-encoder (enabled by default) + result diversity
- **Transport:** stdio (MCP JSON-RPC)

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPECAT_HUB_EXTRA_REPOS` | *(empty)* | Comma-separated repo slugs to ingest alongside defaults |
| `PIPECAT_HUB_TAINTED_REPOS` | *(empty)* | Comma-separated repo slugs to skip entirely and remove from the active refresh set |
| `PIPECAT_HUB_TAINTED_REFS` | *(empty)* | Comma-separated `org/repo@ref` entries. `ref` may be a tag or commit SHA/prefix; refresh skips a repo when fetched HEAD matches one of these refs |
| `PIPECAT_HUB_RERANKER_ENABLED` | `1` (enabled) | Set to `0` to disable cross-encoder reranking |
| `PIPECAT_HUB_ENABLE_QUALITY_BENCHMARK` | *(empty)* | Set to `1` to opt into the retrieval-quality benchmark when running it directly with `pytest` |
| `PIPECAT_HUB_BENCHMARK_OUTPUT` | *(empty)* | Optional JSON output path for the retrieval-quality benchmark report |
| `PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK` | *(empty)* | Set to `1` to opt into the runtime stability benchmark when running it directly with `pytest` |
| `PIPECAT_HUB_STABILITY_OUTPUT` | *(empty)* | Optional JSON output path for the runtime stability benchmark report |


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

  ![Index Explorer](sshot-dashboard-index.jpg)

- **Latent Space Explorer** (`dashboard/public/latent-space.html`) — 3D
  point cloud of all chunks projected from 384D embeddings to 3D via UMAP
  (cosine metric). Supports rotate/zoom/pan, content type filtering, search
  highlighting, and cluster expansion with labels. Uses Three.js with
  additive blending so overlapping content types produce mixed colours.

  ![Latent Space Explorer](sshot-dashboard-latent-space.png)

```bash
# Rebuild dashboard data from the current index
just dashboard-build

# Or refresh the index first, then rebuild
just dashboard-refresh

# Serve on localhost:8765
just dashboard-serve
```

## Development

A [`justfile`](https://github.com/casey/just) provides common tasks. Install with `brew install just` ([other platforms](https://github.com/casey/just#installation)). Run `just` to see all recipes.

```bash
just check    # lint + format check + typecheck
just test     # run tests
just audit    # pip-audit on the frozen env + bandit
just sbom     # generate a reproducible CycloneDX SBOM
just benchmark-quality   # live retrieval-quality benchmark on the local index
```

Or use `uv` directly:

```bash
# Install dev dependencies
uv sync --extra dev --group dev

# Run tests
uv run pytest tests/ -v

# Type checking
uv run mypy src/ tests/

# Lint
uv run ruff check
```

## Benchmarking

Two benchmark modes exist:

- `tests/benchmarks/test_latency.py` measures component and end-to-end latency on a seeded local corpus.
- `tests/benchmarks/test_retrieval_quality.py` measures retrieval quality against the current local index.
- `tests/benchmarks/test_runtime_stability.py` measures repeated `refresh` / `serve` lifecycle stability and concurrent retrieval growth in RSS, thread count, and open file descriptors.

The retrieval-quality benchmark is intended for the default corpus:

- Pipecat docs
- `pipecat-ai/pipecat`
- `pipecat-ai/pipecat-examples`
- No `PIPECAT_HUB_EXTRA_REPOS`

Run it after `uv run pipecat-context-hub refresh`:

```bash
just benchmark-quality
```

Run the runtime stability benchmark when you want an opt-in soak/leak pass:

```bash
just benchmark-stability
```

If the benchmark reports an unhealthy local vector index, rebuild it with:

```bash
uv run pipecat-context-hub refresh --force --reset-index
```

To persist a versioned report for later comparison:

```bash
PIPECAT_HUB_BENCHMARK_OUTPUT=artifacts/benchmarks/retrieval-quality-0.0.9.json just benchmark-quality
just benchmark-stability-report
```

Each JSON report includes:

- `schema_version` and `matrix_version` so query-set changes are explicit
- `server_version`
- `last_refresh_at`
- `docs_content_hash`
- `repo_shas` and `repo_counts`
- per-case scores and top hits

That metadata is the version-to-version trail. If a score changes, you can first check whether the retrieval logic changed, the indexed repo SHAs changed, the docs content hash changed, or the benchmark matrix itself changed.

If extra repos are present, the benchmark still runs and writes a scorecard, but threshold failures are downgraded to warnings because the corpus is no longer comparable to the default baseline.

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
