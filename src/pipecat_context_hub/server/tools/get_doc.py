"""get_doc MCP tool handler."""

from __future__ import annotations

from typing import Any

from pipecat_context_hub.shared.interfaces import Retriever
from pipecat_context_hub.shared.types import GetDocInput


async def handle_get_doc(
    arguments: dict[str, Any],
    retriever: Retriever,
) -> str:
    """Parse input, call retriever, return serialized output."""
    inp = GetDocInput.model_validate(arguments)
    output = await retriever.get_doc(inp)
    return output.model_dump_json()
