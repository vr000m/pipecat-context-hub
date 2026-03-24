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

# Graduated staleness: linear decay, max penalty at 1 year.
STALENESS_MAX_PENALTY = 0.10
STALENESS_DECAY_DAYS = 365

# Boost for exact symbol match in chunk content.
SYMBOL_MATCH_BOOST = 0.15

# Boost for chunks found by both vector AND keyword search.
DUAL_HIT_BONUS = 0.10


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

    Looks for camelCase, snake_case, dotted identifiers, and UPPERCASE
    acronyms (2+ letters, e.g. TTS, STT, VAD, LLM, RTVI) that are
    likely code symbols rather than plain English words.
    """
    symbols: list[str] = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", query):
        is_camel = bool(re.search(r"[a-z][A-Z]", token))
        has_underscore = "_" in token
        has_dot = "." in token
        is_upper_acronym = len(token) >= 2 and token.isupper()
        if is_camel or has_underscore or has_dot or is_upper_acronym:
            symbols.append(token)
    return symbols


def apply_code_intent_heuristics(
    results: list[IndexResult],
    rrf_scores: dict[str, float],
    query: str,
    dual_hit_ids: set[str] | None = None,
    now: datetime | None = None,
) -> list[IndexResult]:
    """Apply code-intent heuristics on top of RRF scores.

    Heuristics:
    1. **Symbol match boost:** If query contains code-like symbols and a chunk's
       content contains an exact match, boost the score.
    2. **Dual-hit bonus:** Chunks found by both vector AND keyword search get a
       score boost (stronger signal than single-backend match).
    3. **Graduated staleness:** Linear decay penalty based on age, capped at
       ``STALENESS_MAX_PENALTY`` at ``STALENESS_DECAY_DAYS``.

    Returns results sorted by adjusted score (descending).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if dual_hit_ids is None:
        dual_hit_ids = set()

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

        # Dual-hit bonus
        if chunk_id in dual_hit_ids:
            score += DUAL_HIT_BONUS
            logger.debug(
                "Dual-hit bonus: chunk=%s new_score=%.6f",
                chunk_id,
                score,
            )

        # Graduated staleness penalty
        indexed_at = result.chunk.indexed_at
        if indexed_at.tzinfo is None:
            indexed_at = indexed_at.replace(tzinfo=timezone.utc)
        age_days = (now - indexed_at).days
        if age_days > 0:
            penalty = min(STALENESS_MAX_PENALTY, age_days / STALENESS_DECAY_DAYS * STALENESS_MAX_PENALTY)
            score -= penalty
            if penalty > 0.01:
                logger.debug(
                    "Staleness penalty: chunk=%s age_days=%d penalty=%.4f new_score=%.6f",
                    chunk_id,
                    age_days,
                    penalty,
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


# Maximum total results from the same repo or file before diversity penalty.
_MAX_SAME_SOURCE = 3

# Chunk-type preference order for search_api results (lower index = higher preference).
_CHUNK_TYPE_PREFERENCE = {"method": 0, "function": 1, "class_overview": 2, "module_overview": 3}


def _apply_diversity(
    results: list[IndexResult],
    filters: dict[str, object] | None = None,
) -> list[IndexResult]:
    """Re-order results to improve repo/file diversity and apply chunk-type preference.

    Uses a two-pass approach:
    1. Apply chunk-type preference boost for source results (when no filter set).
    2. Greedy interleave: place each result at the earliest position where the
       consecutive-run limit (``_MAX_SAME_SOURCE``) is not violated. Deferred
       items are re-tried after each successful placement.

    Best-effort guarantee: when diverse results exist, no more than
    ``_MAX_SAME_SOURCE`` consecutive results from the same repo/file appear.
    When ALL remaining candidates share a repo/file (no diversity possible),
    they are appended in score order — the constraint cannot be satisfied.
    """
    if not results or len(results) <= 1:
        return results

    effective_filters = filters or {}

    # Apply chunk-type preference boost for source content (search_api)
    # only when chunk_type is not explicitly filtered.
    apply_chunk_pref = (
        "chunk_type" not in effective_filters
        and effective_filters.get("content_type") == "source"
    )

    # Phase 1: apply chunk-type preference boost
    boosted: list[IndexResult] = []
    for result in results:
        score = result.score
        if apply_chunk_pref:
            chunk_type = result.chunk.metadata.get("chunk_type", "")
            pref = _CHUNK_TYPE_PREFERENCE.get(chunk_type, 4)
            score += max(0, (4 - pref)) * 0.005
            score = max(0.0, min(1.0, score))
        boosted.append(IndexResult(chunk=result.chunk, score=score, match_type=result.match_type))

    # Re-sort after boost (stable sort preserves ties)
    boosted.sort(key=lambda r: r.score, reverse=True)

    # Phase 2: greedy interleave enforcing consecutive-run limit.
    # Each candidate is checked against the last _MAX_SAME_SOURCE entries
    # in the output. If adding it would create a run exceeding the limit,
    # it is deferred. Deferred items are re-tried after each successful
    # placement, ensuring they get interleaved as early as possible.
    output: list[IndexResult] = []
    pending = list(boosted)

    def _would_violate(candidate: IndexResult) -> bool:
        """Check if appending candidate would exceed the consecutive limit."""
        repo = candidate.chunk.repo or ""
        path = candidate.chunk.path or ""
        # Check the last _MAX_SAME_SOURCE entries in output
        tail = output[-_MAX_SAME_SOURCE:]
        if len(tail) >= _MAX_SAME_SOURCE:
            if all((r.chunk.repo or "") == repo for r in tail):
                return True
            if all((r.chunk.path or "") == path for r in tail):
                return True
        return False

    max_iterations = len(pending) * 2  # Safety bound
    iteration = 0
    while pending and iteration < max_iterations:
        iteration += 1
        placed = False
        remaining: list[IndexResult] = []
        for result in pending:
            if not _would_violate(result):
                output.append(result)
                placed = True
                # Re-check remaining items from scratch after each placement
                remaining.extend(pending[pending.index(result) + 1 :])
                break
            else:
                remaining.append(result)
        pending = remaining
        if not placed:
            # No item can be placed without violating. Force-place the first
            # one (best score among violators), then retry the rest — the new
            # tail may allow subsequent items from different sources.
            if pending:
                output.append(pending[0])
                pending = pending[1:]
            if not pending:
                break

    return output


def rerank(
    vector_results: list[IndexResult],
    keyword_results: list[IndexResult],
    query: str,
    rrf_k: int = DEFAULT_RRF_K,
    now: datetime | None = None,
    filters: dict[str, object] | None = None,
) -> list[IndexResult]:
    """Full reranking pipeline: RRF merge + heuristics + diversity.

    1. Compute RRF scores across vector and keyword result lists.
    2. Identify dual-hit chunk IDs (found by both backends).
    3. Deduplicate by chunk_id, using RRF scores for winner selection.
    4. Apply code-intent heuristics (symbol boost, dual-hit bonus, staleness).
    5. Apply diversity pass (repo/file diversity, chunk-type preference).
    6. Return sorted results.
    """
    # RRF scoring
    rrf_scores = reciprocal_rank_fusion([vector_results, keyword_results], k=rrf_k)

    # Identify chunks found by both backends (dual-hit = stronger signal)
    vector_ids = {r.chunk.chunk_id for r in vector_results}
    keyword_ids = {r.chunk.chunk_id for r in keyword_results}
    dual_hit_ids = vector_ids & keyword_ids

    # Deduplicate: first-seen wins (vector results first, then keyword).
    # RRF scoring already accounts for rank across both lists, so the
    # choice of which IndexResult to keep is cosmetic (affects match_type).
    seen: dict[str, IndexResult] = {}
    for result in vector_results + keyword_results:
        cid = result.chunk.chunk_id
        if cid not in seen:
            seen[cid] = result

    merged = list(seen.values())
    logger.debug(
        "Rerank: %d vector + %d keyword → %d unique (%d dual-hit)",
        len(vector_results),
        len(keyword_results),
        len(merged),
        len(dual_hit_ids),
    )

    # Apply heuristics then diversity
    heuristic_results = apply_code_intent_heuristics(
        merged, rrf_scores, query, dual_hit_ids=dual_hit_ids, now=now
    )
    return _apply_diversity(heuristic_results, filters=filters)
