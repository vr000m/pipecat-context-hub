"""Service interface protocols for the Pipecat Context Hub.

Parallel agents code against these protocols and use mocks in unit tests.
Concrete implementations are wired together during integration (T8).
"""

from __future__ import annotations

from typing import Protocol

from pipecat_context_hub.shared.types import (
    ChunkedRecord,
    GetCodeSnippetInput,
    GetCodeSnippetOutput,
    GetDocInput,
    GetDocOutput,
    GetExampleInput,
    GetExampleOutput,
    IndexQuery,
    IndexResult,
    IngestResult,
    SearchDocsInput,
    SearchDocsOutput,
    SearchExamplesInput,
    SearchExamplesOutput,
)


# ---------------------------------------------------------------------------
# Index service protocols
# ---------------------------------------------------------------------------


class IndexWriter(Protocol):
    """Writes chunked records into the index store."""

    async def upsert(self, records: list[ChunkedRecord]) -> int:
        """Insert or update records. Returns count of records written."""
        ...

    async def delete_by_source(self, source_url: str) -> int:
        """Delete all records from a given source. Returns count deleted."""
        ...


class IndexReader(Protocol):
    """Reads from the index store."""

    async def vector_search(self, query: IndexQuery) -> list[IndexResult]:
        """Return results ranked by embedding similarity."""
        ...

    async def keyword_search(self, query: IndexQuery) -> list[IndexResult]:
        """Return results ranked by FTS5 keyword relevance."""
        ...


# ---------------------------------------------------------------------------
# Retrieval service protocol
# ---------------------------------------------------------------------------


class Retriever(Protocol):
    """High-level retrieval interface consumed by MCP tool handlers."""

    async def search_docs(self, input: SearchDocsInput) -> SearchDocsOutput: ...

    async def get_doc(self, input: GetDocInput) -> GetDocOutput: ...

    async def search_examples(self, input: SearchExamplesInput) -> SearchExamplesOutput: ...

    async def get_example(self, input: GetExampleInput) -> GetExampleOutput: ...

    async def get_code_snippet(self, input: GetCodeSnippetInput) -> GetCodeSnippetOutput: ...


# ---------------------------------------------------------------------------
# Ingestion service protocol
# ---------------------------------------------------------------------------


class Ingester(Protocol):
    """Ingestion interface for crawling a source and writing to the index."""

    async def ingest(self) -> IngestResult:
        """Run a full ingestion pass."""
        ...
