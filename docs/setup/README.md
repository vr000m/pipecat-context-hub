# Client Setup Guides

Pipecat Context Hub is a local-first MCP server that provides fresh Pipecat documentation and code examples to your AI-powered IDE. It communicates over **stdio** — your client spawns the server process and talks to it via stdin/stdout.

## Supported Clients

| Client | Config File | Guide |
|--------|-------------|-------|
| [Claude Code](claude-code.md) | `.mcp.json` (project root) | [Setup guide](claude-code.md) |
| [Cursor](cursor.md) | `.cursor/mcp.json` | [Setup guide](cursor.md) |
| [VS Code](vscode.md) | `.vscode/mcp.json` | [Setup guide](vscode.md) |
| [Zed](zed.md) | `~/.config/zed/settings.json` | [Setup guide](zed.md) |

## Quick Start

All clients follow the same general steps:

1. **Clone** the repo and install dependencies
2. **Populate** the local index
3. **Add** the MCP server config to your client
4. **Verify** the server responds

```bash
# 1. Clone and install
git clone https://github.com/pipecat-ai/pipecat-context-hub.git
cd pipecat-context-hub
uv sync

# 2. Populate the local index
uv run pipecat-context-hub refresh

# 3. Add config — see the client-specific guide

# 4. Verify
uv run pipecat-context-hub serve --help
```

## How It Works

The MCP server runs as a subprocess of your IDE. When your AI assistant needs Pipecat context, it calls MCP tools exposed by the server. The server queries its local index (populated by `uv run pipecat-context-hub refresh`) and returns relevant documentation and code snippets.

```
IDE/Agent  ←stdio→  pipecat-context-hub serve  ←→  Local index (~/.pipecat-context-hub/)
```

No network requests are made during tool calls — all data is served from the local index.
