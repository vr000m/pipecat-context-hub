"""Multi-concept query decomposition.

Splits compound queries like "idle timeout + function calling + Gemini"
into individual sub-queries for per-concept retrieval.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Delimiters that signal concept boundaries.  Only ` + ` and ` & ` are
# supported — they are unambiguous intent signals.  Comma and "and" were
# removed because they produce false positives on natural-language queries
# ("error handling, logging" or "search and replace").
_DELIMITERS = re.compile(r"\s+\+\s+|\s+&\s+")

# Maximum sub-concepts to bound cost.
MAX_CONCEPTS = 5

# Minimum length for a sub-concept to be valid.
MIN_CONCEPT_LENGTH = 2


def decompose_query(query: str) -> list[str] | None:
    """Split a query into sub-concepts if explicit delimiters are present.

    Returns ``None`` if the query is a single concept (no decomposition).
    Returns a list of 2+ sub-query strings when decomposition is applied.
    """
    parts = _DELIMITERS.split(query)
    concepts = [p.strip() for p in parts if len(p.strip()) >= MIN_CONCEPT_LENGTH]

    if len(concepts) <= 1:
        return None

    if len(concepts) > MAX_CONCEPTS:
        logger.debug(
            "decompose_query: capping %d concepts to %d",
            len(concepts),
            MAX_CONCEPTS,
        )
        concepts = concepts[:MAX_CONCEPTS]

    logger.debug("decompose_query: %r -> %r", query, concepts)
    return concepts
