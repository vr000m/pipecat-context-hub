"""Citation assembly and evidence report generation.

Builds Citation objects from IndexResults and assembles EvidenceReports with
known/unknown/confidence/next_retrieval_queries.
"""

from __future__ import annotations

import logging

from pipecat_context_hub.shared.types import (
    Citation,
    EvidenceReport,
    IndexResult,
    KnownItem,
    UnknownItem,
)

logger = logging.getLogger(__name__)

# Confidence thresholds for determining result quality.
HIGH_SCORE_THRESHOLD = 0.5
LOW_SCORE_THRESHOLD = 0.1
MIN_RESULTS_FOR_HIGH_CONFIDENCE = 3


def build_citation(result: IndexResult) -> Citation:
    """Build a Citation from an IndexResult's chunk metadata."""
    chunk = result.chunk
    return Citation(
        source_url=chunk.source_url,
        repo=chunk.repo,
        path=chunk.path,
        commit_sha=chunk.commit_sha,
        section=chunk.metadata.get("section"),
        line_range=chunk.metadata.get("line_range"),
        indexed_at=chunk.indexed_at,
    )


def _compute_confidence(
    results: list[IndexResult],
    query: str,
) -> tuple[float, str]:
    """Compute overall confidence score and rationale.

    Factors:
    - Number of results
    - Score distribution (high scores = high confidence)
    - Coverage (are there enough results to be useful?)
    """
    if not results:
        return 0.0, "No results found for the query."

    scores = [r.score for r in results]
    max_score = max(scores)
    avg_score = sum(scores) / len(scores)
    count = len(results)

    # Base confidence from score quality
    if max_score >= HIGH_SCORE_THRESHOLD and count >= MIN_RESULTS_FOR_HIGH_CONFIDENCE:
        confidence = min(0.95, 0.6 + avg_score * 0.3 + min(count / 10.0, 0.1))
        rationale = (
            f"Found {count} results with strong relevance "
            f"(top score: {max_score:.2f}, avg: {avg_score:.2f})."
        )
    elif max_score >= LOW_SCORE_THRESHOLD:
        confidence = min(0.7, 0.3 + avg_score * 0.3 + min(count / 20.0, 0.1))
        rationale = (
            f"Found {count} results with moderate relevance "
            f"(top score: {max_score:.2f}, avg: {avg_score:.2f})."
        )
    else:
        confidence = max(0.05, 0.1 + avg_score * 0.2)
        rationale = (
            f"Found {count} results but relevance scores are low "
            f"(top score: {max_score:.2f}, avg: {avg_score:.2f}). "
            "Results may not fully address the query."
        )

    logger.debug(
        "Confidence: score=%.3f rationale=%r (count=%d max=%.3f avg=%.3f)",
        confidence,
        rationale,
        count,
        max_score,
        avg_score,
    )
    return confidence, rationale


def _generate_next_queries(
    query: str,
    results: list[IndexResult],
    filters: dict[str, object] | None = None,
) -> list[str]:
    """Generate deterministic follow-up query suggestions.

    Heuristics:
    1. If results are sparse, suggest broadening the query.
    2. If a repo filter was used, suggest searching without it.
    3. Suggest related terms based on the query.
    4. If results exist, suggest drilling into the top result's context.
    """
    suggestions: list[str] = []
    effective_filters = filters or {}

    # Sparse results: suggest broadening
    if len(results) < 3:
        # Suggest dropping qualifier words
        words = query.split()
        if len(words) > 2:
            shorter = " ".join(words[:2])
            suggestions.append(f"Broaden search: {shorter}")

    # Repo filter: suggest widening scope
    if "repo" in effective_filters:
        suggestions.append(f"Search across all repos: {query}")

    # Content type filter: suggest removing it
    if "content_type" in effective_filters:
        suggestions.append(f"Search all content types: {query}")

    # Low confidence: suggest related terms
    if len(results) < 5:
        suggestions.append(f"Related: {query} tutorial")
        suggestions.append(f"Related: {query} example")

    # If we have results, suggest drilling into context of the top one
    if results:
        top = results[0]
        if top.chunk.repo:
            suggestions.append(
                f"More from {top.chunk.repo}: {query}"
            )

    # Cap suggestions
    return suggestions[:5]


def build_known_items(results: list[IndexResult]) -> list[KnownItem]:
    """Build KnownItem entries from search results.

    Each result with a meaningful score becomes a known fact with citation.
    """
    known: list[KnownItem] = []
    for result in results:
        citation = build_citation(result)
        # Truncate content for the statement
        content = result.chunk.content
        statement = content[:200] + "..." if len(content) > 200 else content
        known.append(
            KnownItem(
                statement=statement,
                citations=[citation],
                confidence=min(1.0, max(0.0, result.score)),
            )
        )
    return known


def build_unknown_items(
    query: str,
    results: list[IndexResult],
    filters: dict[str, object] | None = None,
) -> list[UnknownItem]:
    """Build UnknownItem entries for gaps in the results.

    Identifies what the query asked about but wasn't covered.
    """
    unknowns: list[UnknownItem] = []
    effective_filters = filters or {}

    if not results:
        unknowns.append(
            UnknownItem(
                question=f"No content found for: {query}",
                reason="The index returned zero results for this query.",
                suggested_queries=_generate_next_queries(query, results, effective_filters),
            )
        )
    elif all(r.score < LOW_SCORE_THRESHOLD for r in results):
        unknowns.append(
            UnknownItem(
                question=f"Low relevance results for: {query}",
                reason=(
                    "All results had low relevance scores, suggesting the query "
                    "may not match indexed content well."
                ),
                suggested_queries=_generate_next_queries(query, results, effective_filters),
            )
        )

    return unknowns


def assemble_evidence(
    query: str,
    results: list[IndexResult],
    filters: dict[str, object] | None = None,
) -> EvidenceReport:
    """Assemble a complete EvidenceReport from retrieval results.

    Builds known items, unknown items, computes confidence, and generates
    next_retrieval_queries.
    """
    known = build_known_items(results)
    unknown = build_unknown_items(query, results, filters)
    confidence, rationale = _compute_confidence(results, query)
    next_queries = _generate_next_queries(query, results, filters)

    report = EvidenceReport(
        known=known,
        unknown=unknown,
        confidence=confidence,
        confidence_rationale=rationale,
        next_retrieval_queries=next_queries,
    )
    logger.debug(
        "Evidence report: known=%d unknown=%d confidence=%.3f next_queries=%d",
        len(known),
        len(unknown),
        confidence,
        len(next_queries),
    )
    return report


def build_single_item_evidence(
    result: IndexResult | None,
    item_id: str,
    item_type: str,
) -> EvidenceReport:
    """Build evidence report for a single-item lookup (get_doc, get_example).

    Used for direct ID lookups rather than search results.
    """
    if result is None:
        return EvidenceReport(
            known=[],
            unknown=[
                UnknownItem(
                    question=f"{item_type} '{item_id}' not found.",
                    reason=f"No {item_type} with the given ID exists in the index.",
                    suggested_queries=[
                        f"Search for {item_type}s related to: {item_id}",
                    ],
                )
            ],
            confidence=0.0,
            confidence_rationale=f"The requested {item_type} was not found in the index.",
            next_retrieval_queries=[
                f"Search for {item_type}s related to: {item_id}",
            ],
        )

    # Suppress unused variable warning — `now` is intentional for the branch above.
    citation = build_citation(result)
    return EvidenceReport(
        known=[
            KnownItem(
                statement=f"Found {item_type} '{item_id}'.",
                citations=[citation],
                confidence=1.0,
            )
        ],
        unknown=[],
        confidence=1.0,
        confidence_rationale=f"Direct lookup for {item_type} '{item_id}' succeeded.",
        next_retrieval_queries=[],
    )
