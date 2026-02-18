"""MCP server entry point — tool registration and request dispatch."""

from __future__ import annotations

import logging
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server

from pipecat_context_hub.shared.interfaces import Retriever
from pipecat_context_hub.shared.types import (
    GetCodeSnippetInput,
    GetDocInput,
    GetExampleInput,
    SearchDocsInput,
    SearchExamplesInput,
)
from pipecat_context_hub.server.tools.get_code_snippet import handle_get_code_snippet
from pipecat_context_hub.server.tools.get_doc import handle_get_doc
from pipecat_context_hub.server.tools.get_example import handle_get_example
from pipecat_context_hub.server.tools.search_docs import handle_search_docs
from pipecat_context_hub.server.tools.search_examples import handle_search_examples

logger = logging.getLogger(__name__)

# Tool name → (description, input schema, handler)
_TOOL_REGISTRY: list[tuple[str, str, dict[str, Any]]] = [
    (
        "search_docs",
        "Search Pipecat documentation. Returns ranked doc hits with evidence.",
        SearchDocsInput.model_json_schema(),
    ),
    (
        "get_doc",
        "Retrieve a specific Pipecat documentation page by ID.",
        GetDocInput.model_json_schema(),
    ),
    (
        "search_examples",
        "Search Pipecat code examples. Filter by repo, tags, or foundational class.",
        SearchExamplesInput.model_json_schema(),
    ),
    (
        "get_example",
        "Retrieve a specific Pipecat example by ID, including source files.",
        GetExampleInput.model_json_schema(),
    ),
    (
        "get_code_snippet",
        "Get a code snippet by symbol name, intent description, or file path + line range.",
        GetCodeSnippetInput.model_json_schema(),
    ),
]


def create_server(retriever: Retriever) -> Server:
    """Create and configure the MCP server with all tool handlers."""
    server = Server(name="pipecat-context-hub", version="0.1.0")

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=description,
                inputSchema=schema,
            )
            for name, description, schema in _TOOL_REGISTRY
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[types.TextContent]:
        args = arguments or {}
        handler_map: dict[str, Any] = {
            "search_docs": handle_search_docs,
            "get_doc": handle_get_doc,
            "search_examples": handle_search_examples,
            "get_example": handle_get_example,
            "get_code_snippet": handle_get_code_snippet,
        }
        handler = handler_map.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")

        result_json: str = await handler(args, retriever)
        return [types.TextContent(type="text", text=result_json)]

    return server
