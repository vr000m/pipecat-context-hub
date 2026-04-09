# VS Code Setup

Connect Pipecat Context Hub to [VS Code](https://code.visualstudio.com/) as an MCP server over stdio. Requires GitHub Copilot with MCP support enabled.

## Prerequisites

- Python 3.11+
- [VS Code](https://code.visualstudio.com/) 1.99+ (MCP support)
- [GitHub Copilot](https://marketplace.visualstudio.com/items?itemName=GitHub.copilot) extension
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

### Option A: Workspace config (recommended for teams)

Create `.vscode/mcp.json` in your project root. Replace `/path/to/pipecat-context-hub`
with the absolute path where you cloned the repo:

```json
{
  "servers": {
    "pipecat-context-hub": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/pipecat-context-hub", "pipecat-context-hub", "serve"],
      "env": {}
    }
  }
}
```

**Note:** VS Code uses `"servers"` (not `"mcpServers"`) and requires an explicit `"type": "stdio"` field.

### Option B: User settings (all workspaces)

Open your VS Code `settings.json` and add:

```json
{
  "mcp": {
    "servers": {
      "pipecat-context-hub": {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "--directory", "/path/to/pipecat-context-hub", "pipecat-context-hub", "serve"],
        "env": {}
      }
    }
  }
}
```

## Verify

1. Open your project in VS Code.
2. Open the Command Palette (`Ctrl+Shift+P` / `Cmd+Shift+P`) and run **MCP: List Servers** to confirm `pipecat-context-hub` is listed.
3. In Copilot Chat, set the mode to **Agent** and ask a question about Pipecat.

You can also verify the server starts correctly from the command line:

```bash
uv run pipecat-context-hub serve --help
```

## Troubleshooting

- **Server not listed**: Ensure `.vscode/mcp.json` is in your workspace root and that `"type": "stdio"` is present.
- **Command not found**: Ensure the `--directory` path in your MCP config points to your `pipecat-context-hub` clone.
- **Empty results**: Run `uv run pipecat-context-hub refresh` to populate the index.
- **MCP not available**: Ensure you have VS Code 1.99+ and the GitHub Copilot extension installed. MCP support may need to be enabled in settings: `"chat.mcp.enabled": true`.
