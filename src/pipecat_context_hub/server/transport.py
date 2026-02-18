"""stdio transport adapter for the MCP server."""

from __future__ import annotations

import asyncio
import logging

from mcp import stdio_server
from mcp.server.lowlevel import Server

logger = logging.getLogger(__name__)


async def run_stdio(server: Server) -> None:
    """Run the MCP server over stdio transport."""
    logger.info("Starting MCP server on stdio transport")
    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)


def serve_stdio(server: Server) -> None:
    """Blocking entry point that runs the stdio server."""
    asyncio.run(run_stdio(server))
