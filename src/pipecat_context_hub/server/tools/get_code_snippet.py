"""get_code_snippet MCP tool handler."""

from __future__ import annotations

from typing import Any

from pipecat_context_hub.shared.interfaces import Retriever
from pipecat_context_hub.shared.types import GetCodeSnippetInput


async def handle_get_code_snippet(
    arguments: dict[str, Any],
    retriever: Retriever,
) -> str:
    """Parse input, call retriever, return serialized output."""
    inp = GetCodeSnippetInput.model_validate(arguments)
    output = await retriever.get_code_snippet(inp)
    return output.model_dump_json()
