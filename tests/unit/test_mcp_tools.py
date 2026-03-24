"""Unit tests for MCP tool handlers.

Each handler is tested with:
1. Valid input → correct output schema.
2. Invalid input → ValidationError.
3. Output matches expected contract (includes EvidenceReport).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from pipecat_context_hub.shared.types import (
    ApiHit,
    Citation,
    CodeSnippet,
    DocHit,
    EvidenceReport,
    ExampleFile,
    ExampleHit,
    GetCodeSnippetOutput,
    GetDocOutput,
    GetExampleOutput,
    KnownItem,
    SearchApiOutput,
    SearchDocsOutput,
    SearchExamplesOutput,
    TaxonomyEntry,
)
from pipecat_context_hub.server.tools.search_docs import handle_search_docs
from pipecat_context_hub.server.tools.get_doc import handle_get_doc
from pipecat_context_hub.server.tools.search_examples import handle_search_examples
from pipecat_context_hub.server.tools.get_example import handle_get_example
from pipecat_context_hub.server.tools.get_code_snippet import handle_get_code_snippet
from pipecat_context_hub.server.tools.search_api import handle_search_api


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 2, 18, tzinfo=timezone.utc)


def _make_citation(**overrides: Any) -> Citation:
    defaults: dict[str, Any] = {
        "source_url": "https://docs.pipecat.ai/guides/getting-started",
        "repo": "pipecat-ai/pipecat",
        "path": "docs/guides/getting-started.md",
        "commit_sha": "abc1234",
        "section": "Installation",
        "indexed_at": NOW,
    }
    defaults.update(overrides)
    return Citation.model_validate(defaults)


def _make_evidence(**overrides: Any) -> EvidenceReport:
    defaults: dict[str, Any] = {
        "known": [
            KnownItem(
                statement="Pipecat supports TTS.",
                citations=[_make_citation()],
                confidence=0.9,
            )
        ],
        "unknown": [],
        "confidence": 0.9,
        "confidence_rationale": "Good match.",
    }
    defaults.update(overrides)
    return EvidenceReport.model_validate(defaults)


@pytest.fixture
def mock_retriever():
    """A mock Retriever with all methods pre-configured."""
    retriever = AsyncMock()

    # search_docs
    retriever.search_docs.return_value = SearchDocsOutput(
        hits=[
            DocHit(
                doc_id="doc-gs-001",
                title="Getting Started",
                section="Installation",
                snippet="Install pipecat with pip.",
                citation=_make_citation(),
                score=0.92,
            )
        ],
        evidence=_make_evidence(),
    )

    # get_doc
    retriever.get_doc.return_value = GetDocOutput(
        doc_id="doc-gs-001",
        title="Getting Started",
        content="# Getting Started\n\nInstall pipecat.",
        source_url="https://docs.pipecat.ai/guides/getting-started",
        indexed_at=NOW,
        sections=["Installation", "Quick Start"],
        evidence=_make_evidence(),
    )

    # search_examples
    retriever.search_examples.return_value = SearchExamplesOutput(
        hits=[
            ExampleHit(
                example_id="foundational-01",
                summary="Minimal TTS example.",
                foundational_class="01-say-one-thing",
                capability_tags=["tts", "pipeline"],
                key_files=["bot.py"],
                repo="pipecat-ai/pipecat",
                path="examples/foundational/01-say-one-thing",
                commit_sha="abc1234",
                citation=_make_citation(),
                score=0.88,
            )
        ],
        evidence=_make_evidence(),
    )

    # get_example
    retriever.get_example.return_value = GetExampleOutput(
        example_id="foundational-01",
        metadata=TaxonomyEntry(
            example_id="foundational-01",
            repo="pipecat-ai/pipecat",
            path="examples/foundational/01-say-one-thing",
        ),
        files=[ExampleFile(path="bot.py", content="print('hello')", language="python")],
        citation=_make_citation(),
        detected_symbols=["main"],
        evidence=_make_evidence(),
    )

    # search_api
    retriever.search_api.return_value = SearchApiOutput(
        hits=[
            ApiHit(
                chunk_id="api-001",
                module_path="pipecat.services.tts",
                class_name="TTSService",
                method_name=None,
                chunk_type="class_overview",
                snippet="class TTSService(BaseService): ...",
                citation=_make_citation(),
                score=0.95,
            )
        ],
        evidence=_make_evidence(),
    )

    # get_code_snippet
    retriever.get_code_snippet.return_value = GetCodeSnippetOutput(
        snippets=[
            CodeSnippet(
                content="async def main():\n    pass",
                path="examples/foundational/01-say-one-thing/bot.py",
                line_start=1,
                line_end=2,
                language="python",
                citation=_make_citation(),
            )
        ],
        evidence=_make_evidence(),
    )

    return retriever


# ---------------------------------------------------------------------------
# search_docs tests
# ---------------------------------------------------------------------------


class TestSearchDocs:
    async def test_valid_input_returns_output(self, mock_retriever):
        result = await handle_search_docs({"query": "getting started"}, mock_retriever)
        parsed = SearchDocsOutput.model_validate_json(result)
        assert len(parsed.hits) == 1
        assert parsed.hits[0].doc_id == "doc-gs-001"
        assert parsed.evidence.confidence == 0.9

    async def test_with_optional_params(self, mock_retriever):
        result = await handle_search_docs(
            {"query": "TTS", "area": "guides", "limit": 5}, mock_retriever
        )
        parsed = SearchDocsOutput.model_validate_json(result)
        assert len(parsed.hits) == 1
        # Verify the input was parsed correctly
        call_args = mock_retriever.search_docs.call_args[0][0]
        assert call_args.query == "TTS"
        assert call_args.area == "guides"
        assert call_args.limit == 5

    async def test_missing_query_raises(self, mock_retriever):
        with pytest.raises(ValidationError):
            await handle_search_docs({}, mock_retriever)

    async def test_limit_out_of_range_raises(self, mock_retriever):
        with pytest.raises(ValidationError):
            await handle_search_docs({"query": "test", "limit": 0}, mock_retriever)

        with pytest.raises(ValidationError):
            await handle_search_docs({"query": "test", "limit": 100}, mock_retriever)


# ---------------------------------------------------------------------------
# get_doc tests
# ---------------------------------------------------------------------------


class TestGetDoc:
    async def test_valid_input_returns_output(self, mock_retriever):
        result = await handle_get_doc({"doc_id": "doc-gs-001"}, mock_retriever)
        parsed = GetDocOutput.model_validate_json(result)
        assert parsed.doc_id == "doc-gs-001"
        assert parsed.title == "Getting Started"
        assert parsed.evidence.confidence == 0.9

    async def test_with_section(self, mock_retriever):
        result = await handle_get_doc(
            {"doc_id": "doc-gs-001", "section": "Installation"}, mock_retriever
        )
        parsed = GetDocOutput.model_validate_json(result)
        assert parsed.doc_id == "doc-gs-001"

    async def test_missing_doc_id_raises(self, mock_retriever):
        with pytest.raises(ValidationError):
            await handle_get_doc({}, mock_retriever)


# ---------------------------------------------------------------------------
# search_examples tests
# ---------------------------------------------------------------------------


class TestSearchExamples:
    async def test_valid_input_returns_output(self, mock_retriever):
        result = await handle_search_examples({"query": "TTS example"}, mock_retriever)
        parsed = SearchExamplesOutput.model_validate_json(result)
        assert len(parsed.hits) == 1
        assert parsed.hits[0].example_id == "foundational-01"

    async def test_with_filters(self, mock_retriever):
        result = await handle_search_examples(
            {
                "query": "TTS",
                "repo": "pipecat-ai/pipecat",
                "tags": ["tts"],
                "foundational_class": "01-say-one-thing",
                "limit": 3,
            },
            mock_retriever,
        )
        parsed = SearchExamplesOutput.model_validate_json(result)
        assert len(parsed.hits) == 1
        call_args = mock_retriever.search_examples.call_args[0][0]
        assert call_args.repo == "pipecat-ai/pipecat"
        assert call_args.tags == ["tts"]

    async def test_missing_query_raises(self, mock_retriever):
        with pytest.raises(ValidationError):
            await handle_search_examples({}, mock_retriever)


# ---------------------------------------------------------------------------
# get_example tests
# ---------------------------------------------------------------------------


class TestGetExample:
    async def test_valid_input_returns_output(self, mock_retriever):
        result = await handle_get_example({"example_id": "foundational-01"}, mock_retriever)
        parsed = GetExampleOutput.model_validate_json(result)
        assert parsed.example_id == "foundational-01"
        assert len(parsed.files) == 1
        assert parsed.evidence.confidence == 0.9

    async def test_with_optional_params(self, mock_retriever):
        result = await handle_get_example(
            {"example_id": "foundational-01", "include_readme": False},
            mock_retriever,
        )
        parsed = GetExampleOutput.model_validate_json(result)
        assert parsed.example_id == "foundational-01"
        call_args = mock_retriever.get_example.call_args[0][0]
        assert call_args.include_readme is False

    async def test_missing_example_id_raises(self, mock_retriever):
        with pytest.raises(ValidationError):
            await handle_get_example({}, mock_retriever)


# ---------------------------------------------------------------------------
# get_code_snippet tests
# ---------------------------------------------------------------------------


class TestGetCodeSnippet:
    async def test_by_intent(self, mock_retriever):
        result = await handle_get_code_snippet({"intent": "create a pipeline"}, mock_retriever)
        parsed = GetCodeSnippetOutput.model_validate_json(result)
        assert len(parsed.snippets) == 1
        assert parsed.evidence.confidence == 0.9

    async def test_by_symbol(self, mock_retriever):
        result = await handle_get_code_snippet({"symbol": "Pipeline"}, mock_retriever)
        parsed = GetCodeSnippetOutput.model_validate_json(result)
        assert len(parsed.snippets) == 1

    async def test_by_path_and_line(self, mock_retriever):
        result = await handle_get_code_snippet(
            {"path": "src/pipeline.py", "line_start": 10}, mock_retriever
        )
        parsed = GetCodeSnippetOutput.model_validate_json(result)
        assert len(parsed.snippets) == 1

    async def test_by_intent_with_path_and_line(self, mock_retriever):
        """intent + path + line_start is valid (path scopes the search)."""
        result = await handle_get_code_snippet(
            {
                "intent": "kokoro TTS functions",
                "path": "src/processors/kokoro_tts.py",
                "line_start": 40,
                "max_lines": 100,
            },
            mock_retriever,
        )
        parsed = GetCodeSnippetOutput.model_validate_json(result)
        assert len(parsed.snippets) == 1

    async def test_no_lookup_mode_raises(self, mock_retriever):
        with pytest.raises(ValidationError):
            await handle_get_code_snippet({}, mock_retriever)

    async def test_multiple_lookup_modes_raises(self, mock_retriever):
        with pytest.raises(ValidationError):
            await handle_get_code_snippet(
                {"symbol": "Pipeline", "intent": "create pipeline"}, mock_retriever
            )

    async def test_max_lines_validation(self, mock_retriever):
        with pytest.raises(ValidationError):
            await handle_get_code_snippet({"intent": "test", "max_lines": 0}, mock_retriever)

        with pytest.raises(ValidationError):
            await handle_get_code_snippet({"intent": "test", "max_lines": 501}, mock_retriever)


# ---------------------------------------------------------------------------
# search_api tests
# ---------------------------------------------------------------------------


class TestSearchApi:
    async def test_valid_input_returns_output(self, mock_retriever):
        result = await handle_search_api({"query": "TTSService"}, mock_retriever)
        parsed = SearchApiOutput.model_validate_json(result)
        assert len(parsed.hits) == 1
        assert parsed.hits[0].chunk_id == "api-001"
        assert parsed.evidence.confidence == 0.9

    async def test_with_filters(self, mock_retriever):
        result = await handle_search_api(
            {
                "query": "TTSService",
                "module": "pipecat.services",
                "chunk_type": "class_overview",
            },
            mock_retriever,
        )
        parsed = SearchApiOutput.model_validate_json(result)
        assert len(parsed.hits) == 1
        call_args = mock_retriever.search_api.call_args[0][0]
        assert call_args.module == "pipecat.services"
        assert call_args.chunk_type == "class_overview"

    async def test_invalid_chunk_type_raises(self, mock_retriever):
        with pytest.raises(ValidationError):
            await handle_search_api({"query": "test", "chunk_type": "invalid_type"}, mock_retriever)

    async def test_yields_and_calls_filters(self, mock_retriever):
        """yields and calls filters are passed through to the retriever."""
        mock_retriever.search_api.return_value = SearchApiOutput(
            hits=[
                ApiHit(
                    chunk_id="api-yields",
                    module_path="pipecat.services.tts",
                    class_name="TTSService",
                    method_name="run_tts",
                    chunk_type="method",
                    snippet="async def run_tts(self, text): ...",
                    yields=["TTSAudioRawFrame", "TTSStartedFrame"],
                    calls=["push_frame", "_process_audio"],
                    citation=_make_citation(),
                    score=0.91,
                )
            ],
            evidence=_make_evidence(),
        )
        result = await handle_search_api(
            {"query": "TTS", "yields": "TTSAudioRawFrame", "calls": "push_frame"},
            mock_retriever,
        )
        parsed = SearchApiOutput.model_validate_json(result)
        assert len(parsed.hits) == 1
        assert "TTSAudioRawFrame" in parsed.hits[0].yields
        assert "push_frame" in parsed.hits[0].calls
        # Verify filters were forwarded
        call_args = mock_retriever.search_api.call_args[0][0]
        assert call_args.yields == "TTSAudioRawFrame"
        assert call_args.calls == "push_frame"

    async def test_missing_query_raises(self, mock_retriever):
        with pytest.raises(ValidationError):
            await handle_search_api({}, mock_retriever)
