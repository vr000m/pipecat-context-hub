# Multi-Concept Query Decomposition

## Header
- **Status:** In Progress
- **Type:** feature
- **Assignee:** vr000m
- **Priority:** Medium
- **Working Branch:** feature/multi-concept-search
- **Created:** 2026-02-28
- **PR:** #7

## Problem

Compound queries like "idle timeout + function calling + Gemini" return poor results — the single embedding matches no chunk well, and all top-N results cluster around whichever concept dominates the embedding space.

## Solution

When explicit delimiters (` + ` or ` & `) are detected, split the query into sub-concepts, run per-concept searches in parallel, and interleave results for balanced coverage. Single-concept queries are completely unchanged.

### Supported Delimiters

Only ` + ` and ` & ` (with surrounding whitespace). Comma and "and" were intentionally excluded — they produce false positives on natural language queries like "error handling, logging" or "search and replace".

### Implementation

| File | Change |
|------|--------|
| `src/pipecat_context_hub/services/retrieval/decompose.py` | New module: `decompose_query()` pure function |
| `src/pipecat_context_hub/services/retrieval/hybrid.py` | Refactored `_hybrid_search` → dispatcher + `_single_concept_search` + `_multi_concept_search` |
| `tests/unit/test_retrieval.py` | 16 new tests for decomposition and multi-concept search |

### Key Design Decisions

- **Delimiters:** Only ` + ` and ` & ` — zero false positives ("C++", "AT&T" are safe)
- **Parallel execution:** Per-concept searches run via `asyncio.gather` (~360ms for 3 concepts)
- **Interleaving:** Round-robin across concepts with deduplication by chunk_id
- **Limit allocation:** Ceiling division (`-(-limit // n)`) ensures enough candidates
- **Small limit guard:** When `limit < n`, falls back to single-concept search to avoid over-fetching
- **Test mocks:** Dispatch by query text (not call order) for deterministic `asyncio.gather` behavior

## Review Fixes

| Finding | Severity | Fix |
|---------|----------|-----|
| Comma delimiter false positives | P1 | Removed comma from delimiters |
| "and" delimiter false positives | P1 | Removed "and" from delimiters |
| Order-dependent test mocks | P2 | Dispatch by `query.query_text` instead of `call_count` |
| Over-fetch when `limit < n` | P2 | Fall back to single-concept search |
| Ampersand splitting "AT&T" | P2 | Changed `\s*&\s*` to `\s+&\s+` |
| `assert_called_once` without args | P3 | Added `query_text` verification |
| Floor division under-fill | P2 | Changed to ceiling division |
