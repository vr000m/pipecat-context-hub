# Claude Code Setup

Connect Pipecat Context Hub to [Claude Code](https://code.claude.com/) as an MCP server over stdio.

## Prerequisites

- Python 3.11+
- [Claude Code](https://code.claude.com/) installed
- [`uv`](https://docs.astral.sh/uv/) package manager

## Install

```bash
git clone https://github.com/pipecat-ai/pipecat-context-hub.git
cd pipecat-context-hub
uv sync
```

## Populate the Local Index

Before the server can answer queries, populate the local index:

```bash
uv run pipecat-context-hub refresh
```

This downloads Pipecat docs and example repos to `~/.pipecat-context-hub/`.

## Configure

### Option A: Project-level config (recommended for teams)

Create `.mcp.json` at the root of your project. Replace `/path/to/pipecat-context-hub`
with the absolute path where you cloned the repo:

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

### Option B: User-level config (all projects)

Add to `~/.claude.json` (same format as above).

### Option C: CLI

```bash
claude mcp add --scope project pipecat-context-hub -- uv run --directory /path/to/pipecat-context-hub pipecat-context-hub serve
```

## Recommended CLAUDE.md Instructions

Add these lines to your project's `CLAUDE.md` (or global `~/.claude/CLAUDE.md`) so Claude knows to use the MCP tools for Pipecat questions:

```markdown
## MCP Tools

When pipecat-context-hub MCP is available, always prefer its tools (`search_docs`, `search_api`, `search_examples`, `get_example`, `get_doc`, `get_code_snippet`, `check_deprecation`) for Pipecat framework questions. Do not read `.venv` or source files directly.

- "How do I ...?" → `search_docs`
- "Show me an example of ..." → `search_examples`, then `get_example`
- Class constructors, method signatures, frame types → `search_api`
- Specific code span or symbol → `get_code_snippet`
- Retrieve a specific doc page → `get_doc`
- Check if an import is deprecated → `check_deprecation`

**Multi-concept queries:** Use ` + ` or ` & ` as delimiters (e.g., `search_docs("TTS + STT")`). Each concept is searched independently and results are interleaved.

When suggesting commands for Pipecat projects, always use `uv` as the package manager:
- Install dependencies: `uv sync` (not `pip install`)
- Run scripts: `uv run python bot.py` (not `python bot.py`)
- Add packages: `uv add <package>` (not `pip install <package>`)
```

## Verify

1. Start Claude Code in your project directory.
2. Claude Code will detect the MCP config and prompt you to approve the server on first use.
3. Ask Claude a question about Pipecat — the server's tools should appear in the tool list.

You can also verify the server starts correctly from the command line:

```bash
# Check that the serve command is available
uv run pipecat-context-hub serve --help

# Test stdin/stdout communication (sends an MCP initialize request)
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1.0"}}}' | uv run pipecat-context-hub serve
```

## Troubleshooting

- **Server not detected**: Ensure `.mcp.json` is at the project root (not inside `.claude/`).
- **Command not found**: Ensure the `--directory` path in your MCP config points to your `pipecat-context-hub` clone.
- **Empty results**: Run `uv run pipecat-context-hub refresh` to populate the index.
