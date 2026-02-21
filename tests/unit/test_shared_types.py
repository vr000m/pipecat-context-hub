"""Tests for shared Pydantic types — serialization round-trips and validation."""

from __future__ import annotations

from datetime import datetime, timezone

from pipecat_context_hub.shared.types import (
    CapabilityTag,
    ChunkedRecord,
    Citation,
    CodeSnippet,
    DocHit,
    EvidenceReport,
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
    IngestResult,
    KnownItem,
    RetrievalResult,
    SearchDocsInput,
    SearchDocsOutput,
    SearchExamplesInput,
    SearchExamplesOutput,
    TaxonomyEntry,
    UnknownItem,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone.utc)


def _round_trip(model_instance):
    """Serialize to JSON and back; assert equality."""
    json_str = model_instance.model_dump_json()
    rebuilt = type(model_instance).model_validate_json(json_str)
    assert rebuilt == model_instance
    return rebuilt


def _make_citation(**overrides: object) -> Citation:
    defaults: dict[str, object] = dict(
        source_url="https://docs.pipecat.ai/guides/start",
        repo="pipecat-ai/pipecat",
        path="guides/start",
        commit_sha="aaa1111",
        section="Intro",
        indexed_at=NOW,
    )
    defaults.update(overrides)
    return Citation.model_validate(defaults)


def _make_evidence(**overrides: object) -> EvidenceReport:
    defaults: dict[str, object] = dict(
        known=[KnownItem(statement="X works", citations=[_make_citation()], confidence=0.9)],
        unknown=[UnknownItem(question="Y?", reason="Not found", suggested_queries=["search Y"])],
        confidence=0.8,
        confidence_rationale="Good match.",
        next_retrieval_queries=["try Z"],
    )
    defaults.update(overrides)
    return EvidenceReport.model_validate(defaults)


# ---------------------------------------------------------------------------
# Core indexing types
# ---------------------------------------------------------------------------


class TestChunkedRecord:
    def test_round_trip(self, sample_chunked_record: ChunkedRecord):
        _round_trip(sample_chunked_record)

    def test_with_embedding(self):
        rec = ChunkedRecord(
            chunk_id="emb-001",
            content="hello",
            content_type="doc",
            source_url="https://example.com",
            path="hello.md",
            indexed_at=NOW,
            embedding=[0.1, 0.2, 0.3],
        )
        rebuilt = _round_trip(rec)
        assert rebuilt.embedding == [0.1, 0.2, 0.3]

    def test_content_type_validation(self):
        """Only doc, code, readme are valid."""
        import pytest

        with pytest.raises(Exception):
            ChunkedRecord(
                chunk_id="x",
                content="x",
                content_type="invalid",  # type: ignore[arg-type]
                source_url="https://x.com",
                path="x",
                indexed_at=NOW,
            )


class TestIndexQuery:
    def test_round_trip(self, sample_index_query: IndexQuery):
        _round_trip(sample_index_query)

    def test_defaults(self):
        q = IndexQuery(query_text="hello")
        assert q.limit == 10
        assert q.filters == {}
        assert q.query_embedding is None

    def test_limit_bounds(self):
        import pytest

        with pytest.raises(Exception):
            IndexQuery(query_text="x", limit=0)
        with pytest.raises(Exception):
            IndexQuery(query_text="x", limit=101)


class TestIndexResult:
    def test_round_trip(self, sample_index_result: IndexResult):
        _round_trip(sample_index_result)


# ---------------------------------------------------------------------------
# Taxonomy types
# ---------------------------------------------------------------------------


class TestCapabilityTag:
    def test_round_trip(self):
        tag = CapabilityTag(name="tts", confidence=0.9, source="code")
        _round_trip(tag)

    def test_defaults(self):
        tag = CapabilityTag(name="rtvi")
        assert tag.confidence == 1.0
        assert tag.source == "directory"


class TestTaxonomyEntry:
    def test_round_trip(self, sample_taxonomy_entry: TaxonomyEntry):
        _round_trip(sample_taxonomy_entry)

    def test_minimal(self):
        entry = TaxonomyEntry(example_id="ex-1", repo="r/r", path="p")
        rebuilt = _round_trip(entry)
        assert rebuilt.foundational_class is None
        assert rebuilt.capabilities == []
        assert rebuilt.commit_sha is None
        assert rebuilt.indexed_at is None

    def test_with_freshness_fields(self):
        entry = TaxonomyEntry(
            example_id="ex-1",
            repo="r/r",
            path="p",
            commit_sha="abc123",
            indexed_at=NOW,
        )
        rebuilt = _round_trip(entry)
        assert rebuilt.commit_sha == "abc123"
        assert rebuilt.indexed_at == NOW


# ---------------------------------------------------------------------------
# Evidence types
# ---------------------------------------------------------------------------


class TestCitation:
    def test_round_trip(self, sample_citation: Citation):
        _round_trip(sample_citation)

    def test_with_line_range(self):
        c = _make_citation(line_range=(10, 25))
        rebuilt = _round_trip(c)
        assert rebuilt.line_range == (10, 25)

    def test_line_range_list_coercion(self):
        """list→tuple coercion for JSON round-trips via model_validate."""
        import json

        c = _make_citation(line_range=(10, 25))
        raw_dict = json.loads(c.model_dump_json())
        # JSON round-trip produces a list, not tuple
        assert isinstance(raw_dict["line_range"], list)
        # model_validate should coerce it back to tuple
        rebuilt = Citation.model_validate(raw_dict)
        assert rebuilt.line_range == (10, 25)
        assert isinstance(rebuilt.line_range, tuple)


class TestKnownItem:
    def test_round_trip(self):
        item = KnownItem(statement="fact", citations=[_make_citation()], confidence=0.95)
        _round_trip(item)


class TestUnknownItem:
    def test_round_trip(self):
        item = UnknownItem(question="q?", reason="r", suggested_queries=["sq"])
        _round_trip(item)


class TestEvidenceReport:
    def test_round_trip(self, sample_evidence_report: EvidenceReport):
        _round_trip(sample_evidence_report)

    def test_empty(self):
        report = EvidenceReport(confidence=0.0, confidence_rationale="No data.")
        rebuilt = _round_trip(report)
        assert rebuilt.known == []
        assert rebuilt.unknown == []
        assert rebuilt.next_retrieval_queries == []


# ---------------------------------------------------------------------------
# Retrieval result
# ---------------------------------------------------------------------------


class TestRetrievalResult:
    def test_round_trip(self, sample_index_result: IndexResult):
        result = RetrievalResult(
            results=[sample_index_result],
            evidence=_make_evidence(),
            query="how to use pipecat",
            total_candidates=42,
        )
        _round_trip(result)


# ---------------------------------------------------------------------------
# Ingest result
# ---------------------------------------------------------------------------


class TestIngestResult:
    def test_round_trip(self):
        r = IngestResult(source="docs.pipecat.ai", records_upserted=100, duration_seconds=3.5)
        _round_trip(r)

    def test_with_errors(self):
        r = IngestResult(source="github", errors=["timeout on page X"])
        rebuilt = _round_trip(r)
        assert rebuilt.errors == ["timeout on page X"]


# ---------------------------------------------------------------------------
# Tool I/O: search_docs
# ---------------------------------------------------------------------------


class TestSearchDocs:
    def test_input_round_trip(self):
        _round_trip(SearchDocsInput(query="install pipecat"))

    def test_input_with_area(self):
        _round_trip(SearchDocsInput(query="q", area="guides", limit=5))

    def test_output_round_trip(self):
        out = SearchDocsOutput(
            hits=[
                DocHit(
                    doc_id="d1",
                    title="Getting Started",
                    section="Install",
                    snippet="pip install pipecat-ai",
                    citation=_make_citation(),
                    score=0.9,
                ),
            ],
            evidence=_make_evidence(),
        )
        _round_trip(out)


# ---------------------------------------------------------------------------
# Tool I/O: get_doc
# ---------------------------------------------------------------------------


class TestGetDoc:
    def test_input_round_trip(self):
        _round_trip(GetDocInput(doc_id="d1"))

    def test_output_round_trip(self):
        out = GetDocOutput(
            doc_id="d1",
            title="Getting Started",
            content="# Getting Started\n...",
            source_url="https://docs.pipecat.ai/guides/start",
            indexed_at=NOW,
            sections=["Install", "Usage"],
            evidence=_make_evidence(),
        )
        _round_trip(out)


# ---------------------------------------------------------------------------
# Tool I/O: search_examples
# ---------------------------------------------------------------------------


class TestSearchExamples:
    def test_input_round_trip(self):
        _round_trip(SearchExamplesInput(query="wake word"))

    def test_input_with_filters(self):
        _round_trip(
            SearchExamplesInput(
                query="q",
                repo="pipecat-ai/pipecat",
                tags=["tts", "rtvi"],
                foundational_class="01-say-one-thing",
                limit=5,
            )
        )

    def test_output_round_trip(self):
        out = SearchExamplesOutput(
            hits=[
                ExampleHit(
                    example_id="ex-1",
                    summary="Wake word example",
                    foundational_class="07-interruptible",
                    capability_tags=["wake-word", "tts"],
                    key_files=["main.py"],
                    repo="pipecat-ai/pipecat",
                    path="examples/foundational/07-interruptible.py",
                    commit_sha="abc",
                    citation=_make_citation(),
                    score=0.88,
                ),
            ],
            evidence=_make_evidence(),
        )
        _round_trip(out)


# ---------------------------------------------------------------------------
# Tool I/O: get_example
# ---------------------------------------------------------------------------


class TestGetExample:
    def test_input_round_trip(self):
        _round_trip(GetExampleInput(example_id="ex-1"))

    def test_output_round_trip(self):
        out = GetExampleOutput(
            example_id="ex-1",
            metadata=TaxonomyEntry(example_id="ex-1", repo="r/r", path="p"),
            files=[ExampleFile(path="main.py", content="print('hi')", language="python")],
            citation=_make_citation(),
            detected_symbols=["main", "Pipeline"],
            evidence=_make_evidence(),
        )
        _round_trip(out)


# ---------------------------------------------------------------------------
# Tool I/O: get_code_snippet
# ---------------------------------------------------------------------------


class TestGetCodeSnippet:
    def test_input_by_intent(self):
        _round_trip(GetCodeSnippetInput(intent="create a pipeline with TTS"))

    def test_input_by_path(self):
        _round_trip(GetCodeSnippetInput(path="examples/01.py", line_start=10, line_end=30))

    def test_input_by_symbol(self):
        _round_trip(GetCodeSnippetInput(symbol="Pipeline.run"))

    def test_input_rejects_no_mode(self):
        """Must provide at least one lookup mode."""
        import pytest

        with pytest.raises(ValueError, match="Exactly one of"):
            GetCodeSnippetInput()

    def test_input_rejects_multiple_modes(self):
        """Cannot set both symbol and intent."""
        import pytest

        with pytest.raises(ValueError, match="Only one lookup mode"):
            GetCodeSnippetInput(symbol="Pipeline.run", intent="create pipeline")

    def test_input_path_without_line_start_is_not_a_mode(self):
        """path alone (without line_start) doesn't count as path+line_start mode."""
        import pytest

        with pytest.raises(ValueError, match="Exactly one of"):
            GetCodeSnippetInput(path="examples/01.py")

    def test_input_intent_with_path_and_line_start(self):
        """intent + path + line_start is valid (path scopes the intent search)."""
        inp = GetCodeSnippetInput(
            intent="kokoro TTS functions",
            path="src/processors/kokoro_tts.py",
            line_start=40,
            max_lines=100,
        )
        assert inp.intent == "kokoro TTS functions"
        assert inp.path == "src/processors/kokoro_tts.py"
        assert inp.line_start == 40

    def test_input_intent_with_path_only(self):
        """intent + path (no line_start) is valid — path filters results."""
        inp = GetCodeSnippetInput(
            intent="create pipeline",
            path="examples/bot.py",
        )
        assert inp.intent is not None
        assert inp.path is not None

    def test_output_round_trip(self):
        out = GetCodeSnippetOutput(
            snippets=[
                CodeSnippet(
                    content="pipeline = Pipeline()",
                    path="examples/01.py",
                    line_start=5,
                    line_end=5,
                    language="python",
                    citation=_make_citation(),
                    dependency_notes=["from pipecat.pipeline import Pipeline"],
                    companion_snippets=["snippet-transport-setup"],
                    interface_expectations=["Requires configured transport"],
                ),
            ],
            evidence=_make_evidence(),
        )
        _round_trip(out)
