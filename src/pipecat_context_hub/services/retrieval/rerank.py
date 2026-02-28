"""Reciprocal Rank Fusion and code-intent reranking.

Merges results from vector and keyword search paths, applies RRF scoring,
and adjusts scores with code-intent heuristics (symbol match boost, staleness
penalty).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from pipecat_context_hub.shared.types import IndexResult

logger = logging.getLogger(__name__)

# Default RRF constant — standard value from the original RRF paper.
DEFAULT_RRF_K = 60

# Staleness penalty: results older than this many days get penalized.
STALENESS_THRESHOLD_DAYS = 90
STALENESS_PENALTY = 0.05

# Boost for exact symbol match in chunk content.
SYMBOL_MATCH_BOOST = 0.15


def reciprocal_rank_fusion(
    ranked_lists: list[list[IndexResult]],
    k: int = DEFAULT_RRF_K,
) -> dict[str, float]:
    """Compute RRF scores across multiple ranked lists, normalized to 0–1.

    For each result appearing across N ranked lists:
        raw_score = sum(1 / (k + rank_i))  where rank_i is 1-based.

    Scores are normalized by dividing by the theoretical maximum
    (``num_lists / (k + 1)`` — achieved when a result ranks first in every
    list).  This maps the output to the 0–1 range so downstream consumers
    (evidence reports, confidence thresholds) can interpret scores
    consistently.

    Returns a dict mapping chunk_id → normalized RRF score.
    """
    scores: dict[str, float] = {}
    for ranked_list in ranked_lists:
        for rank_0, result in enumerate(ranked_list):
            rank = rank_0 + 1  # 1-based rank
            rrf_score = 1.0 / (k + rank)
            chunk_id = result.chunk.chunk_id
            scores[chunk_id] = scores.get(chunk_id, 0.0) + rrf_score
            logger.debug(
                "RRF: chunk=%s rank=%d list_score=%.6f cumulative=%.6f",
                chunk_id,
                rank,
                rrf_score,
                scores[chunk_id],
            )

    # Normalize to 0–1: divide by theoretical max (rank 1 in every list).
    num_lists = len(ranked_lists)
    max_rrf = num_lists / (k + 1) if num_lists > 0 else 1.0
    if max_rrf > 0:
        scores = {cid: s / max_rrf for cid, s in scores.items()}

    return scores


def _extract_query_symbols(query: str) -> list[str]:
    """Extract potential code symbols from a query string.

    Looks for camelCase, snake_case, and dotted identifiers that are
    likely code symbols rather than plain English words.
    """
    symbols: list[str] = []
    # Match identifiers that look like code: contain underscores, dots, or mixed case
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", query):
        is_camel = bool(re.search(r"[a-z][A-Z]", token))
        has_underscore = "_" in token
        has_dot = "." in token
        if is_camel or has_underscore or has_dot:
            symbols.append(token)
    return symbols


def apply_code_intent_heuristics(
    results: list[IndexResult],
    rrf_scores: dict[str, float],
    query: str,
    now: datetime | None = None,
) -> list[IndexResult]:
    """Apply code-intent heuristics on top of RRF scores.

    Heuristics:
    1. **Symbol match boost:** If query contains code-like symbols and a chunk's
       content contains an exact match, boost the score.
    2. **Staleness penalty:** Penalize chunks whose `indexed_at` is older than
       STALENESS_THRESHOLD_DAYS.

    Returns results sorted by adjusted score (descending).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    query_symbols = _extract_query_symbols(query)
    adjusted: list[tuple[float, IndexResult]] = []

    for result in results:
        chunk_id = result.chunk.chunk_id
        score = rrf_scores.get(chunk_id, result.score)

        # Symbol match boost
        if query_symbols:
            content_lower = result.chunk.content.lower()
            for symbol in query_symbols:
                if symbol.lower() in content_lower:
                    score += SYMBOL_MATCH_BOOST
                    logger.debug(
                        "Symbol boost: chunk=%s symbol=%s new_score=%.6f",
                        chunk_id,
                        symbol,
                        score,
                    )
                    break  # One boost per result

        # Staleness penalty
        indexed_at = result.chunk.indexed_at
        # Ensure both are offset-aware for comparison
        if indexed_at.tzinfo is None:
            indexed_at = indexed_at.replace(tzinfo=timezone.utc)
        age_days = (now - indexed_at).days
        if age_days > STALENESS_THRESHOLD_DAYS:
            score -= STALENESS_PENALTY
            logger.debug(
                "Staleness penalty: chunk=%s age_days=%d new_score=%.6f",
                chunk_id,
                age_days,
                score,
            )

        # Clamp to [0, 1] after all adjustments.
        score = max(0.0, min(1.0, score))
        adjusted.append((score, result))

    # Sort descending by adjusted score, stable (preserves order for ties)
    adjusted.sort(key=lambda x: x[0], reverse=True)

    # Return results with updated scores
    reranked: list[IndexResult] = []
    for adj_score, result in adjusted:
        reranked.append(
            IndexResult(
                chunk=result.chunk,
                score=adj_score,
                match_type=result.match_type,
            )
        )
    return reranked


def rerank(
    vector_results: list[IndexResult],
    keyword_results: list[IndexResult],
    query: str,
    rrf_k: int = DEFAULT_RRF_K,
    now: datetime | None = None,
) -> list[IndexResult]:
    """Full reranking pipeline: RRF merge + code-intent heuristics.

    1. Compute RRF scores across vector and keyword result lists.
    2. Deduplicate by chunk_id, keeping the entry with the higher original score.
    3. Apply code-intent heuristics.
    4. Return sorted results.
    """
    # RRF scoring
    rrf_scores = reciprocal_rank_fusion([vector_results, keyword_results], k=rrf_k)

    # Deduplicate: keep higher-scoring original for each chunk_id
    seen: dict[str, IndexResult] = {}
    for result in vector_results + keyword_results:
        cid = result.chunk.chunk_id
        if cid not in seen or result.score > seen[cid].score:
            seen[cid] = result

    merged = list(seen.values())
    logger.debug(
        "Rerank: %d vector + %d keyword → %d unique candidates",
        len(vector_results),
        len(keyword_results),
        len(merged),
    )

    # Apply heuristics
    return apply_code_intent_heuristics(merged, rrf_scores, query, now=now)
