# Pipecat Context Hub

Local-first MCP server providing fresh Pipecat docs and examples context for
Claude Code, Cursor, VS Code, and Zed.

> **Quick links:**
> [Client Setup](#client-setup) |
> [MCP Tools](#mcp-tools) |
> [Version-Aware Queries](#version-aware-queries) |
> [Environment Variables](#environment-variables) |
> [Report an Issue](https://github.com/pipecat-ai/pipecat-context-hub/issues/new/choose)

## What It Does

When your AI coding assistant needs Pipecat context, it calls MCP tools exposed
by this server. The server queries a local index (ChromaDB + SQLite FTS5) and
returns relevant documentation, code examples, and API source — all with source
citations.

```
IDE/Agent  ←stdio→  pipecat-context-hub serve  ←→  Local index (~/.pipecat-context-hub/)
```

## Install

```bash
# Option A: uv (recommended — installs into an isolated environment)
uv tool install pipecat-context-hub

# Option B: pip
pip install pipecat-context-hub
```

## Populate the Local Index

Before the server can answer queries, build the local index:

```bash
# First-time setup (downloads docs, clones repos, computes embeddings)
pipecat-context-hub refresh

# Force full re-ingest (ignores cached state)
pipecat-context-hub refresh --force

# Recover from an unhealthy local index
pipecat-context-hub refresh --force --reset-index
```

> **Tip:** When `gh` CLI is authenticated, `refresh` also fetches GitHub release
> notes for deprecation data. Without it, `check_deprecation` coverage will be
> limited.

## Start the Server

```bash
pipecat-context-hub serve
```

## Client Setup

Add the server to your IDE's MCP config. Per-client setup guides with
copy-paste configs:

| Client | Setup Guide |
|--------|-------------|
| **Claude Code** | [docs/setup/claude-code.md](setup/claude-code.md) |
| **Cursor** | [docs/setup/cursor.md](setup/cursor.md) |
| **VS Code** | [docs/setup/vscode.md](setup/vscode.md) |
| **Zed** | [docs/setup/zed.md](setup/zed.md) |

Ready-to-use config templates are in [`config/clients/`](../config/clients/).

> **Recommended:** Add a `CLAUDE.md` snippet to your project so Claude prefers
> the MCP tools for Pipecat questions. See the
> [Claude Code setup guide](setup/claude-code.md#recommended-claudemd-instructions)
> for the recommended instructions.

## MCP Tools

| Tool | Use when... |
|------|-------------|
| `search_docs` | "How do I ...?" — conceptual questions, guides, configuration |
| `get_doc` | Retrieve a specific doc page by ID or path (e.g. `/guides/learn/transports`) |
| `search_examples` | "Show me an example of ..." — find working code by task or component |
| `get_example` | Retrieve full source files for a specific example |
| `search_api` | Class definitions, method signatures, frame types, inheritance |
| `get_code_snippet` | Get targeted code by symbol name, intent, or file path + line range |
| `check_deprecation` | Verify whether a pipecat import path is deprecated |
| `get_hub_status` | Index health: last refresh time, record counts, commit SHAs |

All search results include an **EvidenceReport** with confidence scores,
source-grounded facts, unresolved questions, and suggested follow-up queries.

### Filters

`search_examples` supports filters to narrow results:

- `domain` — `"backend"` (Python), `"frontend"` (JS/TS), `"config"`, `"infra"`
- `language` — `"python"`, `"typescript"`
- `repo` — filter by GitHub repo slug
- `tags` — filter by capability tags

`search_api` supports filters for framework internals:

- `module` — module path prefix (e.g. `"pipecat.services"`)
- `class_name` — class name prefix (e.g. `"DailyTransport"`)
- `chunk_type` — `"method"`, `"function"`, `"class_overview"`, `"module_overview"`, `"type_definition"`
- `yields` — methods that yield a specific frame type
- `calls` — methods that call a specific method

### Multi-Concept Queries

Use ` + ` or ` & ` to search for multiple concepts at once:

```
search_docs("TTS + STT")
search_examples("idle timeout + function calling + Gemini")
search_api("BaseTransport + WebSocketTransport")
```

Each concept is searched independently and results are interleaved for balanced
coverage.

## Version-Aware Queries

If your project targets a specific pipecat version, pass `pipecat_version` to
get results scored for compatibility:

```
search_examples("TTS pipeline", pipecat_version="0.0.96", domain="backend")
search_api("DailyTransport", pipecat_version="0.0.96")
```

Results are annotated with `version_compatibility`: `"compatible"`,
`"newer_required"`, `"older_targeted"`, or `"unknown"`. Use
`version_filter="compatible_only"` to exclude results requiring a newer version.

You can also pin the framework index to a specific version:

```bash
pipecat-context-hub refresh --framework-version v0.0.96
# or via env var:
PIPECAT_HUB_FRAMEWORK_VERSION=v0.0.96 pipecat-context-hub refresh
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPECAT_HUB_EXTRA_REPOS` | *(empty)* | Comma-separated repo slugs to ingest alongside defaults |
| `PIPECAT_HUB_FRAMEWORK_VERSION` | *(empty)* | Pin framework repo to a specific git tag (e.g. `v0.0.96`) |
| `PIPECAT_HUB_TAINTED_REPOS` | *(empty)* | Comma-separated repo slugs to skip entirely |
| `PIPECAT_HUB_TAINTED_REFS` | *(empty)* | Comma-separated `org/repo@ref` entries to skip |
| `PIPECAT_HUB_RERANKER_ENABLED` | `1` | Set to `0` to disable cross-encoder reranking |

See [`.env.example`](../.env.example) for curated repo bundles you can copy
into your `.env`.

## Data Sources

The default index includes:

- **Pipecat documentation** — `docs.pipecat.ai` (200+ pages)
- **Pipecat framework** — `pipecat-ai/pipecat` (Python AST-indexed: classes, methods, imports, call graphs)
- **Pipecat examples** — `pipecat-ai/pipecat-examples` (project-level code examples)
- **Daily Python SDK** — `daily-co/daily-python` (`.pyi` stubs + RST type definitions)
- **TypeScript SDKs** — `pipecat-client-web`, `pipecat-client-web-transports`, `voice-ui-kit`, and more (tree-sitter-indexed)

Add more repos via `PIPECAT_HUB_EXTRA_REPOS`.

## Security

- Threat model: [docs/security/threat-model.md](security/threat-model.md)
- Vulnerability reporting: [SECURITY.md](../SECURITY.md)
- Upstream denylisting: `PIPECAT_HUB_TAINTED_REPOS` and `PIPECAT_HUB_TAINTED_REFS`

## Troubleshooting

- **Empty results** — run `pipecat-context-hub refresh` to populate the index
- **Command not found** — ensure `pipecat-context-hub` is on your `PATH` (`uv tool list` to check)
- **Stale results** — run `refresh --force` to re-ingest from latest upstream
- **Index corruption** — run `refresh --force --reset-index` to wipe and rebuild

If the server returns poor or missing results, [file a retrieval quality issue](https://github.com/pipecat-ai/pipecat-context-hub/issues/new?template=retrieval-quality.yml) —
the issue template includes a diagnostic prompt your coding agent can run to
generate a structured report.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture, development workflow,
benchmarking, and project structure.
