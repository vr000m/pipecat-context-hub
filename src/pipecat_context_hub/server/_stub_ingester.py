"""Stub Ingester for v0 — returns empty results.

Replaced by the real ingester in integration (T8).
"""

from __future__ import annotations

from pipecat_context_hub.shared.types import IngestResult


class StubIngester:
    """No-op ingester that satisfies the Ingester protocol."""

    async def ingest(self) -> IngestResult:
        return IngestResult(source="stub", records_upserted=0)

    async def refresh(self) -> IngestResult:
        return IngestResult(source="stub", records_upserted=0)
