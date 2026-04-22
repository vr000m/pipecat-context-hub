"""Shared Pydantic models for the Pipecat Context Hub.

All data contracts used across services (ingestion, indexing, retrieval, MCP tools)
are defined here so parallel agents can code against stable types.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator


class IdleTracker:
    """Tracks the time since the last MCP tool dispatch.

    Lives in ``shared/`` because both ``server/main.py`` (the call_tool
    dispatcher, producer) and ``server/transport.py`` (the idle
    watchdog, consumer) reference it — the same neutral-layer pattern
    used for ``Retriever``, ``IndexStore``, ``RerankerStatus``.

    Single-event-loop semantics: ``touch()`` and ``seconds_since_last()``
    are called from the same asyncio loop, so no lock is needed; float
    read/write is atomic under the GIL. ``time.monotonic`` is used so
    wall-clock changes can't trigger spurious idle fires.
    """

    def __init__(self) -> None:
        self._last = time.monotonic()

    def touch(self) -> None:
        self._last = time.monotonic()

    def seconds_since_last(self) -> float:
        return time.monotonic() - self._last


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
    repo: str | None = Field(
        default=None, description="GitHub repo slug, e.g. 'pipecat-ai/pipecat'."
    )
    path: str = Field(description="File path within the repo or URL path for docs.")
    commit_sha: str | None = Field(default=None, description="Git commit SHA at index time.")
    indexed_at: datetime = Field(description="Timestamp when this record was indexed.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary extra metadata.")
    embedding: list[float] | None = Field(
        default=None, description="Embedding vector, if computed."
    )


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
    filter_only: bool = Field(
        default=False,
        description="When True, bypass text search (FTS MATCH) and return results by metadata filters only.",
    )
    limit: int = Field(default=10, ge=1, le=500, description="Max results to return.")


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
    readme_content: str | None = Field(default=None, description="README contents if present.")
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
    low_confidence: bool = Field(
        default=False,
        description="True when confidence < 0.3, signaling context may be insufficient.",
    )
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

    query: str = Field(max_length=1000)
    area: str | None = Field(
        default=None,
        max_length=256,
        description="Narrow to a docs area by path prefix, e.g. 'api', 'guides', 'server/services'.",
    )
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

    doc_id: str | None = Field(
        default=None,
        max_length=256,
        description="Chunk ID from a previous search_docs result. Either doc_id or path must be provided.",
    )
    path: str | None = Field(
        default=None,
        max_length=512,
        description="Doc path prefix (e.g. '/guides/learn/transports'). Looks up by path when doc_id is not known.",
    )
    section: str | None = Field(
        default=None,
        max_length=256,
        description="Extract a specific section by heading. Falls back to full document if not found.",
    )

    @model_validator(mode="after")
    def _require_doc_id_or_path(self) -> "GetDocInput":
        has_doc_id = self.doc_id is not None and self.doc_id.strip()
        has_path = self.path is not None and self.path.strip()
        if not has_doc_id and not has_path:
            raise ValueError("Either doc_id or path must be provided.")
        return self


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

    query: str = Field(max_length=1000)
    repo: str | None = Field(default=None, max_length=256)
    language: str | None = Field(
        default=None,
        max_length=64,
        description="Filter by programming language (e.g. 'python', 'typescript').",
    )
    domain: str | None = Field(
        default=None,
        max_length=64,
        description="Filter by domain: 'backend' (Python pipeline/bot code), 'frontend' (JS/TS client code), 'config' (YAML/TOML/JSON), 'infra' (Docker/CI).",
    )
    tags: list[Annotated[str, StringConstraints(max_length=64)]] | None = Field(
        default=None,
        max_length=20,
    )
    foundational_class: str | None = Field(default=None, max_length=256)
    execution_mode: str | None = Field(default=None, max_length=64)
    pipecat_version: str | None = Field(
        default=None,
        max_length=64,
        description="User's pipecat-ai version (e.g. '0.0.95'). When provided, results are scored for compatibility and annotated with version_compatibility.",
    )
    version_filter: Literal["compatible_only"] | None = Field(
        default=None,
        description="Set to 'compatible_only' to exclude results targeting versions newer than the user's. Requires pipecat_version.",
    )
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def _version_filter_requires_version(self) -> "SearchExamplesInput":
        if self.version_filter and not self.pipecat_version:
            raise ValueError("version_filter requires pipecat_version to be set.")
        return self


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
    pipecat_version_pin: str | None = None
    version_compatibility: Literal["compatible", "newer_required", "older_targeted", "unknown"] | None = None
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

    example_id: str = Field(max_length=256)
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

    symbol: str | None = Field(default=None, max_length=256)
    intent: str | None = Field(default=None, max_length=1000)
    path: str | None = Field(default=None, max_length=512)
    line_start: int | None = None
    line_end: int | None = None
    module: str | None = Field(
        default=None,
        max_length=256,
        description="Filter by module path prefix, e.g. 'pipecat.runner.daily'. Symbol mode only.",
    )
    class_name: str | None = Field(
        default=None,
        max_length=256,
        description="Filter by class name prefix, e.g. 'DailyTransport' matches DailyTransport, DailyTransportClient, etc. Symbol mode only.",
    )
    content_type: Literal["code", "source"] | None = Field(
        default=None,
        description="Override content type: 'source' for framework, 'code' for examples. "
        "Defaults to 'source' for symbol mode, 'code' for intent/path mode.",
    )
    pipecat_version: str | None = Field(
        default=None,
        max_length=64,
        description="User's pipecat-ai version (e.g. '0.0.95'). When provided, results are scored for compatibility.",
    )
    max_lines: int = Field(default=100, ge=1, le=500)

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
        has_path_range = self.path is not None and self.line_start is not None and not has_intent
        modes = [has_symbol, has_intent, has_path_range]
        if sum(modes) == 0:
            raise ValueError(
                "Exactly one of symbol, intent, or (path + line_start) must be provided."
            )
        if sum(modes) > 1:
            raise ValueError("Only one lookup mode may be set at a time.")
        if not has_symbol and (self.module or self.class_name):
            raise ValueError("`module` and `class_name` filters are only supported in symbol mode.")
        return self


class CodeSnippet(BaseModel):
    """A single code snippet result."""

    content: str
    path: str
    line_start: int
    line_end: int
    language: str | None = None
    pipecat_version_pin: str | None = None
    version_compatibility: Literal["compatible", "newer_required", "older_targeted", "unknown"] | None = None
    citation: Citation
    dependency_notes: list[str] = Field(
        default_factory=list,
        description="Pipecat-internal imports used by the containing method. Extracted per-method from AST — only includes imports this method actually references. May cover more than the visible lines when content is truncated by max_lines.",
    )
    companion_snippets: list[str] = Field(
        default_factory=list,
        description="Qualified method names called by the containing method (e.g. 'TTSService.push_frame'). May cover more than the visible lines when content is truncated by max_lines.",
    )
    related_type_defs: list[str] = Field(
        default_factory=list,
        description="RST type definition names related to this method's parameters (e.g. 'DialoutSendDtmfSettings' for send_dtmf). Look up with search_api(query=name, chunk_type='type_definition').",
    )
    interface_expectations: list[str] = Field(
        default_factory=list,
        description="Frame types yielded and base classes implemented by the containing method/class. May cover more than the visible lines when content is truncated by max_lines.",
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


# Typed alias for the reranker disabled-reason sentinel. Literal so mypy and
# Pydantic both enforce the valid set at boundaries.
RerankerDisabledReason = Literal["config_disabled", "not_cached", "load_failed"]


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
    framework_version: str | None = Field(
        default=None,
        description="Pinned framework version tag (e.g. 'v0.0.96') if set, else None (HEAD).",
    )
    reranker_enabled: bool = Field(
        default=False,
        description="Whether cross-encoder reranking is ACTIVE right now "
        "(reflects runtime availability, not just configured intent).",
    )
    reranker_model: str | None = Field(
        default=None,
        description="Active cross-encoder model name when running, else None.",
    )
    reranker_configured_model: str | None = Field(
        default=None,
        description="The model the operator literally requested (raw env-var "
        "value or field value, pre-validation). Differs from reranker_model "
        "when the request was invalid and silently fell back to a different "
        "model — surfaces misconfiguration without requiring log inspection.",
    )
    reranker_disabled_reason: RerankerDisabledReason | None = Field(
        default=None,
        description="Why reranking is not active. 'config_disabled' "
        "(explicitly turned off), 'not_cached' (model not pre-downloaded), "
        "'load_failed' (model failed to load at runtime). None when active "
        "or when the state is unknown.",
    )


class RerankerStatus(BaseModel):
    """Snapshot of the live reranker's state at status-query time.

    Built by cli.py after ``CrossEncoderReranker`` construction (or skip)
    and passed into ``create_server`` so ``get_hub_status`` reflects
    runtime reality, not just configured intent.
    """

    enabled: bool = Field(description="Whether reranking is actually active.")
    model: str | None = Field(
        default=None, description="Active model name (None when disabled)."
    )
    configured_model: str | None = Field(
        default=None,
        description="Operator's raw requested model (pre-validation). "
        "May differ from .model if the request fell back to the default.",
    )
    disabled_reason: RerankerDisabledReason | None = Field(
        default=None,
        description="Reason for disabled state, or None when active/unknown.",
    )


class SearchApiInput(BaseModel):
    """Input for the search_api MCP tool."""

    query: str = Field(max_length=1000)
    module: str | None = Field(
        default=None,
        max_length=256,
        description="Filter by module path prefix, e.g. 'pipecat.services'.",
    )
    class_name: str | None = Field(
        default=None,
        max_length=256,
        description="Filter by class name prefix, e.g. 'DailyTransport' matches DailyTransport, DailyTransportClient, etc.",
    )
    chunk_type: Literal["module_overview", "class_overview", "method", "function", "type_definition"] | None = Field(
        default=None,
        description="Filter by chunk type.",
    )
    is_dataclass: bool | None = Field(default=None, description="Filter for dataclass types only.")
    yields: str | None = Field(
        default=None,
        max_length=256,
        description="Filter for methods that yield a specific frame type, e.g. 'TTSAudioRawFrame'.",
    )
    calls: str | None = Field(
        default=None,
        max_length=256,
        description="Filter for methods that call a specific method, e.g. 'push_frame'.",
    )
    pipecat_version: str | None = Field(
        default=None,
        max_length=64,
        description="User's pipecat-ai version (e.g. '0.0.95'). When provided, results are scored for compatibility.",
    )
    version_filter: Literal["compatible_only"] | None = Field(
        default=None,
        description="Set to 'compatible_only' to exclude results targeting versions newer than the user's. Requires pipecat_version.",
    )
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def _version_filter_requires_version(self) -> "SearchApiInput":
        if self.version_filter and not self.pipecat_version:
            raise ValueError("version_filter requires pipecat_version to be set.")
        return self


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
        description="Pipecat-internal imports. Precision varies by chunk_type: method/function chunks list only imports the method body references; class_overview lists all pipecat imports in the module (upper bound); module_overview lists all imports including stdlib.",
    )
    yields: list[str] = Field(
        default_factory=list,
        description="Frame types yielded by this method.",
    )
    calls: list[str] = Field(
        default_factory=list,
        description="Methods called by this method (self.X, Class.X).",
    )
    related_types: list[str] = Field(
        default_factory=list,
        description="RST type definition names for this method's parameters. Look up with search_api(query=name, chunk_type='type_definition').",
    )
    pipecat_version_pin: str | None = None
    version_compatibility: Literal["compatible", "newer_required", "older_targeted", "unknown"] | None = None
    citation: Citation
    score: float


class SearchApiOutput(BaseModel):
    """Output for the search_api MCP tool."""

    hits: list[ApiHit] = Field(default_factory=list)
    evidence: EvidenceReport


# ---------------------------------------------------------------------------
# MCP Tool I/O models — check_deprecation
# ---------------------------------------------------------------------------


class CheckDeprecationInput(BaseModel):
    """Input for the check_deprecation MCP tool."""

    symbol: str = Field(
        max_length=256,
        description=(
            "Module path, class name, or method to check. "
            "E.g., 'pipecat.services.grok.llm' or 'DailyTransport'."
        ),
    )


class CheckDeprecationOutput(BaseModel):
    """Output for the check_deprecation MCP tool."""

    deprecated: bool
    replacement: str | None = None
    deprecated_in: str | None = None
    removed_in: str | None = None
    note: str | None = None
