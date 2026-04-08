# Contributing to Pipecat Context Hub

## Development Setup

```bash
# Clone and install dev dependencies from the lockfile
git clone https://github.com/pipecat-ai/pipecat-context-hub.git
cd pipecat-context-hub
uv sync --extra dev --group dev

# Run tests
uv run pytest tests/ -v

# Lint + typecheck
uv run ruff check src/ tests/
uv run mypy src/ tests/
```

A [`justfile`](https://github.com/casey/just) provides common tasks
(`brew install just`):

```bash
just check    # lint + format check + typecheck
just test     # run tests
just audit    # pip-audit + bandit
just sbom     # generate CycloneDX SBOM
```

## Architecture

```
Ingestion:
  DocsCrawler (llms-full.txt)    ──┐
  GitHubRepoIngester (N repos)   ──┤→ EmbeddingIndexWriter → IndexStore
  SourceIngester (AST + tree-sitter)─┤   (sentence-transformers)   (ChromaDB + FTS5)
  TaxonomyBuilder (auto-infer)   ──┘

Retrieval:
  MCP Tool Call → HybridRetriever → decompose_query (split on + / &)
                    ↓                     ↓
              single-concept         multi-concept (parallel per-concept)
                    ↓                     ↓
              vector + keyword      round-robin interleave + dedup
                    ↓                     ↓
                  rerank (RRF + cross-encoder + heuristics)
                    ↓
                  Cited response with EvidenceReport
```

### Technology Stack

- **Embeddings:** `all-MiniLM-L6-v2` via sentence-transformers (local, no API key)
- **AST parsing:** Python `ast` module (Python), `tree-sitter` (TypeScript/TSX)
- **Vector store:** ChromaDB with cosine distance
- **Keyword index:** SQLite FTS5 with porter tokenizer
- **Reranking:** Reciprocal Rank Fusion + code-intent heuristics + cross-encoder
  (enabled by default) + result diversity penalties
- **Transport:** stdio (MCP JSON-RPC)

### Data Sources

- `https://docs.pipecat.ai/llms-full.txt` — primary documentation
- `pipecat-ai/pipecat` — framework repo (Python AST-indexed)
  - Supports flat file layout and subdirectory layout in `examples/`
- `pipecat-ai/pipecat-examples` — project-level examples
- `daily-co/daily-python` — Daily Python SDK (`.pyi` stubs + RST type definitions)
- **TypeScript SDK repos** (default since v0.0.12):
  - `pipecat-ai/pipecat-client-web`, `pipecat-ai/pipecat-client-web-transports`,
    `pipecat-ai/voice-ui-kit`, `pipecat-ai/pipecat-flows-editor`,
    `pipecat-ai/web-client-ui`, `pipecat-ai/small-webrtc-prebuilt`
  - Tree-sitter-extracted: interfaces, classes, types, functions, enums, const exports
    with individual method chunks and full signatures
- Additional repos via `PIPECAT_HUB_EXTRA_REPOS` env var
  - Repos with `src/` layouts are Python AST-indexed for `search_api`
  - Repos with `.pyi` stubs at root are also AST-indexed
  - Repos with `package.json`/`tsconfig.json` are tree-sitter-indexed
  - See `.env.example` for curated repo bundles

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
│   │   ├── ast_extractor.py        # Python AST (classes, methods, imports, yields, calls)
│   │   ├── docs_crawler.py         # llms-full.txt ingester + markdown chunker
│   │   ├── github_ingest.py        # Git clone/fetch + code chunking
│   │   ├── source_ingest.py        # Source code chunking + module metadata
│   │   ├── ts_tree_sitter_parser.py # TypeScript/TSX tree-sitter extraction
│   │   └── taxonomy.py             # Automated capability inference
│   ├── index/
│   │   ├── vector.py               # ChromaDB vector index
│   │   ├── fts.py                  # SQLite FTS5 keyword index
│   │   └── store.py                # Unified IndexStore facade
│   └── retrieval/
│       ├── decompose.py            # Multi-concept query decomposition
│       ├── hybrid.py               # HybridRetriever (8 tool methods)
│       ├── rerank.py               # RRF + code-intent reranking
│       ├── cross_encoder.py        # Cross-encoder reranker
│       └── evidence.py             # Citation + evidence assembly
└── server/
    ├── main.py                     # MCP server setup
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

## Dashboard

Interactive dashboard for understanding the index — what's in it, how chunks
distribute, and how concepts relate in embedding space.

- **Index Explorer** (`dashboard/public/index.html`) — treemap, content type
  breakdown, AST chunk types, method length histogram
- **Latent Space Explorer** (`dashboard/public/latent-space.html`) — 3D point
  cloud of all chunks via UMAP projection (Three.js)

```bash
just dashboard-build     # rebuild dashboard data from current index
just dashboard-refresh   # refresh index + rebuild dashboard
just dashboard-serve     # serve on localhost:8765
```

## Benchmarking

Two benchmark modes:

- **Latency** (`tests/benchmarks/test_latency.py`) — component and end-to-end
  latency on a seeded local corpus
- **Retrieval quality** (`tests/benchmarks/test_retrieval_quality.py`) — measures
  result relevance against the current local index
- **Runtime stability** (`tests/benchmarks/test_runtime_stability.py`) — repeated
  refresh/serve cycles, concurrent retrieval, RSS/thread/FD growth

```bash
# Retrieval quality (run after `pipecat-context-hub refresh`)
just benchmark-quality

# Runtime stability (opt-in soak/leak pass)
just benchmark-stability

# Persist a versioned report for comparison
PIPECAT_HUB_BENCHMARK_OUTPUT=artifacts/benchmarks/quality-0.0.16.json just benchmark-quality
```

Benchmark reports include `server_version`, `last_refresh_at`,
`docs_content_hash`, `repo_shas`, and per-case scores for version-to-version
tracking.

If extra repos are present, threshold failures are downgraded to warnings
(corpus no longer comparable to the default baseline).

## Versioning

The version lives in **two places** — both must be updated together on every
release:

1. `pyproject.toml` → `[project].version`
2. `src/pipecat_context_hub/server/main.py` → `_SERVER_VERSION`

A test (`tests/unit/test_server.py::TestVersionConsistency`) enforces they match.

## Release Process

See the [Release Notes Template](../CLAUDE.md#release-notes-template) in
CLAUDE.md for the standardised format. Every release needs:

1. Update `CHANGELOG.md` (change "Unreleased" → date)
2. Bump version in both locations above
3. Commit and merge via PR
4. Create GitHub release via `gh release create vX.Y.Z`
