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
git clone https://github.com/pipecat-ai/pipecat-context-hub.git
cd pipecat-context-hub
uv sync
```

## Populate the Local Index

Before the server can answer queries, build the local index:

```bash
# First-time setup (downloads docs, clones repos, computes embeddings)
uv run pipecat-context-hub refresh

# Force full re-ingest (ignores cached state)
uv run pipecat-context-hub refresh --force

# Recover from an unhealthy local index
uv run pipecat-context-hub refresh --force --reset-index
```

> **Tip:** When `gh` CLI is authenticated, `refresh` also fetches GitHub release
> notes for deprecation data. Without it, `check_deprecation` coverage will be
> limited.

## Start the Server

Run `refresh` at least once first (see above). `serve` exits with code `2`
if the index is empty or cannot be opened — it will not start against an
unusable index, since MCP clients would otherwise hang on zero-hit queries.

```bash
uv run pipecat-context-hub serve
```

## Client Setup

Point your IDE's MCP config at the cloned repo using `uv run --directory`.
Per-client setup guides:

| Client | Setup Guide |
|--------|-------------|
| **Claude Code** | [docs/setup/claude-code.md](setup/claude-code.md) |
| **Cursor** | [docs/setup/cursor.md](setup/cursor.md) |
| **VS Code** | [docs/setup/vscode.md](setup/vscode.md) |
| **Zed** | [docs/setup/zed.md](setup/zed.md) |

**Example** (Claude Code `.mcp.json`):

```json
{
  "mcpServers": {
    "pipecat-context-hub": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/pipecat-context-hub", "pipecat-context-hub", "serve"],
      "env": {}
    }
  }
}
```

Config templates for all clients are in [`config/clients/`](../config/clients/).

### Add CLAUDE.md Instructions (Recommended)

Add this to your project's `CLAUDE.md` (or `~/.claude/CLAUDE.md` globally) so
your coding agent prefers the MCP tools for Pipecat questions:

```markdown
## MCP Tools

When pipecat-context-hub MCP is available, always prefer its tools
(`search_docs`, `search_api`, `search_examples`, `get_example`, `get_doc`,
`get_code_snippet`, `check_deprecation`) for Pipecat framework questions.
Do not read `.venv` or source files directly.

- "How do I ...?" → `search_docs`
- "Show me an example of ..." → `search_examples`, then `get_example`
- Class constructors, method signatures, frame types → `search_api`
- Specific code span or symbol → `get_code_snippet`
- Retrieve a specific doc page → `get_doc`
- Check if an import is deprecated → `check_deprecation`

**Multi-concept queries:** Use ` + ` or ` & ` as delimiters
(e.g., `search_docs("TTS + STT")`). Each concept is searched independently
and results are interleaved.

When suggesting commands for Pipecat projects, always use `uv` as the
package manager:
- Install dependencies: `uv sync` (not `pip install`)
- Run scripts: `uv run python bot.py` (not `python bot.py`)
- Add packages: `uv add <package>` (not `pip install <package>`)
```

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
| `get_hub_status` | Index health, reranker runtime state, record counts, framework version, commit SHAs |

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
uv run pipecat-context-hub refresh --framework-version v0.0.96
# or via env var:
PIPECAT_HUB_FRAMEWORK_VERSION=v0.0.96 uv run pipecat-context-hub refresh
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPECAT_HUB_EXTRA_REPOS` | *(empty)* | Comma-separated repo slugs to ingest alongside defaults |
| `PIPECAT_HUB_FRAMEWORK_VERSION` | *(empty)* | Pin framework repo to a specific git tag (e.g. `v0.0.96`) |
| `PIPECAT_HUB_TAINTED_REPOS` | *(empty)* | Comma-separated repo slugs to skip entirely |
| `PIPECAT_HUB_TAINTED_REFS` | *(empty)* | Comma-separated `org/repo@ref` entries to skip |
| `PIPECAT_HUB_RERANKER_ENABLED` | `1` | Set to `0` to disable cross-encoder reranking |
| `PIPECAT_HUB_RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Swap reranker model. Allowed: `cross-encoder/ms-marco-MiniLM-L-6-v2` (~80 MB), `cross-encoder/ms-marco-MiniLM-L-12-v2` (~130 MB), `cross-encoder/ms-marco-TinyBERT-L-2-v2` (~17 MB) |
| `PIPECAT_HUB_IDLE_TIMEOUT_SECS` | `1800` | Exit `serve` if no MCP request arrives for this many seconds (30 min default). Set to `0` to disable. |
| `PIPECAT_HUB_PARENT_WATCH_INTERVAL` | `2.0` | Hidden tuning knob (primarily for tests): poll interval (seconds) for the parent-death watchdog. Floored at `0.1s` when non-zero. Set to `0` to disable the watchdog. |

See [`.env.example`](../.env.example) for curated repo bundles you can copy
into your `.env`.

## MCP Client Configuration

Two ways to point an MCP client (Claude Code, Cursor, Zed, etc.) at this
hub. They differ in how cleanly the server exits when the client goes away.

**Recommended — direct invocation (instant orphan cleanup):**

```json
{
  "mcpServers": {
    "pipecat-context-hub": {
      "command": "/absolute/path/to/pipecat-context-hub/.venv/bin/pipecat-context-hub",
      "args": ["serve"]
    }
  }
}
```

Python is the immediate child of the MCP client. When the client dies
or restarts, the parent-death watchdog fires within ~2s and the hub
exits cleanly, releasing the Chroma + SQLite handles.

**Alternative — `uv run` (simpler, slower cleanup):**

```json
{
  "mcpServers": {
    "pipecat-context-hub": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/pipecat-context-hub", "pipecat-context-hub", "serve"]
    }
  }
}
```

Convenient (no need to know the venv path) but `uv` stays alive as an
intermediate parent, so the parent-death watchdog cannot detect client
death from inside Python. The 30-minute idle-timeout backstop
(`PIPECAT_HUB_IDLE_TIMEOUT_SECS`) still fires, so orphans don't
accumulate forever — they just take longer to clear. Tune the env var
down (e.g. `300` for 5 minutes) if you spawn lots of short-lived
sessions.

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

- **Empty results** — run `uv run pipecat-context-hub refresh` to populate the index
- **Stale results** — run `uv run pipecat-context-hub refresh --force` to re-ingest from latest upstream
- **Index corruption** — run `uv run pipecat-context-hub refresh --force --reset-index` to wipe and rebuild
- **`serve` exits immediately with code 2** — the index is empty or
  unopenable. Run `uv run pipecat-context-hub refresh` (or
  `refresh --force --reset-index` if the error message mentions a failed
  open) and try again. This is deliberate: prior versions started anyway
  and MCP clients hung on every query.
- **Stale `serve` processes** — `serve` polls its parent PID every 2s
  and exits cleanly when the MCP client disappears (look for
  `Shutting down: parent_died original_ppid=… current_ppid=1` in the
  trace). If you still see orphans (older versions, or Windows where the
  watchdog is disabled), `pkill -f "pipecat-context-hub serve"` is safe
  to run between sessions.
- **Diagnosing degraded starts** — on `serve` boot, look for
  `pipecat-context-hub vX.Y.Z starting: …` (`INFO`) to confirm the running
  version and index content-type counts. If reranking is off, a
  `Reranker disabled at startup: reason=…` (`WARNING`) line names the
  cause (`config_disabled` | `not_cached`) and, for `not_cached`, the
  exact HF cache directory probed.

### Windows

- **Refresh appears to hang or returns zero code results** — a prior
  `refresh` may have left a clone half-initialised (common after an
  interrupted run or antivirus quarantine). `pipecat-context-hub` now
  detects this on the next refresh and re-clones automatically; look for
  `Recovered N corrupt clone(s): …` in the summary. As a manual remedy you
  can delete `%LOCALAPPDATA%\pipecat-context-hub\repos\`.
- **`UnicodeEncodeError` in the refresh summary** — the summary table uses
  box-drawing characters that some Windows code pages (cp1252, cp1254,
  etc.) cannot encode. The server falls back to ASCII automatically. To
  opt into the full Unicode output, set `PYTHONIOENCODING=utf-8` before
  invoking `refresh`, or use Windows Terminal (which defaults to UTF-8).

If the server returns poor or missing results, [file a retrieval quality issue](https://github.com/pipecat-ai/pipecat-context-hub/issues/new?template=retrieval-quality.yml) —
the issue template includes a diagnostic prompt your coding agent can run to
generate a structured report.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture, development workflow,
benchmarking, and project structure.
