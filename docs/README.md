# Pipecat Context Hub

Local-first MCP server providing fresh Pipecat docs and examples context for Claude Code, Cursor, VS Code, and Zed.

## What It Does

When your AI coding assistant needs Pipecat context, it calls MCP tools exposed by this server. The server queries a local index (ChromaDB + SQLite FTS5) and returns relevant documentation, code examples, and snippets — all with source citations.

```
IDE/Agent  ←stdio→  pipecat-context-hub serve  ←→  Local index (~/.pipecat-context-hub/)
```

### MCP Tools (v0)

| Tool | Purpose |
|------|---------|
| `search_docs` | Search Pipecat documentation by query |
| `get_doc` | Fetch a specific doc page by ID |
| `search_examples` | Find code examples by task, capability, or component |
| `get_example` | Retrieve full example with files and metadata |
| `get_code_snippet` | Get targeted code spans by intent, symbol, or path |

All responses include an `EvidenceReport` with `known`/`unknown` items, confidence scores, and suggested follow-up queries.

## Quick Start

```bash
# Install (editable, with dev deps)
uv pip install -e ".[dev]"

# Populate the local index (crawls docs + clones repos + computes embeddings)
pipecat-context-hub refresh

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
  DocsCrawler (docs.pipecat.ai)  ──┐
  GitHubRepoIngester (2 repos)   ──┤→ EmbeddingIndexWriter → IndexStore
  TaxonomyBuilder (auto-infer)   ──┘   (sentence-transformers)   (ChromaDB + FTS5)

Retrieval:
  MCP Tool Call → HybridRetriever → vector_search + keyword_search
                    ↓                     ↓
                  rerank (RRF)      evidence assembly
                    ↓
                  Cited response with EvidenceReport
```

### Data Sources (v0)

- `https://docs.pipecat.ai/` — primary documentation
- `pipecat-ai/pipecat` — framework repo (including `examples/foundational`)
- `pipecat-ai/pipecat-examples` — project-level examples

### Technology

- **Embeddings:** `all-MiniLM-L6-v2` via sentence-transformers (local, no API key)
- **Vector store:** ChromaDB with cosine distance
- **Keyword index:** SQLite FTS5 with porter tokenizer
- **Reranking:** Reciprocal Rank Fusion + code-intent heuristics
- **Transport:** stdio (MCP JSON-RPC)

## Development

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
│   │   ├── docs_crawler.py         # HTML→markdown chunking crawler
│   │   ├── github_ingest.py        # Git clone/fetch + code chunking
│   │   └── taxonomy.py             # Automated capability inference
│   ├── index/
│   │   ├── vector.py               # ChromaDB vector index
│   │   ├── fts.py                  # SQLite FTS5 keyword index
│   │   └── store.py                # Unified IndexStore facade
│   └── retrieval/
│       ├── hybrid.py               # HybridRetriever (5 tool methods)
│       ├── rerank.py               # RRF + code-intent reranking
│       └── evidence.py             # Citation + evidence assembly
└── server/
    ├── main.py                     # MCP server with 5 tools
    ├── transport.py                # stdio transport
    └── tools/                      # Per-tool handler modules
```
