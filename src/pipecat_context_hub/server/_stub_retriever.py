"""Stub Retriever for v0 — returns empty results.

Replaced by the real retriever in integration (T8).
"""

from __future__ import annotations

from pipecat_context_hub.shared.types import (
    EvidenceReport,
    GetCodeSnippetInput,
    GetCodeSnippetOutput,
    GetDocInput,
    GetDocOutput,
    GetExampleInput,
    GetExampleOutput,
    SearchDocsInput,
    SearchDocsOutput,
    SearchExamplesInput,
    SearchExamplesOutput,
)


def _empty_evidence() -> EvidenceReport:
    return EvidenceReport(
        confidence=0.0,
        confidence_rationale="Stub retriever: no index available.",
    )


class StubRetriever:
    """No-op retriever that satisfies the Retriever protocol with empty results."""

    async def search_docs(self, input: SearchDocsInput) -> SearchDocsOutput:
        return SearchDocsOutput(hits=[], evidence=_empty_evidence())

    async def get_doc(self, input: GetDocInput) -> GetDocOutput:
        from datetime import datetime, timezone

        return GetDocOutput(
            doc_id=input.doc_id,
            title="(not indexed)",
            content="",
            source_url="",
            indexed_at=datetime.now(tz=timezone.utc),
            sections=[],
            evidence=_empty_evidence(),
        )

    async def search_examples(
        self, input: SearchExamplesInput
    ) -> SearchExamplesOutput:
        return SearchExamplesOutput(hits=[], evidence=_empty_evidence())

    async def get_example(self, input: GetExampleInput) -> GetExampleOutput:
        from datetime import datetime, timezone

        from pipecat_context_hub.shared.types import Citation, TaxonomyEntry

        citation = Citation(
            source_url="",
            path="",
            indexed_at=datetime.now(tz=timezone.utc),
        )
        metadata = TaxonomyEntry(
            example_id=input.example_id,
            repo="",
            path="",
        )
        return GetExampleOutput(
            example_id=input.example_id,
            metadata=metadata,
            files=[],
            citation=citation,
            detected_symbols=[],
            evidence=_empty_evidence(),
        )

    async def get_code_snippet(
        self, input: GetCodeSnippetInput
    ) -> GetCodeSnippetOutput:
        return GetCodeSnippetOutput(snippets=[], evidence=_empty_evidence())
