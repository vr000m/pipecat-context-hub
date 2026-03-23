"""Hybrid retrieval service combining vector and keyword search.

Implements the Retriever protocol with six MCP tool methods:
search_docs, get_doc, search_examples, get_example, get_code_snippet,
search_api.

Uses an IndexReader for data access, reranker for result merging, and
evidence module for citation/report assembly.
"""

from __future__ import annotations

import asyncio
import json
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
from pipecat_context_hub.services.retrieval.decompose import decompose_query
from pipecat_context_hub.services.retrieval.rerank import rerank
from pipecat_context_hub.shared.interfaces import IndexReader
from pipecat_context_hub.shared.types import (
    ApiHit,
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
    SearchApiInput,
    SearchApiOutput,
    SearchDocsInput,
    SearchDocsOutput,
    SearchExamplesInput,
    SearchExamplesOutput,
    TaxonomyEntry,
)

logger = logging.getLogger(__name__)

# Number of candidate results to fetch for code snippet lookups.
_DEFAULT_SNIPPET_CANDIDATES = 5


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

        If the query contains multiple concepts (delimited by `` + ``
        or `` & ``), runs per-concept searches and interleaves results
        for balanced coverage.
        """
        concepts = decompose_query(query_text)
        if concepts is not None:
            return await self._multi_concept_search(concepts, filters, limit)
        return await self._single_concept_search(query_text, filters, limit)

    async def _single_concept_search(
        self,
        query_text: str,
        filters: dict[str, Any],
        limit: int,
    ) -> list[IndexResult]:
        """Run vector + keyword search for a single query, merge with RRF."""
        query_embedding: list[float] | None = None
        if self._embedding is not None:
            query_embedding = await asyncio.to_thread(self._embedding.embed_query, query_text)

        query = IndexQuery(
            query_text=query_text,
            query_embedding=query_embedding,
            filters=filters,
            limit=limit,
        )

        logger.debug(
            "Single-concept search: query=%r filters=%r limit=%d", query_text, filters, limit
        )

        vector_results, keyword_results = await asyncio.gather(
            self._index.vector_search(query),
            self._index.keyword_search(query),
        )

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

    async def _multi_concept_search(
        self,
        concepts: list[str],
        filters: dict[str, Any],
        limit: int,
    ) -> list[IndexResult]:
        """Run per-concept searches and interleave for balanced coverage."""
        n = len(concepts)

        # When the requested limit is smaller than the concept count,
        # per-concept searches would over-fetch.  Fall back to a single
        # search using the full (joined) query.
        if limit < n:
            return await self._single_concept_search(" ".join(concepts), filters, limit)

        per_concept = -(-limit // n)  # ceiling division

        logger.debug(
            "Multi-concept search: concepts=%r per_concept=%d limit=%d",
            concepts,
            per_concept,
            limit,
        )

        concept_results = await asyncio.gather(
            *(self._single_concept_search(c, filters, per_concept) for c in concepts)
        )

        # Round-robin interleave with deduplication
        merged: list[IndexResult] = []
        seen_ids: set[str] = set()
        max_len = max((len(r) for r in concept_results), default=0)

        for i in range(max_len):
            for results in concept_results:
                if i < len(results):
                    cid = results[i].chunk.chunk_id
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        merged.append(results[i])
                        if len(merged) >= limit:
                            return merged

        return merged[:limit]

    # -----------------------------------------------------------------
    # search_docs
    # -----------------------------------------------------------------

    async def search_docs(self, input: SearchDocsInput) -> SearchDocsOutput:
        """Search documentation with hybrid retrieval."""
        filters: dict[str, Any] = {"content_type": "doc"}
        if input.area:
            # Doc paths are stored with a leading slash (e.g. "/guides/...")
            # from urlparse(source_url).path.  Normalize the user-supplied
            # area so the prefix filter matches.
            area = input.area if input.area.startswith("/") else f"/{input.area}"
            filters["path"] = area

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
        key_files: list[str] = chunk.metadata.get("key_files", [])
        taxonomy = TaxonomyEntry(
            example_id=chunk.chunk_id,
            repo=chunk.repo or "",
            path=chunk.path,
            foundational_class=chunk.metadata.get("foundational_class"),
            capabilities=[],  # Raw tag names are in capability_tags
            key_files=key_files,
            summary=chunk.content[:200],
            readme_content=chunk.metadata.get("readme_content") if input.include_readme else None,
            commit_sha=chunk.commit_sha,
            indexed_at=chunk.indexed_at,
        )

        # Build file list — always use the chunk's actual path to avoid
        # mislabelling content when input.path doesn't match the chunk.
        files: list[ExampleFile] = []
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
        filters: dict[str, Any] = {}
        results: list[IndexResult] = []

        # Determine query text and content_type based on lookup mode.
        # Symbol lookups target framework source (content_type="source")
        # where class/method definitions are AST-indexed.
        # Intent and path lookups target example code (content_type="code").
        if input.symbol:
            query_text = input.symbol
            base_filters: dict[str, Any] = {
                "content_type": input.content_type or "source",
            }
            if input.path is not None:
                base_filters["path"] = input.path
            if input.module:
                base_filters["module_path"] = input.module
            if input.class_name:
                base_filters["class_name"] = input.class_name

            # Filter cascade: class_name → method_name → unstructured fallback.
            # Tries exact metadata filters first for precise symbol matches,
            # then relaxes progressively.  When the caller already supplied
            # class_name, skip the first cascade step (it would be redundant)
            # and the caller's class_name carries through all subsequent steps
            # (scoping method and unstructured searches to that class).
            cascade_steps: list[dict[str, str]] = []
            if not input.class_name:
                cascade_steps.append({"class_name": input.symbol})
            cascade_steps.append({"method_name": input.symbol})
            cascade_steps.append({})

            cascade_filters: dict[str, Any] = base_filters
            for extra_filter in cascade_steps:
                cascade_filters = {**base_filters, **extra_filter}
                results = await self._hybrid_search(
                    query_text, cascade_filters, _DEFAULT_SNIPPET_CANDIDATES
                )
                if results:
                    break
            filters = cascade_filters
        elif input.intent:
            query_text = input.intent
            filters["content_type"] = input.content_type or "code"
            # path narrows intent search to a specific file
            if input.path is not None:
                filters["path"] = input.path
        elif input.path is not None and input.line_start is not None:
            query_text = input.path
            filters["content_type"] = input.content_type or "code"
            filters["path"] = input.path
            # line_start/line_end are applied as post-filters below, not
            # passed to index backends which don't support numeric ranges.
        else:
            # Should not happen due to model_validator, but be safe
            query_text = ""

        if not input.symbol:
            results = await self._hybrid_search(query_text, filters, _DEFAULT_SNIPPET_CANDIDATES)
        evidence = assemble_evidence(query_text, results, filters)

        snippets: list[CodeSnippet] = []
        for r in results:
            citation = build_citation(r)
            content = r.chunk.content
            all_lines = content.splitlines()

            # Derive line range from stored metadata
            chunk_line_start: int = r.chunk.metadata.get("line_start", 1)
            chunk_line_end: int = r.chunk.metadata.get(
                "line_end", chunk_line_start + len(all_lines) - 1
            )

            # When path+line_start lookup was requested, extract the
            # requested sub-range from within this chunk.
            line_sliced = False
            if input.path is not None and input.line_start is not None:
                req_start = input.line_start
                req_end = input.line_end or (req_start + input.max_lines - 1)
                # Skip this chunk entirely if the requested range
                # does not overlap with the chunk's line range.
                if req_end < chunk_line_start or req_start > chunk_line_end:
                    continue
                # Compute offsets relative to chunk start
                offset_start = max(0, req_start - chunk_line_start)
                offset_end = min(len(all_lines), req_end - chunk_line_start + 1)
                all_lines = all_lines[offset_start:offset_end]
                chunk_line_start = max(req_start, chunk_line_start)
                chunk_line_end = chunk_line_start + len(all_lines) - 1
                content = "\n".join(all_lines)
                line_sliced = True

            # Respect max_lines (enrichment still applies — the metadata
            # describes the full method, helping agents decide whether to
            # re-fetch with a larger max_lines).
            if len(all_lines) > input.max_lines:
                all_lines = all_lines[: input.max_lines]
                content = "\n".join(all_lines)
                chunk_line_end = chunk_line_start + len(all_lines) - 1

            # -- Enrich from call-graph metadata --
            # Skip enrichment for:
            # - path+line_start slicing: arbitrary line range, metadata may
            #   not apply to the requested sub-range.
            # - module_overview chunks: imports include stdlib/third-party,
            #   not just pipecat-internal.
            # Keep enrichment for max_lines truncation: metadata describes
            # the whole method and helps agents decide if more context is needed.
            chunk_type = r.chunk.metadata.get("chunk_type", "")
            if line_sliced or chunk_type == "module_overview":
                imports_raw: list[str] = []
                companion: list[str] = []
                expectations: list[str] = []
            else:
                imports_raw = _parse_metadata_list(r.chunk.metadata, "imports")
                calls_raw = _parse_metadata_list(r.chunk.metadata, "calls")
                class_name = r.chunk.metadata.get("class_name", "")
                companion = [
                    f"{class_name}.{c}"
                    if class_name and "." not in c and not c.startswith("super()")
                    else c
                    for c in calls_raw
                ]

                yields_raw = _parse_metadata_list(r.chunk.metadata, "yields")
                base_classes = _parse_metadata_list(r.chunk.metadata, "base_classes")
                expectations = []
                if yields_raw:
                    expectations.append(f"Yields: {', '.join(yields_raw)}")
                if base_classes:
                    expectations.append(f"Implements: {', '.join(base_classes)}")

            snippets.append(
                CodeSnippet(
                    content=content,
                    path=r.chunk.path,
                    line_start=chunk_line_start,
                    line_end=chunk_line_end,
                    language=r.chunk.metadata.get("language"),
                    citation=citation,
                    dependency_notes=imports_raw,
                    companion_snippets=companion,
                    interface_expectations=expectations,
                )
            )

        logger.debug("get_code_snippet: query=%r snippets=%d", query_text, len(snippets))
        return GetCodeSnippetOutput(snippets=snippets, evidence=evidence)

    # -----------------------------------------------------------------
    # search_api
    # -----------------------------------------------------------------

    async def search_api(self, input: SearchApiInput) -> SearchApiOutput:
        """Search framework API source with hybrid retrieval."""
        filters: dict[str, Any] = {"content_type": "source"}
        if input.module:
            filters["module_path"] = input.module
        if input.class_name:
            filters["class_name"] = input.class_name
        if input.chunk_type:
            filters["chunk_type"] = input.chunk_type
        if input.is_dataclass is not None:
            filters["is_dataclass"] = input.is_dataclass
        if input.yields:
            filters["yields"] = input.yields
        if input.calls:
            filters["calls"] = input.calls

        results = await self._hybrid_search(input.query, filters, input.limit)
        evidence = assemble_evidence(input.query, results, filters)

        hits: list[ApiHit] = []
        for r in results:
            citation = build_citation(r)
            base_classes_raw = _parse_metadata_list(r.chunk.metadata, "base_classes")
            imports_raw = _parse_metadata_list(r.chunk.metadata, "imports")
            yields_raw = _parse_metadata_list(r.chunk.metadata, "yields")
            calls_raw = _parse_metadata_list(r.chunk.metadata, "calls")
            hits.append(
                ApiHit(
                    chunk_id=r.chunk.chunk_id,
                    module_path=r.chunk.metadata.get("module_path", ""),
                    class_name=r.chunk.metadata.get("class_name") or None,
                    method_name=r.chunk.metadata.get("method_name") or None,
                    base_classes=base_classes_raw,
                    chunk_type=r.chunk.metadata.get("chunk_type", "unknown"),
                    snippet=r.chunk.content[:500],
                    method_signature=r.chunk.metadata.get("method_signature") or None,
                    is_dataclass=bool(r.chunk.metadata.get("is_dataclass", False)),
                    imports=imports_raw,
                    yields=yields_raw,
                    calls=calls_raw,
                    citation=citation,
                    score=r.score,
                )
            )

        logger.debug("search_api: query=%r hits=%d", input.query, len(hits))
        return SearchApiOutput(hits=hits, evidence=evidence)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_metadata_list(metadata: dict[str, Any], key: str) -> list[str]:
    """Extract a list-of-strings field from chunk metadata.

    Handles three storage formats:
    - Native list (in-memory / mock): returned as-is.
    - JSON-encoded string (ChromaDB): decoded via json.loads.
    - Malformed / missing: returns [].
    """
    raw = metadata.get(key, [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return raw


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
