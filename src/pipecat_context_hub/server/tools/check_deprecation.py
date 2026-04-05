"""check_deprecation MCP tool handler."""

from __future__ import annotations

from typing import Any

from pipecat_context_hub.shared.types import CheckDeprecationInput, CheckDeprecationOutput


async def handle_check_deprecation(
    arguments: dict[str, Any],
    deprecation_map: Any,
) -> str:
    """Check whether a symbol is deprecated in the pipecat framework.

    Args:
        arguments: Raw tool arguments from MCP call.
        deprecation_map: A ``DeprecationMap`` instance (from the retriever).
    """
    inp = CheckDeprecationInput.model_validate(arguments)

    if deprecation_map is None:
        output = CheckDeprecationOutput(
            deprecated=False,
            note="Deprecation map not available. Run `refresh` to build it.",
        )
        return output.model_dump_json()

    entry = deprecation_map.check(inp.symbol)

    if entry is None:
        output = CheckDeprecationOutput(deprecated=False)
    else:
        output = CheckDeprecationOutput(
            deprecated=True,
            replacement=entry.new_path,
            deprecated_in=entry.deprecated_in,
            removed_in=entry.removed_in,
            note=entry.note or None,
        )

    return output.model_dump_json()
