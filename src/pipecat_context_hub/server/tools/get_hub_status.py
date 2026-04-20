"""get_hub_status MCP tool handler."""

from __future__ import annotations

from typing import Any

from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.shared.types import HubStatusOutput, RerankerStatus


async def handle_get_hub_status(
    arguments: dict[str, Any],
    index_store: IndexStore,
    reranker_status: RerankerStatus | None = None,
) -> str:
    """Return index health metadata: freshness, record counts, commit SHAs.

    *reranker_status* reflects live runtime state (not configured intent),
    built by the CLI after the reranker is constructed or skipped. When
    omitted, reranker fields report as disabled.
    """
    # Import here to use the same version string as the server.
    from pipecat_context_hub.server.main import _SERVER_VERSION

    stats: dict[str, Any] = index_store.get_index_stats()
    metadata: dict[str, str] = index_store.get_all_metadata()

    duration_str = metadata.get("last_refresh_duration_seconds")

    if reranker_status is None:
        # Caller didn't wire a provider — we don't actually know why
        # reranking is off, so leave disabled_reason unset.
        reranker_status = RerankerStatus(enabled=False)

    output = HubStatusOutput(
        server_version=_SERVER_VERSION,
        last_refresh_at=metadata.get("last_refresh_at"),
        last_refresh_duration_seconds=float(duration_str) if duration_str else None,
        total_records=stats["total"],
        counts_by_type=stats["counts_by_type"],
        commit_shas=stats.get("commit_shas", []),
        index_path=str(index_store.data_dir),
        framework_version=metadata.get("framework_version"),
        reranker_enabled=reranker_status.enabled,
        reranker_model=reranker_status.model,
        reranker_configured_model=reranker_status.configured_model,
        reranker_disabled_reason=reranker_status.disabled_reason,
    )
    return output.model_dump_json()
