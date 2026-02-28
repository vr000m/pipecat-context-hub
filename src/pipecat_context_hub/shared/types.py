"""Shared Pydantic models for the Pipecat Context Hub.

All data contracts used across services (ingestion, indexing, retrieval, MCP tools)
are defined here so parallel agents can code against stable types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Core indexing types
# ---------------------------------------------------------------------------


class ChunkedRecord(BaseModel):
    """A single indexed chunk of content (doc page section or code fragment)."""

    chunk_id: str = Field(description="Deterministic content-hash ID.")
    content: str = Field(description="The chunk text (markdown or code).")
    content_type: Literal["doc", "code", "readme", "source"] = Field(
        description="Whether this chunk came from documentation, code, a README, or framework source."
    )
    source_url: str = Field(description="Canonical URL for the source.")
    repo: str | None = Field(default=None, description="GitHub repo slug, e.g. 'pipecat-ai/pipecat'.")
    path: str = Field(description="File path within the repo or URL path for docs.")
    commit_sha: str | None = Field(default=None, description="Git commit SHA at index time.")
    indexed_at: datetime = Field(description="Timestamp when this record was indexed.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary extra metadata.")
    embedding: list[float] | None = Field(default=None, description="Embedding vector, if computed.")


class IndexQuery(BaseModel):
    """Query payload for the index store."""

    query_text: str = Field(description="Natural-language query string.")
    query_embedding: list[float] | None = Field(
        default=None, description="Pre-computed embedding for vector search."
    )
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Metadata filters (repo, content_type, path prefix, capability_tags).",
    )
    limit: int = Field(default=10, ge=1, le=100, description="Max results to return.")


class IndexResult(BaseModel):
    """A single result from the index store.

    Note: match_type is "vector" or "keyword" at the index layer.
    The retrieval layer (T5) may tag merged results as "hybrid" in
    RetrievalResult, but IndexReader only returns single-path results.
    """

    chunk: ChunkedRecord
    score: float = Field(description="Relevance score (higher is better).")
    match_type: Literal["vector", "keyword"] = Field(
        description="Which index path produced this result."
    )


# ---------------------------------------------------------------------------
# Taxonomy types
# ---------------------------------------------------------------------------


class CapabilityTag(BaseModel):
    """A capability tag extracted from an example."""

    name: str = Field(description="Tag name, e.g. 'rtvi', 'wake-word', 'tts'.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Extraction confidence.")
    source: Literal["directory", "readme", "code", "manual"] = Field(
        default="directory", description="How this tag was inferred."
    )


class TaxonomyEntry(BaseModel):
    """Metadata record for a single example in the taxonomy."""

    example_id: str = Field(description="Unique identifier for this example.")
    repo: str = Field(description="GitHub repo slug.")
    path: str = Field(description="Path to example directory within repo.")
    foundational_class: str | None = Field(
        default=None, description="Class name if from examples/foundational, else None."
    )
    capabilities: list[CapabilityTag] = Field(
        default_factory=list, description="Detected capability tags."
    )
    key_files: list[str] = Field(default_factory=list, description="Primary files in this example.")
    summary: str = Field(default="", description="Short auto-generated summary.")
    readme_content: str | None = Field(
        default=None, description="README contents if present."
    )
    commit_sha: str | None = Field(default=None, description="Git commit SHA at index time.")
    indexed_at: datetime | None = Field(
        default=None, description="Timestamp when this entry was last indexed."
    )


# ---------------------------------------------------------------------------
# Evidence reporting types
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    """Source citation for a retrieved fact."""

    source_url: str
    repo: str | None = None
    path: str
    commit_sha: str | None = None
    section: str | None = None
    line_range: tuple[int, int] | None = None
    indexed_at: datetime

    @field_validator("line_range", mode="before")
    @classmethod
    def coerce_line_range(cls, v: Any) -> tuple[int, int] | None:
        """Coerce list→tuple so JSON round-trips (which deserialize as list) work."""
        if v is None:
            return None
        if isinstance(v, list):
            return (v[0], v[1])
        if isinstance(v, tuple):
            return v
        msg = f"line_range must be a tuple, list, or None, got {type(v)}"
        raise TypeError(msg)


class KnownItem(BaseModel):
    """A source-grounded fact returned in evidence."""

    statement: str = Field(description="What is known, in natural language.")
    citations: list[Citation] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class UnknownItem(BaseModel):
    """An unresolved question surfaced in evidence."""

    question: str = Field(description="What is not known.")
    reason: str = Field(description="Why it could not be resolved.")
    suggested_queries: list[str] = Field(
        default_factory=list, description="Follow-up queries that might resolve this."
    )


class EvidenceReport(BaseModel):
    """Structured evidence report attached to every retrieval response."""

    known: list[KnownItem] = Field(default_factory=list)
    unknown: list[UnknownItem] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, description="Overall confidence score.")
    confidence_rationale: str = Field(default="", description="Brief explanation of confidence.")
    next_retrieval_queries: list[str] = Field(
        default_factory=list,
        description="Deterministic heuristic suggestions for follow-up retrieval.",
    )


# ---------------------------------------------------------------------------
# Retrieval result type
# ---------------------------------------------------------------------------


class RetrievalResult(BaseModel):
    """Top-level retrieval response wrapping ranked results and evidence."""

    results: list[IndexResult] = Field(default_factory=list)
    evidence: EvidenceReport
    query: str
    total_candidates: int = Field(default=0, description="Total matches before limit.")


# ---------------------------------------------------------------------------
# Ingestion result type
# ---------------------------------------------------------------------------


class IngestResult(BaseModel):
    """Summary of an ingestion run."""

    source: str = Field(description="Which source was ingested (URL, repo slug, etc.).")
    records_upserted: int = 0
    records_deleted: int = 0
    errors: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# MCP Tool I/O models — search_docs
# ---------------------------------------------------------------------------


class SearchDocsInput(BaseModel):
    """Input for the search_docs MCP tool."""

    query: str
    area: str | None = Field(default=None, description="Narrow to a docs area, e.g. 'api', 'guides'.")
    limit: int = Field(default=10, ge=1, le=50)


class DocHit(BaseModel):
    """A single docs search result."""

    doc_id: str
    title: str
    section: str | None = None
    snippet: str
    citation: Citation
    score: float


class SearchDocsOutput(BaseModel):
    """Output for the search_docs MCP tool."""

    hits: list[DocHit] = Field(default_factory=list)
    evidence: EvidenceReport


# ---------------------------------------------------------------------------
# MCP Tool I/O models — get_doc
# ---------------------------------------------------------------------------


class GetDocInput(BaseModel):
    """Input for the get_doc MCP tool."""

    doc_id: str
    section: str | None = None


class GetDocOutput(BaseModel):
    """Output for the get_doc MCP tool."""

    doc_id: str
    title: str
    content: str = Field(description="Full normalized markdown.")
    source_url: str
    indexed_at: datetime
    sections: list[str] = Field(default_factory=list, description="Available section headings.")
    evidence: EvidenceReport


# ---------------------------------------------------------------------------
# MCP Tool I/O models — search_examples
# ---------------------------------------------------------------------------


class SearchExamplesInput(BaseModel):
    """Input for the search_examples MCP tool."""

    query: str
    repo: str | None = None
    language: str | None = None
    tags: list[str] | None = None
    foundational_class: str | None = None
    execution_mode: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class ExampleHit(BaseModel):
    """A single example search result."""

    example_id: str
    summary: str
    foundational_class: str | None = None
    capability_tags: list[str] = Field(default_factory=list)
    key_files: list[str] = Field(default_factory=list)
    repo: str
    path: str
    commit_sha: str | None = None
    citation: Citation
    score: float


class SearchExamplesOutput(BaseModel):
    """Output for the search_examples MCP tool."""

    hits: list[ExampleHit] = Field(default_factory=list)
    evidence: EvidenceReport


# ---------------------------------------------------------------------------
# MCP Tool I/O models — get_example
# ---------------------------------------------------------------------------


class GetExampleInput(BaseModel):
    """Input for the get_example MCP tool."""

    example_id: str
    path: str | None = Field(default=None, description="Specific file within the example.")
    include_readme: bool = True


class ExampleFile(BaseModel):
    """A single file from an example package."""

    path: str
    content: str
    language: str | None = None


class GetExampleOutput(BaseModel):
    """Output for the get_example MCP tool."""

    example_id: str
    metadata: TaxonomyEntry
    files: list[ExampleFile] = Field(default_factory=list)
    citation: Citation
    detected_symbols: list[str] = Field(
        default_factory=list, description="Top-level symbols found in code files."
    )
    evidence: EvidenceReport


# ---------------------------------------------------------------------------
# MCP Tool I/O models — get_code_snippet
# ---------------------------------------------------------------------------


class GetCodeSnippetInput(BaseModel):
    """Input for the get_code_snippet MCP tool.

    Exactly one lookup mode must be provided:
    - ``symbol`` — search by symbol name (best-effort in v0)
    - ``intent`` — search by intent description (optionally scoped by ``path``
      and/or ``line_start``/``line_end``)
    - ``path`` + ``line_start`` (without ``intent``) — direct line-range lookup
    """

    symbol: str | None = None
    intent: str | None = None
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    framework: str | None = None
    example_ids: list[str] | None = None
    max_lines: int = Field(default=50, ge=1, le=500)

    @model_validator(mode="after")
    def validate_lookup_mode(self) -> GetCodeSnippetInput:
        """Enforce exactly one lookup mode.

        ``path`` and ``line_start`` may accompany ``intent`` as optional
        filters (scoping results to a specific file/range) without
        triggering the "multiple modes" error.
        """
        has_symbol = self.symbol is not None
        has_intent = self.intent is not None
        # path+line_start is its own mode only when intent is absent.
        has_path_range = (
            self.path is not None
            and self.line_start is not None
            and not has_intent
        )
        modes = [has_symbol, has_intent, has_path_range]
        if sum(modes) == 0:
            raise ValueError(
                "Exactly one of symbol, intent, or (path + line_start) must be provided."
            )
        if sum(modes) > 1:
            raise ValueError("Only one lookup mode may be set at a time.")
        return self


class CodeSnippet(BaseModel):
    """A single code snippet result."""

    content: str
    path: str
    line_start: int
    line_end: int
    language: str | None = None
    citation: Citation
    dependency_notes: list[str] = Field(
        default_factory=list, description="Imports or setup required by this snippet."
    )
    companion_snippets: list[str] = Field(
        default_factory=list, description="IDs of related snippets needed alongside this one."
    )
    interface_expectations: list[str] = Field(
        default_factory=list, description="Interfaces this snippet expects from callers."
    )


class GetCodeSnippetOutput(BaseModel):
    """Output for the get_code_snippet MCP tool."""

    snippets: list[CodeSnippet] = Field(default_factory=list)
    evidence: EvidenceReport


# ---------------------------------------------------------------------------
# MCP Tool I/O models — search_api
# ---------------------------------------------------------------------------


class GetHubStatusInput(BaseModel):
    """Input for the get_hub_status MCP tool (no parameters needed)."""


class HubStatusOutput(BaseModel):
    """Output for the get_hub_status MCP tool."""

    server_version: str = Field(description="Server version string.")
    last_refresh_at: str | None = Field(
        default=None, description="ISO timestamp of last successful refresh."
    )
    last_refresh_duration_seconds: float | None = Field(
        default=None, description="Duration of last refresh in seconds."
    )
    total_records: int = Field(default=0, description="Total indexed records.")
    counts_by_type: dict[str, int] = Field(
        default_factory=dict,
        description="Record counts by content type, e.g. {'doc': 3520, 'code': 1422, 'source': 5075}.",
    )
    commit_shas: list[str] = Field(
        default_factory=list, description="Distinct git commit SHAs in the index."
    )
    index_path: str = Field(default="", description="Path to the index data directory.")


class SearchApiInput(BaseModel):
    """Input for the search_api MCP tool."""

    query: str
    module: str | None = Field(default=None, description="Filter by module path prefix, e.g. 'pipecat.services'.")
    class_name: str | None = Field(default=None, description="Filter by class name, e.g. 'TTSService'.")
    chunk_type: str | None = Field(
        default=None,
        description="Filter by chunk type: 'module_overview', 'class_overview', 'method', or 'function'.",
    )
    is_dataclass: bool | None = Field(default=None, description="Filter for dataclass types only.")
    limit: int = Field(default=10, ge=1, le=50)


class ApiHit(BaseModel):
    """A single API source search result."""

    chunk_id: str
    module_path: str
    class_name: str | None = None
    method_name: str | None = None
    base_classes: list[str] = Field(default_factory=list)
    chunk_type: str
    snippet: str
    method_signature: str | None = None
    is_dataclass: bool = False
    imports: list[str] = Field(
        default_factory=list,
        description="Pipecat-internal imports in this module.",
    )
    citation: Citation
    score: float


class SearchApiOutput(BaseModel):
    """Output for the search_api MCP tool."""

    hits: list[ApiHit] = Field(default_factory=list)
    evidence: EvidenceReport
