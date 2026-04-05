"""Unit tests for version-aware scoring (Phase 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipecat_context_hub.services.retrieval.rerank import (
    COMBINED_PENALTY_CAP,
    STALENESS_DECAY_DAYS,
    STALENESS_MAX_PENALTY,
    VERSION_PENALTY,
    apply_code_intent_heuristics,
    compute_version_compatibility,
)
from pipecat_context_hub.shared.types import ChunkedRecord, IndexResult

NOW = datetime(2026, 4, 5, tzinfo=timezone.utc)


def _make_result(
    chunk_id: str,
    *,
    score: float = 0.5,
    pipecat_version_pin: str | None = None,
    indexed_at: datetime | None = None,
) -> IndexResult:
    meta: dict[str, object] = {}
    if pipecat_version_pin is not None:
        meta["pipecat_version_pin"] = pipecat_version_pin
    return IndexResult(
        chunk=ChunkedRecord(
            chunk_id=chunk_id,
            content="test content",
            content_type="code",
            source_url="https://example.com",
            path="test.py",
            indexed_at=indexed_at or NOW,
            metadata=meta,
        ),
        score=score,
        match_type="vector",
    )


class TestComputeVersionCompatibility:
    """Test compute_version_compatibility()."""

    def test_no_pin_returns_unknown(self) -> None:
        label, penalty = compute_version_compatibility("0.0.95", None)
        assert label == "unknown"
        assert penalty == 0.0

    def test_empty_pin_returns_unknown(self) -> None:
        label, penalty = compute_version_compatibility("0.0.95", "")
        assert label == "unknown"
        assert penalty == 0.0

    def test_compatible_minimum(self) -> None:
        label, penalty = compute_version_compatibility("0.0.110", ">=0.0.105")
        assert label == "compatible"
        assert penalty == 0.0

    def test_incompatible_minimum(self) -> None:
        label, penalty = compute_version_compatibility("0.0.95", ">=0.0.105")
        assert label == "newer_required"
        assert penalty == VERSION_PENALTY

    def test_compatible_exact(self) -> None:
        label, penalty = compute_version_compatibility("0.0.98", "==0.0.98")
        assert label == "compatible"
        assert penalty == 0.0

    def test_incompatible_exact(self) -> None:
        label, penalty = compute_version_compatibility("0.0.95", "==0.0.98")
        assert label == "newer_required"
        assert penalty == VERSION_PENALTY

    def test_compatible_range(self) -> None:
        label, penalty = compute_version_compatibility("0.0.95", "<1,>=0.0.93")
        assert label == "compatible"
        assert penalty == 0.0

    def test_incompatible_range(self) -> None:
        label, penalty = compute_version_compatibility("0.0.90", "<1,>=0.0.93")
        assert label == "newer_required"
        assert penalty == VERSION_PENALTY

    def test_plain_version_compatible(self) -> None:
        """Plain version (from git tag) treated as >=version."""
        label, penalty = compute_version_compatibility("0.0.110", "0.0.108")
        assert label == "compatible"
        assert penalty == 0.0

    def test_plain_version_incompatible(self) -> None:
        """User on older version than the chunk's git tag version."""
        label, penalty = compute_version_compatibility("0.0.95", "0.0.108")
        assert label == "newer_required"
        assert penalty == VERSION_PENALTY

    def test_invalid_user_version(self) -> None:
        label, penalty = compute_version_compatibility("not-a-version", ">=0.0.105")
        assert label == "unknown"
        assert penalty == 0.0

    def test_invalid_specifier(self) -> None:
        label, penalty = compute_version_compatibility("0.0.95", ">>>bad")
        assert label == "unknown"
        assert penalty == 0.0


class TestVersionScoringInHeuristics:
    """Test version penalty integration in apply_code_intent_heuristics."""

    def test_compatible_no_penalty(self) -> None:
        r1 = _make_result("a", pipecat_version_pin=">=0.0.90")
        rrf_scores = {"a": 0.5}
        results, compat = apply_code_intent_heuristics(
            [r1], rrf_scores, "test", now=NOW, pipecat_version="0.0.95"
        )
        assert results[0].score == pytest.approx(0.5)
        assert compat["a"] == "compatible"

    def test_incompatible_penalized(self) -> None:
        r1 = _make_result("a", pipecat_version_pin=">=0.0.105")
        rrf_scores = {"a": 0.5}
        results, compat = apply_code_intent_heuristics(
            [r1], rrf_scores, "test", now=NOW, pipecat_version="0.0.95"
        )
        assert results[0].score == pytest.approx(0.5 - VERSION_PENALTY)
        assert compat["a"] == "newer_required"

    def test_no_version_param_no_penalty(self) -> None:
        """When pipecat_version is None, no version penalty applied."""
        r1 = _make_result("a", pipecat_version_pin=">=0.0.105")
        rrf_scores = {"a": 0.5}
        results, compat = apply_code_intent_heuristics(
            [r1], rrf_scores, "test", now=NOW, pipecat_version=None
        )
        assert results[0].score == pytest.approx(0.5)
        assert compat == {}

    def test_combined_cap_staleness_plus_version(self) -> None:
        """staleness + version penalty capped at COMBINED_PENALTY_CAP."""
        old_date = NOW - timedelta(days=500)  # max staleness = 0.10
        r1 = _make_result("a", pipecat_version_pin=">=0.0.105", indexed_at=old_date)
        rrf_scores = {"a": 0.5}
        results, compat = apply_code_intent_heuristics(
            [r1], rrf_scores, "test", now=NOW, pipecat_version="0.0.95"
        )
        # Without cap: 0.10 (staleness) + 0.05 (version) = 0.15
        # With cap: 0.10
        assert results[0].score == pytest.approx(0.5 - COMBINED_PENALTY_CAP)
        assert compat["a"] == "newer_required"

    def test_combined_cap_partial_staleness(self) -> None:
        """Partial staleness + version should not exceed cap."""
        half_year = NOW - timedelta(days=180)
        staleness = min(STALENESS_MAX_PENALTY, 180 / STALENESS_DECAY_DAYS * STALENESS_MAX_PENALTY)
        r1 = _make_result("a", pipecat_version_pin=">=0.0.105", indexed_at=half_year)
        rrf_scores = {"a": 0.5}
        results, _ = apply_code_intent_heuristics(
            [r1], rrf_scores, "test", now=NOW, pipecat_version="0.0.95"
        )
        # staleness (~0.049) + version (0.05) = ~0.099, under cap of 0.10
        expected = min(staleness + VERSION_PENALTY, COMBINED_PENALTY_CAP)
        assert results[0].score == pytest.approx(0.5 - expected)

    def test_relevant_incompatible_beats_irrelevant_compatible(self) -> None:
        """A highly relevant older example should still rank above an irrelevant newer one."""
        relevant = _make_result("rel", score=0.9, pipecat_version_pin=">=0.0.105")
        irrelevant = _make_result("irr", score=0.3, pipecat_version_pin=">=0.0.90")
        rrf_scores = {"rel": 0.8, "irr": 0.2}
        results, compat = apply_code_intent_heuristics(
            [relevant, irrelevant], rrf_scores, "test", now=NOW, pipecat_version="0.0.95"
        )
        # rel: 0.8 - 0.05 = 0.75, irr: 0.2
        assert results[0].chunk.chunk_id == "rel"
        assert compat["rel"] == "newer_required"
        assert compat["irr"] == "compatible"

    def test_compat_map_in_rerank(self) -> None:
        """Full rerank() pipeline returns compat_map."""
        from pipecat_context_hub.services.retrieval.rerank import rerank

        v1 = _make_result("a", score=0.9, pipecat_version_pin=">=0.0.105")
        k1 = _make_result("a", score=0.8, pipecat_version_pin=">=0.0.105")
        results, compat = rerank([v1], [k1], "test", now=NOW, pipecat_version="0.0.95")
        assert "a" in compat
        assert compat["a"] == "newer_required"
