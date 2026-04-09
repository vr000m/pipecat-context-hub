# Zed Setup

Connect Pipecat Context Hub to [Zed](https://zed.dev/) as an MCP server over stdio. MCP tools are available in Zed's Agent panel.

## Prerequisites

- Python 3.11+
- [Zed](https://zed.dev/) with Agent panel support
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

Zed uses a global settings file — there is no project-level MCP config.

Edit `~/.config/zed/settings.json` (open with `zed: open settings` from the command palette).
Replace `/path/to/pipecat-context-hub` with the absolute path where you cloned the repo:

```json
{
  "context_servers": {
    "pipecat-context-hub": {
      "source": "custom",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/pipecat-context-hub", "pipecat-context-hub", "serve"],
      "env": {}
    }
  }
}
```

**Note:** Zed uses `"context_servers"` (not `"mcpServers"`) and requires `"source": "custom"` for manually configured servers.

## Verify

1. Open Zed and open the Agent panel.
2. The server should appear in the MCP server list. Check for any error indicators.
3. Ask the agent a question about Pipecat — the server's tools should be invoked.

You can also verify the server starts correctly from the command line:

```bash
uv run pipecat-context-hub serve --help
```

## Troubleshooting

- **Server not appearing**: Ensure the `context_servers` key is at the top level of `settings.json` and that `"source": "custom"` is included.
- **Command not found**: Ensure the `--directory` path in your MCP config points to your `pipecat-context-hub` clone.
- **Empty results**: Run `uv run pipecat-context-hub refresh` to populate the index.
- **JSON parse errors**: Zed's `settings.json` contains other settings — make sure you merge the `context_servers` block rather than replacing the entire file.
