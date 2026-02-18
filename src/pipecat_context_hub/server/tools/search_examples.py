"""search_examples MCP tool handler."""

from __future__ import annotations

from typing import Any

from pipecat_context_hub.shared.interfaces import Retriever
from pipecat_context_hub.shared.types import SearchExamplesInput


async def handle_search_examples(
    arguments: dict[str, Any],
    retriever: Retriever,
) -> str:
    """Parse input, call retriever, return serialized output."""
    inp = SearchExamplesInput.model_validate(arguments)
    output = await retriever.search_examples(inp)
    return output.model_dump_json()
