"""Hybrid retrieval service combining vector and keyword search.

Implements the Retriever protocol with five MCP tool methods:
search_docs, get_doc, search_examples, get_example, get_code_snippet.

Uses an IndexReader for data access, reranker for result merging, and
evidence module for citation/report assembly.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pipecat_context_hub.services.embedding import EmbeddingService

from pipecat_context_hub.services.retrieval.evidence import (
    assemble_evidence,
    build_citation,
    build_single_item_evidence,
)
from pipecat_context_hub.services.retrieval.rerank import rerank
from pipecat_context_hub.shared.interfaces import IndexReader
from pipecat_context_hub.shared.types import (
    Citation,
    CodeSnippet,
    DocHit,
    ExampleFile,
    ExampleHit,
    GetCodeSnippetInput,
    GetCodeSnippetOutput,
    GetDocInput,
    GetDocOutput,
    GetExampleInput,
    GetExampleOutput,
    IndexQuery,
    IndexResult,
    SearchDocsInput,
    SearchDocsOutput,
    SearchExamplesInput,
    SearchExamplesOutput,
    TaxonomyEntry,
)

logger = logging.getLogger(__name__)


class HybridRetriever:
    """Hybrid retrieval service implementing the Retriever protocol.

    Combines vector and keyword search via reciprocal rank fusion,
    applies code-intent heuristics, and assembles evidence reports.
    """

    def __init__(
        self,
        index_reader: IndexReader,
        embedding_service: EmbeddingService | None = None,
        rrf_k: int = 60,
        vector_weight: float = 0.6,
        keyword_weight: float = 0.4,
    ) -> None:
        self._index = index_reader
        self._embedding = embedding_service
        self._rrf_k = rrf_k
        self._vector_weight = vector_weight
        self._keyword_weight = keyword_weight
        logger.debug(
            "HybridRetriever init: rrf_k=%d vector_weight=%.2f keyword_weight=%.2f",
            rrf_k,
            vector_weight,
            keyword_weight,
        )

    async def _hybrid_search(
        self,
        query_text: str,
        filters: dict[str, Any],
        limit: int,
    ) -> list[IndexResult]:
        """Run both vector and keyword search, merge with reranking.

        The limit is applied to each individual search path. After RRF
        merge, the combined results are truncated to the requested limit.
        """
        # Compute query embedding for vector search if embedding service available
        query_embedding: list[float] | None = None
        if self._embedding is not None:
            query_embedding = await asyncio.to_thread(
                self._embedding.embed_query, query_text
            )

        query = IndexQuery(
            query_text=query_text,
            query_embedding=query_embedding,
            filters=filters,
            limit=limit,
        )

        logger.debug("Hybrid search: query=%r filters=%r limit=%d", query_text, filters, limit)

        vector_results = await self._index.vector_search(query)
        keyword_results = await self._index.keyword_search(query)

        logger.debug(
            "Raw results: vector=%d keyword=%d",
            len(vector_results),
            len(keyword_results),
        )

        if not vector_results and not keyword_results:
            return []

        reranked = rerank(
            vector_results=vector_results,
            keyword_results=keyword_results,
            query=query_text,
            rrf_k=self._rrf_k,
        )

        return reranked[:limit]

    # -----------------------------------------------------------------
    # search_docs
    # -----------------------------------------------------------------

    async def search_docs(self, input: SearchDocsInput) -> SearchDocsOutput:
        """Search documentation with hybrid retrieval."""
        filters: dict[str, Any] = {"content_type": "doc"}
        if input.area:
            filters["area"] = input.area

        results = await self._hybrid_search(input.query, filters, input.limit)
        evidence = assemble_evidence(input.query, results, filters)

        hits: list[DocHit] = []
        for r in results:
            citation = build_citation(r)
            hits.append(
                DocHit(
                    doc_id=r.chunk.chunk_id,
                    title=r.chunk.metadata.get("title", r.chunk.path),
                    section=r.chunk.metadata.get("section"),
                    snippet=r.chunk.content[:300],
                    citation=citation,
                    score=r.score,
                )
            )

        logger.debug("search_docs: query=%r hits=%d", input.query, len(hits))
        return SearchDocsOutput(hits=hits, evidence=evidence)

    # -----------------------------------------------------------------
    # get_doc
    # -----------------------------------------------------------------

    async def get_doc(self, input: GetDocInput) -> GetDocOutput:
        """Get a specific document by ID (direct lookup)."""
        filters: dict[str, Any] = {"chunk_id": input.doc_id}
        query = IndexQuery(
            query_text=input.doc_id,
            filters=filters,
            limit=1,
        )

        results = await self._index.keyword_search(query)
        result = results[0] if results else None
        evidence = build_single_item_evidence(result, input.doc_id, "document")

        if result is None:
            logger.debug("get_doc: doc_id=%r not found", input.doc_id)
            return GetDocOutput(
                doc_id=input.doc_id,
                title="Not Found",
                content="",
                source_url="",
                indexed_at=_epoch(),
                sections=[],
                evidence=evidence,
            )

        chunk = result.chunk
        sections: list[str] = chunk.metadata.get("sections", [])
        content = chunk.content

        # If a specific section was requested, try to extract it
        if input.section and sections:
            section_content = _extract_section(content, input.section)
            if section_content:
                content = section_content

        logger.debug("get_doc: doc_id=%r found, sections=%d", input.doc_id, len(sections))
        return GetDocOutput(
            doc_id=chunk.chunk_id,
            title=chunk.metadata.get("title", chunk.path),
            content=content,
            source_url=chunk.source_url,
            indexed_at=chunk.indexed_at,
            sections=sections,
            evidence=evidence,
        )

    # -----------------------------------------------------------------
    # search_examples
    # -----------------------------------------------------------------

    async def search_examples(self, input: SearchExamplesInput) -> SearchExamplesOutput:
        """Search code examples with hybrid retrieval."""
        filters: dict[str, Any] = {"content_type": "code"}
        if input.repo:
            filters["repo"] = input.repo
        if input.language:
            filters["language"] = input.language
        if input.tags:
            filters["capability_tags"] = input.tags
        if input.foundational_class:
            filters["foundational_class"] = input.foundational_class
        if input.execution_mode:
            filters["execution_mode"] = input.execution_mode

        results = await self._hybrid_search(input.query, filters, input.limit)
        evidence = assemble_evidence(input.query, results, filters)

        hits: list[ExampleHit] = []
        for r in results:
            citation = build_citation(r)
            cap_tags: list[str] = r.chunk.metadata.get("capability_tags", [])
            key_files: list[str] = r.chunk.metadata.get("key_files", [])
            hits.append(
                ExampleHit(
                    example_id=r.chunk.chunk_id,
                    summary=r.chunk.content[:200],
                    foundational_class=r.chunk.metadata.get("foundational_class"),
                    capability_tags=cap_tags,
                    key_files=key_files,
                    repo=r.chunk.repo or "",
                    path=r.chunk.path,
                    commit_sha=r.chunk.commit_sha,
                    citation=citation,
                    score=r.score,
                )
            )

        logger.debug("search_examples: query=%r hits=%d", input.query, len(hits))
        return SearchExamplesOutput(hits=hits, evidence=evidence)

    # -----------------------------------------------------------------
    # get_example
    # -----------------------------------------------------------------

    async def get_example(self, input: GetExampleInput) -> GetExampleOutput:
        """Get a specific example by ID (direct lookup)."""
        filters: dict[str, Any] = {"chunk_id": input.example_id}
        query = IndexQuery(
            query_text=input.example_id,
            filters=filters,
            limit=1,
        )

        results = await self._index.keyword_search(query)
        result = results[0] if results else None
        evidence = build_single_item_evidence(result, input.example_id, "example")

        if result is None:
            logger.debug("get_example: example_id=%r not found", input.example_id)
            return GetExampleOutput(
                example_id=input.example_id,
                metadata=_empty_taxonomy(input.example_id),
                files=[],
                citation=_empty_citation(),
                detected_symbols=[],
                evidence=evidence,
            )

        chunk = result.chunk
        citation = build_citation(result)

        # Build taxonomy entry from chunk metadata
        cap_tags = chunk.metadata.get("capability_tags", [])
        key_files: list[str] = chunk.metadata.get("key_files", [])
        taxonomy = TaxonomyEntry(
            example_id=chunk.chunk_id,
            repo=chunk.repo or "",
            path=chunk.path,
            foundational_class=chunk.metadata.get("foundational_class"),
            capabilities=[],  # Raw tag names are in capability_tags
            key_files=key_files,
            summary=chunk.content[:200],
            readme_content=chunk.metadata.get("readme_content")
            if input.include_readme
            else None,
            commit_sha=chunk.commit_sha,
            indexed_at=chunk.indexed_at,
        )

        # Build file list
        files: list[ExampleFile] = []
        if input.path:
            # Specific file requested
            files.append(
                ExampleFile(
                    path=input.path,
                    content=chunk.content,
                    language=chunk.metadata.get("language"),
                )
            )
        else:
            files.append(
                ExampleFile(
                    path=chunk.path,
                    content=chunk.content,
                    language=chunk.metadata.get("language"),
                )
            )

        symbols: list[str] = chunk.metadata.get("detected_symbols", [])

        logger.debug(
            "get_example: example_id=%r found, files=%d symbols=%d",
            input.example_id,
            len(files),
            len(symbols),
        )
        return GetExampleOutput(
            example_id=chunk.chunk_id,
            metadata=taxonomy,
            files=files,
            citation=citation,
            detected_symbols=symbols,
            evidence=evidence,
        )

    # -----------------------------------------------------------------
    # get_code_snippet
    # -----------------------------------------------------------------

    async def get_code_snippet(self, input: GetCodeSnippetInput) -> GetCodeSnippetOutput:
        """Get code snippets by symbol, intent, or path+line_start."""
        filters: dict[str, Any] = {"content_type": "code"}
        if input.framework:
            filters["framework"] = input.framework
        if input.example_ids:
            filters["example_ids"] = input.example_ids

        # Determine query text based on lookup mode
        if input.symbol:
            query_text = input.symbol
            filters["symbol"] = input.symbol
        elif input.intent:
            query_text = input.intent
        elif input.path is not None and input.line_start is not None:
            query_text = input.path
            filters["path"] = input.path
            filters["line_start"] = input.line_start
            if input.line_end:
                filters["line_end"] = input.line_end
        else:
            # Should not happen due to model_validator, but be safe
            query_text = ""

        _DEFAULT_SNIPPET_CANDIDATES = 5
        results = await self._hybrid_search(query_text, filters, _DEFAULT_SNIPPET_CANDIDATES)
        evidence = assemble_evidence(query_text, results, filters)

        snippets: list[CodeSnippet] = []
        for r in results:
            citation = build_citation(r)
            content = r.chunk.content
            # Respect max_lines
            lines = content.splitlines()
            if len(lines) > input.max_lines:
                content = "\n".join(lines[: input.max_lines])

            line_start = r.chunk.metadata.get("line_start", 1)
            line_end = r.chunk.metadata.get("line_end", line_start + len(lines) - 1)

            snippets.append(
                CodeSnippet(
                    content=content,
                    path=r.chunk.path,
                    line_start=line_start,
                    line_end=line_end,
                    language=r.chunk.metadata.get("language"),
                    citation=citation,
                    dependency_notes=r.chunk.metadata.get("dependency_notes", []),
                    companion_snippets=r.chunk.metadata.get("companion_snippets", []),
                    interface_expectations=r.chunk.metadata.get("interface_expectations", []),
                )
            )

        logger.debug("get_code_snippet: query=%r snippets=%d", query_text, len(snippets))
        return GetCodeSnippetOutput(snippets=snippets, evidence=evidence)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _epoch() -> datetime:
    """Return UTC epoch datetime for placeholder values."""
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _empty_citation() -> Citation:
    """Return a placeholder citation for not-found items."""
    return Citation(
        source_url="",
        repo=None,
        path="",
        commit_sha=None,
        indexed_at=_epoch(),
    )


def _empty_taxonomy(example_id: str) -> TaxonomyEntry:
    """Return a placeholder taxonomy entry for not-found items."""
    return TaxonomyEntry(
        example_id=example_id,
        repo="",
        path="",
    )


def _extract_section(content: str, section_name: str) -> str | None:
    """Try to extract a named section from markdown content.

    Looks for a heading matching section_name and returns content until
    the next heading of equal or higher level.
    """
    lines = content.splitlines()
    start_idx: int | None = None
    start_level = 0

    for i, line in enumerate(lines):
        stripped = line.lstrip("#")
        level = len(line) - len(stripped)
        heading = stripped.strip()
        if level > 0 and heading.lower() == section_name.lower():
            start_idx = i
            start_level = level
            continue
        if start_idx is not None and level > 0 and level <= start_level:
            return "\n".join(lines[start_idx:i])

    if start_idx is not None:
        return "\n".join(lines[start_idx:])

    return None
