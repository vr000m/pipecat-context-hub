"""search_docs MCP tool handler."""

from __future__ import annotations

from typing import Any

from pipecat_context_hub.shared.interfaces import Retriever
from pipecat_context_hub.shared.types import SearchDocsInput


async def handle_search_docs(
    arguments: dict[str, Any],
    retriever: Retriever,
) -> str:
    """Parse input, call retriever, return serialized output."""
    inp = SearchDocsInput.model_validate(arguments)
    output = await retriever.search_docs(inp)
    return output.model_dump_json()
