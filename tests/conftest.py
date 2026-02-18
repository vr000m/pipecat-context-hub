"""Shared test fixtures for the Pipecat Context Hub test suite."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipecat_context_hub.shared.types import (
    CapabilityTag,
    ChunkedRecord,
    Citation,
    EvidenceReport,
    IndexQuery,
    IndexResult,
    KnownItem,
    TaxonomyEntry,
    UnknownItem,
)


@pytest.fixture
def sample_citation() -> Citation:
    return Citation(
        source_url="https://docs.pipecat.ai/guides/getting-started",
        repo="pipecat-ai/pipecat",
        path="docs/guides/getting-started.md",
        commit_sha="abc1234",
        section="Installation",
        indexed_at=datetime(2026, 2, 18, tzinfo=timezone.utc),
    )


@pytest.fixture
def sample_chunked_record() -> ChunkedRecord:
    return ChunkedRecord(
        chunk_id="doc-getting-started-001",
        content="# Getting Started\n\nInstall pipecat with `pip install pipecat-ai`.",
        content_type="doc",
        source_url="https://docs.pipecat.ai/guides/getting-started",
        repo=None,
        path="guides/getting-started",
        commit_sha=None,
        indexed_at=datetime(2026, 2, 18, tzinfo=timezone.utc),
    )


@pytest.fixture
def sample_code_record() -> ChunkedRecord:
    return ChunkedRecord(
        chunk_id="code-pipecat-bot-001",
        content='from pipecat.pipeline import Pipeline\n\nasync def main():\n    pipeline = Pipeline()\n    await pipeline.run()',
        content_type="code",
        source_url="https://github.com/pipecat-ai/pipecat/blob/main/examples/foundational/01-say-one-thing.py",
        repo="pipecat-ai/pipecat",
        path="examples/foundational/01-say-one-thing.py",
        commit_sha="def5678",
        indexed_at=datetime(2026, 2, 18, tzinfo=timezone.utc),
    )


@pytest.fixture
def sample_taxonomy_entry() -> TaxonomyEntry:
    return TaxonomyEntry(
        example_id="foundational-01-say-one-thing",
        repo="pipecat-ai/pipecat",
        path="examples/foundational/01-say-one-thing.py",
        foundational_class="01-say-one-thing",
        capabilities=[
            CapabilityTag(name="tts", confidence=0.95, source="code"),
            CapabilityTag(name="pipeline", confidence=1.0, source="directory"),
        ],
        key_files=["01-say-one-thing.py"],
        summary="Minimal example: say a single phrase via TTS.",
    )


@pytest.fixture
def sample_evidence_report(sample_citation: Citation) -> EvidenceReport:
    return EvidenceReport(
        known=[
            KnownItem(
                statement="Pipecat supports ElevenLabs TTS.",
                citations=[sample_citation],
                confidence=0.95,
            ),
        ],
        unknown=[
            UnknownItem(
                question="Does Pipecat support real-time screen sharing?",
                reason="No matching documentation found.",
                suggested_queries=["pipecat screen share", "pipecat RTVI video"],
            ),
        ],
        confidence=0.7,
        confidence_rationale="Partial match: TTS confirmed, screen share unknown.",
        next_retrieval_queries=["pipecat screen share example", "RTVI frontend integration"],
    )


@pytest.fixture
def sample_index_result(sample_chunked_record: ChunkedRecord) -> IndexResult:
    return IndexResult(
        chunk=sample_chunked_record,
        score=0.85,
        match_type="vector",
    )


@pytest.fixture
def sample_index_query() -> IndexQuery:
    return IndexQuery(
        query_text="how to create a pipecat bot",
        filters={"content_type": "doc"},
        limit=5,
    )
