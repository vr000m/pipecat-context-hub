# Pipecat Context Hub

Local-first MCP server providing Pipecat docs, examples, and API context.

## Stack

- **Python 3.11+**, `uv` package manager, `hatchling` build
- **Embeddings:** `all-MiniLM-L6-v2` (sentence-transformers, local)
- **Vector store:** ChromaDB | **Keyword index:** SQLite FTS5
- **Transport:** stdio (MCP JSON-RPC)

## Commands

```bash
uv run pytest tests/ -v          # full test suite
uv run ruff check src/ tests/    # lint
uv run mypy src/ tests/          # type check
uv run pipecat-context-hub refresh  # rebuild index
uv run pipecat-context-hub serve    # start MCP server
```

## MCP Tools — Multi-Concept Queries

When calling search tools (`search_docs`, `search_examples`, `search_api`, `get_code_snippet`), use ` + ` or ` & ` to search for multiple concepts at once:

```
search_docs("TTS + STT")
search_examples("idle timeout + function calling + Gemini")
search_api("BaseTransport + WebSocketTransport")
```

Each concept is searched independently and results are interleaved for balanced coverage. Do NOT stuff multiple concepts into a single natural-language query — that clusters results around whichever concept dominates the embedding.

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
│   ├── ingest/               # Docs crawler, GitHub ingester, AST, taxonomy
│   ├── index/                # ChromaDB vector, SQLite FTS5, IndexStore
│   └── retrieval/            # HybridRetriever, decompose, rerank, evidence
└── server/
    ├── main.py               # MCP server setup (_SERVER_VERSION here)
    └── tools/                # Per-tool handler modules
```
