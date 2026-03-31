"""MCP server entry point — tool registration and request dispatch."""

from __future__ import annotations

import logging
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server

from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.shared.interfaces import Retriever
from pipecat_context_hub.shared.types import (
    GetCodeSnippetInput,
    GetDocInput,
    GetExampleInput,
    GetHubStatusInput,
    SearchApiInput,
    SearchDocsInput,
    SearchExamplesInput,
)
from pipecat_context_hub.server.tools.get_code_snippet import handle_get_code_snippet
from pipecat_context_hub.server.tools.get_doc import handle_get_doc
from pipecat_context_hub.server.tools.get_example import handle_get_example
from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status
from pipecat_context_hub.server.tools.search_api import handle_search_api
from pipecat_context_hub.server.tools.search_docs import handle_search_docs
from pipecat_context_hub.server.tools.search_examples import handle_search_examples

logger = logging.getLogger(__name__)

_SERVER_VERSION = "0.0.13"

# Tool name → (description, input schema, handler)
_BASE_TOOLS: list[tuple[str, str, dict[str, Any]]] = [
    (
        "search_docs",
        "Search Pipecat documentation for conceptual questions, guides, configuration, and API "
        "references. Use for 'how do I...?' questions. Returns ranked doc hits with evidence. "
        "Use `area` to narrow by docs path prefix (e.g. 'guides', 'server/services'). "
        "For multiple topics, use ` + ` or ` & ` delimiters (e.g. 'TTS + STT').",
        SearchDocsInput.model_json_schema(),
    ),
    (
        "get_doc",
        "Retrieve a specific Pipecat documentation page by chunk ID or path. "
        "Use `doc_id` (from a search_docs result) or `path` (e.g. '/guides/learn/transports') for direct lookup. "
        "Use `section` to extract a specific heading; falls back to full document if not found.",
        GetDocInput.model_json_schema(),
    ),
    (
        "search_examples",
        "Find working Pipecat code examples by task, modality, or component. "
        "Use when the user needs runnable code patterns. "
        "Filter by `repo`, `tags` (capability tags), `foundational_class`, `language`, `domain` "
        "(backend/frontend/config/infra), or `execution_mode`. "
        "For multiple topics, use ` + ` or ` & ` delimiters (e.g. 'idle timeout + function calling').",
        SearchExamplesInput.model_json_schema(),
    ),
    (
        "get_example",
        "Retrieve full source files for a specific Pipecat example. "
        "Use after search_examples to get complete runnable code.",
        GetExampleInput.model_json_schema(),
    ),
    (
        "get_code_snippet",
        "Get a targeted code snippet by symbol name, intent, or file path + line range. "
        "Symbol lookups search framework source (class/method definitions); "
        "intent lookups search example code. "
        "Use `module` to scope symbol lookups (e.g. module='pipecat.runner.daily' with symbol='configure'). "
        "Use `class_name` to scope to a specific class (prefix match, e.g. 'DailyTransport' matches DailyTransportClient). "
        "Use `content_type='source'` with intent to search framework code instead of examples. "
        "For multiple topics, use ` + ` or ` & ` delimiters.",
        GetCodeSnippetInput.model_json_schema(),
    ),
    (
        "search_api",
        "Search Pipecat framework internals — class definitions, method signatures, constructors, "
        "base classes, and frame types. Use when you need implementation details, type information, "
        "or inheritance hierarchies. "
        "Filter by `module` (path prefix, e.g. 'pipecat.services'), `class_name` (prefix match, e.g. 'DailyTransport' matches DailyTransportClient), "
        "`chunk_type` ('module_overview', 'class_overview', 'method', 'function', 'type_definition'), or `is_dataclass`. "
        "For multiple topics, use ` + ` or ` & ` delimiters (e.g. 'BaseTransport + WebSocketTransport').",
        SearchApiInput.model_json_schema(),
    ),
]

_HUB_STATUS_TOOL: tuple[str, str, dict[str, Any]] = (
    "get_hub_status",
    "Get index health: last refresh time, record counts by type, indexed pipecat version, "
    "and commit SHAs. Use to check if the index is fresh before answering questions.",
    GetHubStatusInput.model_json_schema(),
)


_SERVER_INSTRUCTIONS = """\
You are using the Pipecat Context Hub — a retrieval server for Pipecat \
framework documentation, code examples, and API source.

**Always use these tools for Pipecat questions instead of reading .venv or \
source files directly.**

Tool selection guide:
- "How do I ...?" / conceptual questions → search_docs
- "Show me an example of ..." / working code → search_examples, then get_example
- Class constructors, method signatures, frame types → search_api
- Specific code span or symbol → get_code_snippet
- Retrieve a specific doc page → get_doc
- Index health, freshness, version info → get_hub_status

Multi-concept queries: use ` + ` or ` & ` to search for multiple concepts \
at once (e.g. "idle timeout + function calling + Gemini"). Each concept is \
searched independently and results are interleaved for balanced coverage.

When suggesting commands for Pipecat projects, always use `uv` as the \
package manager:
- Install dependencies: `uv sync` (not `pip install`)
- Run scripts: `uv run python bot.py` (not `python bot.py`)
- Add packages: `uv add <package>` (not `pip install <package>`)
- Run tools: `uv run pytest`, `uv run mypy`, etc.

Pipecat examples use `uv` and include a `pyproject.toml`. Do not suggest \
`pip`, `venv`, or `conda` unless the user explicitly requests them.\
"""


def create_server(retriever: Retriever, index_store: IndexStore | None = None) -> Server:
    """Create and configure the MCP server with all tool handlers.

    When *index_store* is provided the ``get_hub_status`` tool is registered;
    otherwise it is omitted so clients never discover an unusable tool.
    """
    # Build the tool list — only include get_hub_status when store is available
    tool_registry = list(_BASE_TOOLS)
    if index_store is not None:
        tool_registry.append(_HUB_STATUS_TOOL)

    server = Server(
        name="pipecat-context-hub",
        version=_SERVER_VERSION,
        instructions=_SERVER_INSTRUCTIONS,
    )

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=description,
                inputSchema=schema,
            )
            for name, description, schema in tool_registry
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        args = arguments or {}

        # get_hub_status has a different dispatch signature (needs index_store)
        if name == "get_hub_status" and index_store is not None:
            result_json = await handle_get_hub_status(args, index_store)
            return [types.TextContent(type="text", text=result_json)]

        handler_map: dict[str, Any] = {
            "search_docs": handle_search_docs,
            "get_doc": handle_get_doc,
            "search_examples": handle_search_examples,
            "get_example": handle_get_example,
            "get_code_snippet": handle_get_code_snippet,
            "search_api": handle_search_api,
        }
        handler = handler_map.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")

        result_json = await handler(args, retriever)
        return [types.TextContent(type="text", text=result_json)]

    return server
