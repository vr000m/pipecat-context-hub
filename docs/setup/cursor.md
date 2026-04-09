# Cursor Setup

Connect Pipecat Context Hub to [Cursor](https://cursor.com/) as an MCP server over stdio.

## Prerequisites

- Python 3.11+
- [Cursor](https://cursor.com/) installed
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

### Option A: Project-level config (recommended)

Create `.cursor/mcp.json` in your project root. Replace `/path/to/pipecat-context-hub`
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

### Option B: Global config (all projects)

Create or edit `~/.cursor/mcp.json` (same format as above).

## Verify

1. Open your project in Cursor.
2. Open Cursor Settings > MCP to confirm `pipecat-context-hub` appears and shows a green status.
3. In the AI chat, ask a question about Pipecat — the server's tools should be invoked automatically.

You can also verify the server starts correctly from the command line:

```bash
uv run pipecat-context-hub serve --help
```

## Troubleshooting

- **Server not appearing**: Ensure `.cursor/mcp.json` exists in your project root directory.
- **Command not found**: Ensure the `--directory` path in your MCP config points to your `pipecat-context-hub` clone.
- **Empty results**: Run `uv run pipecat-context-hub refresh` to populate the index.
- **Red status indicator**: Check the Cursor MCP logs for error details.
