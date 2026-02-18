"""Tests for the TaxonomyBuilder — automatic taxonomy extraction from example dirs."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipecat_context_hub.services.ingest.taxonomy import (
    TaxonomyBuilder,
    _dedup_tags,
    _extract_summary_from_readme,
    _infer_tags_from_code,
    _infer_tags_from_directory_name,
    _infer_tags_from_readme,
)
from pipecat_context_hub.shared.types import CapabilityTag, TaxonomyEntry


# ---------------------------------------------------------------------------
# Fixtures — temp directory structures mimicking real repos
# ---------------------------------------------------------------------------


@pytest.fixture
def foundational_dir(tmp_path: Path) -> Path:
    """Create a mock ``examples/foundational/`` tree with several examples."""
    base = tmp_path / "examples" / "foundational"

    # 01-say-one-thing: minimal example with a Python file
    ex01 = base / "01-say-one-thing"
    ex01.mkdir(parents=True)
    (ex01 / "bot.py").write_text(
        "from pipecat.services.elevenlabs import ElevenLabsTTSService\n"
        "from pipecat.pipeline.pipeline import Pipeline\n"
        "async def main():\n"
        "    pipeline = Pipeline()\n"
    )
    (ex01 / "README.md").write_text(
        "# Say One Thing\n\nA minimal example that says a single phrase via text-to-speech.\n"
    )

    # 07-interruptible: example with interruptible keyword
    ex07 = base / "07-interruptible"
    ex07.mkdir(parents=True)
    (ex07 / "bot.py").write_text(
        "from pipecat.transports.daily import DailyTransport\n"
        "from pipecat.services.deepgram import DeepgramSTTService\n"
        "from pipecat.services.openai import OpenAILLMService\n"
        "class MyBot:\n"
        "    pass\n"
    )
    (ex07 / "README.md").write_text(
        "# Interruptible Bot\n\n"
        "Demonstrates how to handle user interrupts during speech.\n"
        "Uses voice activity detection.\n"
    )

    # 13-whisper: example showcasing whisper
    ex13 = base / "13-whisper"
    ex13.mkdir(parents=True)
    (ex13 / "bot.py").write_text(
        "from pipecat.services.whisper import WhisperSTTService\n"
        "stt = WhisperSTTService()\n"
    )

    return base


@pytest.fixture
def examples_repo_dir(tmp_path: Path) -> Path:
    """Create a mock pipecat-examples repo root.

    Uses a subdirectory of tmp_path so it can coexist with foundational_dir
    in the same test without directory collisions.
    """
    root = tmp_path / "pipecat-examples"
    root.mkdir()

    # chatbot example
    chatbot = root / "chatbot"
    chatbot.mkdir()
    (chatbot / "main.py").write_text(
        "from pipecat.services.openai import OpenAILLMService\n"
        "from pipecat.services.cartesia import CartesiaTTSService\n"
    )
    (chatbot / "README.md").write_text(
        "# Chatbot\n\nA conversational chatbot using LLM and TTS pipeline.\n"
    )
    (chatbot / "requirements.txt").write_text("pipecat-ai\n")

    # storytelling example (no README)
    story = root / "storytelling"
    story.mkdir()
    (story / "app.py").write_text(
        "from pipecat.pipeline.pipeline import Pipeline\n"
        "from pipecat.services.anthropic import AnthropicLLMService\n"
    )

    # hidden dir — should be skipped
    hidden = root / ".git"
    hidden.mkdir()
    (hidden / "config").write_text("core.bare=false\n")

    # __pycache__ — should be skipped
    pycache = root / "__pycache__"
    pycache.mkdir()

    return root


@pytest.fixture
def full_repo_dir(tmp_path: Path) -> Path:
    """Create a mock pipecat main repo root with examples/foundational/ inside."""
    base = tmp_path / "examples" / "foundational"
    ex = base / "01-hello"
    ex.mkdir(parents=True)
    (ex / "bot.py").write_text(
        "from pipecat.pipeline.pipeline import Pipeline\n"
        "Pipeline()\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


class TestInferTagsFromDirectoryName:
    def test_interruptible(self):
        tags = _infer_tags_from_directory_name("07-interruptible")
        names = {t.name for t in tags}
        assert "interruptible" in names
        assert all(t.source == "directory" for t in tags)
        assert all(t.confidence == 1.0 for t in tags)

    def test_whisper(self):
        tags = _infer_tags_from_directory_name("13-whisper")
        names = {t.name for t in tags}
        assert "whisper" in names

    def test_no_match(self):
        tags = _infer_tags_from_directory_name("01-say-one-thing")
        # "say-one-thing" doesn't match any fragment
        assert tags == []

    def test_multiple_matches(self):
        tags = _infer_tags_from_directory_name("stt-whisper-pipeline")
        names = {t.name for t in tags}
        assert "stt" in names
        assert "whisper" in names
        assert "pipeline" in names


class TestInferTagsFromReadme:
    def test_interrupt_and_vad(self):
        text = "This example handles user interrupts using voice activity detection."
        tags = _infer_tags_from_readme(text)
        names = {t.name for t in tags}
        assert "interruptible" in names
        assert "vad" in names
        assert all(t.source == "readme" for t in tags)
        assert all(t.confidence == 0.8 for t in tags)

    def test_no_match(self):
        tags = _infer_tags_from_readme("Just a simple hello world example.")
        assert tags == []

    def test_function_calling(self):
        text = "Demonstrates function calling with the LLM."
        tags = _infer_tags_from_readme(text)
        names = {t.name for t in tags}
        assert "function-calling" in names
        assert "llm" in names


class TestInferTagsFromCode:
    def test_imports(self):
        code = (
            "from pipecat.services.elevenlabs import ElevenLabsTTSService\n"
            "from pipecat.services.deepgram import DeepgramSTTService\n"
        )
        tags = _infer_tags_from_code(code)
        names = {t.name for t in tags}
        assert "elevenlabs" in names
        assert "deepgram" in names
        assert all(t.source == "code" for t in tags)
        assert all(t.confidence == 0.9 for t in tags if t.source == "code" and t.name in ("elevenlabs", "deepgram"))

    def test_class_references(self):
        code = "pipeline = Pipeline()\ntts = SomeTTSService()\n"
        tags = _infer_tags_from_code(code)
        names = {t.name for t in tags}
        assert "pipeline" in names
        assert "tts" in names

    def test_dedup_within_code(self):
        code = (
            "from pipecat.services.openai import OpenAILLMService\n"
            "from pipecat.services.openai import OpenAITTSService\n"
        )
        tags = _infer_tags_from_code(code)
        openai_tags = [t for t in tags if t.name == "openai"]
        # Should be deduped within _infer_tags_from_code
        assert len(openai_tags) == 1


class TestExtractSummaryFromReadme:
    def test_basic(self):
        text = "# Hello\n\nThis is a summary paragraph.\n\nMore text.\n"
        summary = _extract_summary_from_readme(text)
        assert summary == "This is a summary paragraph."

    def test_multi_line_paragraph(self):
        text = "# Title\n\nFirst line.\nSecond line.\n\nAnother paragraph.\n"
        summary = _extract_summary_from_readme(text)
        assert summary == "First line. Second line."

    def test_truncation(self):
        text = "# Title\n\n" + "A" * 300 + "\n"
        summary = _extract_summary_from_readme(text)
        assert len(summary) <= 200
        assert summary.endswith("...")

    def test_empty(self):
        summary = _extract_summary_from_readme("")
        assert summary == ""

    def test_headings_only(self):
        text = "# Heading\n## Subheading\n"
        summary = _extract_summary_from_readme(text)
        assert summary == ""


class TestDedupTags:
    def test_keeps_highest_confidence(self):
        tags = [
            CapabilityTag(name="tts", confidence=0.8, source="readme"),
            CapabilityTag(name="tts", confidence=0.9, source="code"),
        ]
        result = _dedup_tags(tags)
        assert len(result) == 1
        assert result[0].confidence == 0.9
        assert result[0].source == "code"

    def test_same_confidence_prefers_code(self):
        tags = [
            CapabilityTag(name="pipeline", confidence=1.0, source="directory"),
            CapabilityTag(name="pipeline", confidence=1.0, source="code"),
        ]
        result = _dedup_tags(tags)
        assert len(result) == 1
        assert result[0].source == "code"

    def test_preserves_distinct_tags(self):
        tags = [
            CapabilityTag(name="tts", confidence=0.9, source="code"),
            CapabilityTag(name="stt", confidence=0.8, source="readme"),
        ]
        result = _dedup_tags(tags)
        assert len(result) == 2

    def test_sorted_by_confidence_then_name(self):
        tags = [
            CapabilityTag(name="zzz", confidence=1.0, source="code"),
            CapabilityTag(name="aaa", confidence=1.0, source="code"),
            CapabilityTag(name="mmm", confidence=0.5, source="readme"),
        ]
        result = _dedup_tags(tags)
        assert [t.name for t in result] == ["aaa", "zzz", "mmm"]


# ---------------------------------------------------------------------------
# TaxonomyBuilder integration tests (with mock directories)
# ---------------------------------------------------------------------------


class TestTaxonomyBuilderFoundational:
    def test_builds_entries_from_foundational(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir, repo="pipecat-ai/pipecat")
        assert len(entries) == 3

    def test_entry_ids(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        ids = {e.example_id for e in entries}
        assert "foundational-01-say-one-thing" in ids
        assert "foundational-07-interruptible" in ids
        assert "foundational-13-whisper" in ids

    def test_foundational_class_set(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        for entry in entries:
            assert entry.foundational_class is not None
            # foundational_class should be the directory name for numbered dirs
            assert entry.foundational_class == entry.path.split("/")[-1]

    def test_path_format(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        for entry in entries:
            assert entry.path.startswith("examples/foundational/")

    def test_capabilities_inferred(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        entry_map = {e.example_id: e for e in entries}

        # 07-interruptible should have interruptible, daily, deepgram, openai
        e07 = entry_map["foundational-07-interruptible"]
        tag_names = {t.name for t in e07.capabilities}
        assert "interruptible" in tag_names
        assert "daily" in tag_names
        assert "deepgram" in tag_names

        # 13-whisper should have whisper
        e13 = entry_map["foundational-13-whisper"]
        tag_names = {t.name for t in e13.capabilities}
        assert "whisper" in tag_names

    def test_summary_extracted(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        entry_map = {e.example_id: e for e in entries}

        assert "minimal" in entry_map["foundational-01-say-one-thing"].summary.lower()

    def test_readme_content_stored(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        entry_map = {e.example_id: e for e in entries}

        assert entry_map["foundational-01-say-one-thing"].readme_content is not None
        assert "Say One Thing" in (entry_map["foundational-01-say-one-thing"].readme_content or "")
        assert entry_map["foundational-13-whisper"].readme_content is None

    def test_key_files_detected(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        entry_map = {e.example_id: e for e in entries}

        assert "bot.py" in entry_map["foundational-01-say-one-thing"].key_files
        assert "README.md" in entry_map["foundational-01-say-one-thing"].key_files

    def test_commit_sha_propagated(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(
            foundational_dir, commit_sha="abc123"
        )
        for entry in entries:
            assert entry.commit_sha == "abc123"

    def test_indexed_at_set(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        for entry in entries:
            assert entry.indexed_at is not None

    def test_empty_dir(self, tmp_path: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(tmp_path)
        assert entries == []

    def test_nonexistent_dir(self, tmp_path: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(tmp_path / "nonexistent")
        assert entries == []

    def test_entries_are_taxonomy_entry(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        for entry in entries:
            assert isinstance(entry, TaxonomyEntry)

    def test_repo_field(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir, repo="my/repo")
        for entry in entries:
            assert entry.repo == "my/repo"


class TestTaxonomyBuilderExamplesRepo:
    def test_builds_entries(self, examples_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_examples_repo(examples_repo_dir)
        # Should include chatbot and storytelling, not .git or __pycache__
        assert len(entries) == 2

    def test_skips_hidden_and_pycache(self, examples_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_examples_repo(examples_repo_dir)
        ids = {e.example_id for e in entries}
        assert "example-.git" not in ids
        assert "example-__pycache__" not in ids

    def test_no_foundational_class(self, examples_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_examples_repo(examples_repo_dir)
        for entry in entries:
            assert entry.foundational_class is None

    def test_capabilities_from_code(self, examples_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_examples_repo(examples_repo_dir)
        entry_map = {e.example_id: e for e in entries}

        chatbot = entry_map["example-chatbot"]
        tag_names = {t.name for t in chatbot.capabilities}
        assert "openai" in tag_names
        assert "cartesia" in tag_names

    def test_capabilities_from_directory(self, examples_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_examples_repo(examples_repo_dir)
        entry_map = {e.example_id: e for e in entries}

        chatbot = entry_map["example-chatbot"]
        tag_names = {t.name for t in chatbot.capabilities}
        assert "chatbot" in tag_names

    def test_summary_from_readme(self, examples_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_examples_repo(examples_repo_dir)
        entry_map = {e.example_id: e for e in entries}

        assert "conversational" in entry_map["example-chatbot"].summary.lower()
        assert entry_map["example-storytelling"].summary == ""

    def test_key_files(self, examples_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_examples_repo(examples_repo_dir)
        entry_map = {e.example_id: e for e in entries}

        assert "main.py" in entry_map["example-chatbot"].key_files
        assert "requirements.txt" in entry_map["example-chatbot"].key_files

    def test_empty_dir(self, tmp_path: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_examples_repo(tmp_path)
        assert entries == []


class TestTaxonomyBuilderBuildFromDirectory:
    def test_auto_detects_foundational(self, full_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(full_repo_dir, repo="pipecat-ai/pipecat")
        assert len(entries) == 1
        assert entries[0].foundational_class == "01-hello"

    def test_falls_back_to_examples(self, examples_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            examples_repo_dir, repo="pipecat-ai/pipecat-examples"
        )
        assert len(entries) == 2
        for entry in entries:
            assert entry.foundational_class is None


class TestTaxonomyBuilderQueryMethods:
    def test_query_by_class(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        builder.build_from_foundational(foundational_dir)

        results = builder.query_by_class("07-interruptible")
        assert len(results) == 1
        assert results[0].example_id == "foundational-07-interruptible"

    def test_query_by_class_no_match(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        builder.build_from_foundational(foundational_dir)

        results = builder.query_by_class("99-nonexistent")
        assert results == []

    def test_query_by_tag(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        builder.build_from_foundational(foundational_dir)

        results = builder.query_by_tag("whisper")
        assert len(results) >= 1
        assert any(e.example_id == "foundational-13-whisper" for e in results)

    def test_query_by_tag_no_match(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        builder.build_from_foundational(foundational_dir)

        results = builder.query_by_tag("nonexistent-tag")
        assert results == []

    def test_query_by_example_id(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        builder.build_from_foundational(foundational_dir)

        result = builder.query_by_example_id("foundational-07-interruptible")
        assert result is not None
        assert result.foundational_class == "07-interruptible"

    def test_query_by_example_id_not_found(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        builder.build_from_foundational(foundational_dir)

        result = builder.query_by_example_id("nonexistent")
        assert result is None

    def test_entries_property(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        assert builder.entries == entries

    def test_entries_property_returns_copy(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        builder.build_from_foundational(foundational_dir)
        entries = builder.entries
        entries.clear()
        # Original should be unaffected
        assert len(builder.entries) == 3

    def test_clear(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        builder.build_from_foundational(foundational_dir)
        assert len(builder.entries) > 0
        builder.clear()
        assert builder.entries == []

    def test_accumulates_across_builds(
        self, foundational_dir: Path, examples_repo_dir: Path
    ):
        builder = TaxonomyBuilder()
        builder.build_from_foundational(foundational_dir)
        builder.build_from_examples_repo(examples_repo_dir)
        # 3 foundational + 2 examples
        assert len(builder.entries) == 5

    def test_query_by_tag_across_repos(
        self, foundational_dir: Path, examples_repo_dir: Path
    ):
        builder = TaxonomyBuilder()
        builder.build_from_foundational(foundational_dir, repo="pipecat-ai/pipecat")
        builder.build_from_examples_repo(
            examples_repo_dir, repo="pipecat-ai/pipecat-examples"
        )

        # "openai" should appear in both repos
        results = builder.query_by_tag("openai")
        repos = {e.repo for e in results}
        assert "pipecat-ai/pipecat" in repos
        assert "pipecat-ai/pipecat-examples" in repos


class TestTaxonomyEntryRoundTrip:
    """Verify that taxonomy entries produced by the builder round-trip through JSON."""

    def test_round_trip(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        for entry in entries:
            json_str = entry.model_dump_json()
            rebuilt = TaxonomyEntry.model_validate_json(json_str)
            assert rebuilt == entry
