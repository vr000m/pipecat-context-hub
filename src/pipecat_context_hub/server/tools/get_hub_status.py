"""get_hub_status MCP tool handler."""

from __future__ import annotations

from typing import Any

from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.shared.types import HubStatusOutput


async def handle_get_hub_status(
    arguments: dict[str, Any],
    index_store: IndexStore,
) -> str:
    """Return index health metadata: freshness, record counts, commit SHAs."""
    # Import here to use the same version string as the server.
    from pipecat_context_hub.server.main import _SERVER_VERSION

    stats: dict[str, Any] = index_store.get_index_stats()
    metadata: dict[str, str] = index_store.get_all_metadata()

    duration_str = metadata.get("last_refresh_duration_seconds")
    output = HubStatusOutput(
        server_version=_SERVER_VERSION,
        last_refresh_at=metadata.get("last_refresh_at"),
        last_refresh_duration_seconds=float(duration_str) if duration_str else None,
        total_records=stats["total"],
        counts_by_type=stats["counts_by_type"],
        commit_shas=stats.get("commit_shas", []),
        index_path=str(index_store.data_dir),
        framework_version=metadata.get("framework_version"),
    )
    return output.model_dump_json()
