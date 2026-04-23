"""Regression tests for ``_build_chunk_metadata`` taxonomy handling.

Phase 3 of the topic-based examples layout work. These tests lock in the
invariant that ``_build_chunk_metadata`` omits the ``foundational_class``
metadata key when the underlying ``TaxonomyEntry`` carries
``foundational_class=None`` — the common case for the current topic-based
``examples/<topic>/<example>/`` pipecat layout.

The invariant is load-bearing because:

- Persisted ChromaDB entries from older indexes may still carry
  ``foundational_class`` strings; the hybrid-retrieval filter path
  (``hybrid.py:370-371``) treats that field as optional and only filters when
  callers pass a non-``None`` value.
- Newly ingested topic-layout examples must therefore **not** write a stale
  placeholder (e.g. an empty string or the literal ``"None"``) — the key must
  be absent.
"""

from __future__ import annotations

from pipecat_context_hub.services.ingest.github_ingest import _build_chunk_metadata
from pipecat_context_hub.shared.types import CapabilityTag, TaxonomyEntry


def _make_entry(foundational_class: str | None) -> TaxonomyEntry:
    return TaxonomyEntry(
        example_id="voice/twilio-chatbot",
        repo="pipecat-ai/pipecat",
        path="examples/voice/twilio-chatbot",
        foundational_class=foundational_class,
        capabilities=[CapabilityTag(name="voice", source="directory")],
        key_files=["bot.py"],
    )


def test_metadata_omits_foundational_class_when_entry_has_none() -> None:
    """Topic-layout entries must not emit a ``foundational_class`` meta key."""

    entry = _make_entry(foundational_class=None)

    meta = _build_chunk_metadata(
        repo_slug="pipecat-ai/pipecat",
        commit_sha="deadbeef",
        chunk_index=0,
        language="python",
        line_start=1,
        line_end=10,
        rel_path="examples/voice/twilio-chatbot/bot.py",
        taxonomy_entry=entry,
    )

    assert "foundational_class" not in meta
    # Capability tags should still propagate — proves we didn't accidentally
    # short-circuit the whole taxonomy block.
    assert meta.get("capability_tags") == ["voice"]
    assert meta.get("key_files") == ["bot.py"]


def test_metadata_includes_foundational_class_for_legacy_entries() -> None:
    """Legacy ``examples/foundational/`` entries keep the field for back-compat."""

    entry = _make_entry(foundational_class="07-interruptible")

    meta = _build_chunk_metadata(
        repo_slug="pipecat-ai/pipecat",
        commit_sha="deadbeef",
        chunk_index=0,
        language="python",
        line_start=1,
        line_end=10,
        rel_path="examples/foundational/07-interruptible/bot.py",
        taxonomy_entry=entry,
    )

    assert meta.get("foundational_class") == "07-interruptible"


def test_metadata_without_taxonomy_entry_has_no_taxonomy_fields() -> None:
    """No taxonomy entry → no taxonomy-derived keys."""

    meta = _build_chunk_metadata(
        repo_slug="pipecat-ai/pipecat",
        commit_sha="deadbeef",
        chunk_index=0,
        language="python",
        line_start=1,
        line_end=10,
        rel_path="examples/voice/twilio-chatbot/bot.py",
        taxonomy_entry=None,
    )

    assert "foundational_class" not in meta
    assert "capability_tags" not in meta
    assert "key_files" not in meta
    assert "execution_mode" not in meta
