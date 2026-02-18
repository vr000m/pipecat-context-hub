"""get_example MCP tool handler."""

from __future__ import annotations

from typing import Any

from pipecat_context_hub.shared.interfaces import Retriever
from pipecat_context_hub.shared.types import GetExampleInput


async def handle_get_example(
    arguments: dict[str, Any],
    retriever: Retriever,
) -> str:
    """Parse input, call retriever, return serialized output."""
    inp = GetExampleInput.model_validate(arguments)
    output = await retriever.get_example(inp)
    return output.model_dump_json()
