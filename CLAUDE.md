# Pipecat Context Hub

Local-first MCP server providing Pipecat docs, examples, and API context.

## Stack

- **Python 3.11+**, `uv` package manager, `hatchling` build
- **Embeddings:** `all-MiniLM-L6-v2` (sentence-transformers, local)
- **Vector store:** ChromaDB | **Keyword index:** SQLite FTS5
- **Transport:** stdio (MCP JSON-RPC)

## Commands

```bash
uv run pytest tests/ -v                             # full test suite
uv run ruff check src/ tests/                       # lint
uv run mypy src/ tests/                             # type check
uv run pipecat-context-hub refresh                  # incremental rebuild
uv run pipecat-context-hub refresh --force          # full re-ingest
uv run pipecat-context-hub refresh --force --reset-index  # recover unhealthy local Chroma state
uv run pipecat-context-hub serve                    # start MCP server
```

Use `refresh --force --reset-index` when the persisted local Chroma index is
unhealthy and needs a clean rebuild.

A `justfile` is also available as a task runner:

```bash
just check              # lint + format check + typecheck
just test               # run tests
just audit              # pip-audit + bandit
just sbom               # generate CycloneDX SBOM
just benchmark-stability  # opt-in refresh/serve/search stability benchmark
just dashboard-refresh  # refresh index + rebuild all dashboard data
just dashboard-build    # rebuild dashboard data without re-indexing
just dashboard-serve    # serve dashboard on localhost:8765
```

## MCP Tools — Multi-Concept Queries

When calling search tools (`search_docs`, `search_examples`, `search_api`, `get_code_snippet`), use ` + ` or ` & ` to search for multiple concepts at once:

```
search_docs("TTS + STT")
search_examples("idle timeout + function calling + Gemini")
search_api("BaseTransport + WebSocketTransport")
```

Each concept is searched independently and results are interleaved for balanced coverage. Do NOT stuff multiple concepts into a single natural-language query — that clusters results around whichever concept dominates the embedding.

## Example Search Filters

`search_examples` supports domain and language filters to reduce noise:

- `domain="backend"` — Python pipeline/bot code only
- `domain="frontend"` — JS/TS client code only
- `language="python"` — filter by programming language
- `language="typescript"` — filter by programming language
- Combine: `search_examples("TTS pipeline", domain="backend", language="python")`

## Versioning

The version lives in **two places** — both must be updated together on every release:

1. `pyproject.toml` → `[project].version`
2. `src/pipecat_context_hub/server/main.py` → `_SERVER_VERSION`

A test (`tests/unit/test_server.py::TestVersionConsistency`) enforces they match.

## Project Layout

```
src/pipecat_context_hub/
├── cli.py                    # CLI entry point (serve + refresh)
├── shared/                   # Types, interfaces, config
├── services/
│   ├── embedding.py          # EmbeddingService
│   ├── ingest/               # Docs crawler, GitHub ingester, Python AST, TS regex, taxonomy
│   ├── index/                # ChromaDB vector, SQLite FTS5, IndexStore
│   └── retrieval/            # HybridRetriever, decompose, rerank, evidence
└── server/
    ├── main.py               # MCP server setup (_SERVER_VERSION here)
    └── tools/                # Per-tool handler modules

dashboard/
├── public/                   # Served by `just dashboard-serve`
│   ├── index.html            # Stats dashboard (loads dashboard_data.json)
│   └── latent-space.html     # 3D embedding space explorer (Three.js)
└── scripts/                  # Data extraction pipeline
    ├── extract_embeddings.py # ChromaDB → UMAP 3D → embeddings_3d.json
    ├── compute_clusters.py   # K-means clustering → clusters.json
    └── extract_dashboard.py  # Index stats → dashboard_data.json
```

## Cross-Encoder Reranking

Cross-encoder reranking is **enabled by default**. It scores query-result pairs
for semantic relevance after RRF merge, significantly improving result quality
(especially for `search_examples` and multi-concept queries).

- **First run:** `uv run pipecat-context-hub refresh` downloads the model (~80MB)
- **Disable:** `PIPECAT_HUB_RERANKER_ENABLED=0` env var
- **Model:** `cross-encoder/ms-marco-MiniLM-L-6-v2` (configurable via `RerankerConfig`)
- **Latency:** ~50-100ms per query on CPU
- **Offline:** gracefully disabled if model not cached (falls back to RRF-only)
