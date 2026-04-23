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


class TestTaxonomyBuilderFlatFoundational:
    """Tests for flat .py file foundational examples (not subdirectories)."""

    @pytest.fixture
    def flat_foundational_dir(self, tmp_path: Path) -> Path:
        """Create a mock foundational dir with flat .py files (no subdirs)."""
        base = tmp_path / "examples" / "foundational"
        base.mkdir(parents=True)

        (base / "01-say-one-thing.py").write_text(
            "from pipecat.services.elevenlabs import ElevenLabsTTSService\n"
            "from pipecat.pipeline.pipeline import Pipeline\n"
            "async def main():\n"
            "    pipeline = Pipeline()\n"
        )
        (base / "07-interruptible.py").write_text(
            "from pipecat.transports.daily import DailyTransport\n"
            "from pipecat.services.deepgram import DeepgramSTTService\n"
            "class MyBot:\n"
            "    pass\n"
        )
        (base / "13-whisper.py").write_text(
            "from pipecat.services.whisper import WhisperSTTService\n"
            "stt = WhisperSTTService()\n"
        )
        return base

    def test_builds_entries_from_flat_files(self, flat_foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(flat_foundational_dir)
        assert len(entries) == 3

    def test_entry_ids(self, flat_foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(flat_foundational_dir)
        ids = {e.example_id for e in entries}
        assert "foundational-01-say-one-thing" in ids
        assert "foundational-07-interruptible" in ids
        assert "foundational-13-whisper" in ids

    def test_foundational_class_set(self, flat_foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(flat_foundational_dir)
        for entry in entries:
            assert entry.foundational_class is not None

    def test_path_includes_extension(self, flat_foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(flat_foundational_dir)
        for entry in entries:
            assert entry.path.startswith("examples/foundational/")
            assert entry.path.endswith(".py")

    def test_capabilities_inferred_from_code(self, flat_foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(flat_foundational_dir)
        entry_map = {e.example_id: e for e in entries}

        e07 = entry_map["foundational-07-interruptible"]
        tag_names = {t.name for t in e07.capabilities}
        assert "daily" in tag_names
        assert "deepgram" in tag_names

        e13 = entry_map["foundational-13-whisper"]
        tag_names = {t.name for t in e13.capabilities}
        assert "whisper" in tag_names

    def test_no_readme_for_flat_files(self, flat_foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(flat_foundational_dir)
        for entry in entries:
            assert entry.readme_content is None
            assert entry.summary == ""

    def test_key_files_is_filename(self, flat_foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(flat_foundational_dir)
        entry_map = {e.example_id: e for e in entries}
        assert entry_map["foundational-01-say-one-thing"].key_files == ["01-say-one-thing.py"]

    def test_build_from_directory_auto_detects_flat(self, tmp_path: Path):
        """build_from_directory handles flat foundational layout."""
        base = tmp_path / "examples" / "foundational"
        base.mkdir(parents=True)
        (base / "01-hello.py").write_text("from pipecat.pipeline import Pipeline\n")

        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(tmp_path, repo="pipecat-ai/pipecat")
        assert len(entries) == 1
        assert entries[0].foundational_class == "01-hello"
        assert entries[0].path == "examples/foundational/01-hello.py"


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


class TestBuildEntryForRepoRoot:
    """Tests for build_entry_for_repo_root (single-project repos)."""

    def test_path_is_dot(self, tmp_path: Path):
        """Root entry uses path '.' for ingester lookup compatibility."""
        (tmp_path / "main.py").write_text("from pipecat.pipeline import Pipeline\n")
        builder = TaxonomyBuilder()
        entry = builder.build_entry_for_repo_root(tmp_path, repo="org/repo")
        assert entry.path == "."

    def test_capabilities_inferred_from_code(self, tmp_path: Path):
        """Root entry scans all .py files recursively for tags."""
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        (src / "server.py").write_text(
            "from pipecat.transports.daily import DailyTransport\n"
            "from pipecat.services.deepgram import DeepgramSTTService\n"
        )
        builder = TaxonomyBuilder()
        entry = builder.build_entry_for_repo_root(tmp_path, repo="org/repo")
        tag_names = {t.name for t in entry.capabilities}
        assert "daily" in tag_names
        assert "deepgram" in tag_names

    def test_readme_captured(self, tmp_path: Path):
        """Root README is captured in the entry."""
        (tmp_path / "main.py").write_text("pass\n")
        (tmp_path / "README.md").write_text(
            "# My Bot\n\nA voice agent for local use.\n"
        )
        builder = TaxonomyBuilder()
        entry = builder.build_entry_for_repo_root(tmp_path, repo="org/repo")
        assert entry.readme_content is not None
        assert "voice agent" in entry.summary.lower()

    def test_accumulated_in_entries(self, tmp_path: Path):
        """Root entry is added to builder.entries."""
        (tmp_path / "main.py").write_text("pass\n")
        builder = TaxonomyBuilder()
        builder.build_entry_for_repo_root(tmp_path, repo="org/repo")
        assert len(builder.entries) == 1
        assert builder.entries[0].path == "."

    def test_commit_sha_propagated(self, tmp_path: Path):
        (tmp_path / "main.py").write_text("pass\n")
        builder = TaxonomyBuilder()
        entry = builder.build_entry_for_repo_root(
            tmp_path, repo="org/repo", commit_sha="abc123"
        )
        assert entry.commit_sha == "abc123"


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


class TestTaxonomyBuilderMixedLayout:
    """Tests for repos with both examples/foundational/ and sibling example dirs."""

    @pytest.fixture
    def mixed_repo_dir(self, tmp_path: Path) -> Path:
        """Create a repo with examples/foundational/ + examples/quickstart/."""
        # Foundational example
        foundational = tmp_path / "examples" / "foundational" / "01-hello"
        foundational.mkdir(parents=True)
        (foundational / "bot.py").write_text(
            "from pipecat.pipeline.pipeline import Pipeline\n"
            "Pipeline()\n"
        )
        (foundational / "README.md").write_text("# Hello\n\nA hello example.\n")

        # Non-foundational sibling: quickstart
        quickstart = tmp_path / "examples" / "quickstart"
        quickstart.mkdir(parents=True)
        (quickstart / "main.py").write_text(
            "from pipecat.services.openai import OpenAILLMService\n"
            "from pipecat.services.deepgram import DeepgramSTTService\n"
        )
        (quickstart / "README.md").write_text(
            "# Quickstart\n\nGet started quickly with Pipecat.\n"
        )

        # Non-foundational sibling: websocket-demo
        ws = tmp_path / "examples" / "websocket-demo"
        ws.mkdir(parents=True)
        (ws / "server.py").write_text(
            "from pipecat.transports.websocket import WebSocketTransport\n"
        )

        # Dirs that should be skipped
        (tmp_path / "examples" / ".hidden").mkdir()
        (tmp_path / "examples" / "__pycache__").mkdir()

        return tmp_path

    def test_returns_both_foundational_and_non_foundational(self, mixed_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(mixed_repo_dir, repo="pipecat-ai/pipecat")
        # 1 foundational + 2 non-foundational
        assert len(entries) == 3

    def test_foundational_entries_have_foundational_class(self, mixed_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(mixed_repo_dir, repo="pipecat-ai/pipecat")
        foundational = [e for e in entries if e.foundational_class is not None]
        assert len(foundational) == 1
        assert foundational[0].foundational_class == "01-hello"
        assert foundational[0].path == "examples/foundational/01-hello"

    def test_non_foundational_paths_include_examples_prefix(self, mixed_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(mixed_repo_dir, repo="pipecat-ai/pipecat")
        non_foundational = [e for e in entries if e.foundational_class is None]
        paths = {e.path for e in non_foundational}
        assert "examples/quickstart" in paths
        assert "examples/websocket-demo" in paths

    def test_non_foundational_capabilities_inferred(self, mixed_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(mixed_repo_dir, repo="pipecat-ai/pipecat")
        entry_map = {e.path: e for e in entries}

        qs = entry_map["examples/quickstart"]
        qs_tags = {t.name for t in qs.capabilities}
        assert "openai" in qs_tags
        assert "deepgram" in qs_tags

        ws = entry_map["examples/websocket-demo"]
        ws_tags = {t.name for t in ws.capabilities}
        assert "websocket" in ws_tags

    def test_skips_hidden_and_pycache(self, mixed_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(mixed_repo_dir, repo="pipecat-ai/pipecat")
        paths = {e.path for e in entries}
        assert "examples/.hidden" not in paths
        assert "examples/__pycache__" not in paths

    def test_entries_accumulated(self, mixed_repo_dir: Path):
        builder = TaxonomyBuilder()
        builder.build_from_directory(mixed_repo_dir, repo="pipecat-ai/pipecat")
        assert len(builder.entries) == 3

    def test_taxonomy_lookup_matches_find_example_dirs(self, mixed_repo_dir: Path):
        """Verify taxonomy paths match what _find_example_dirs produces.

        This is the integration seam that caused the P1 bug — the ingester
        discovers dirs via _find_example_dirs and looks them up by relative
        path in the taxonomy. Both sides must agree on path format.
        """
        from pipecat_context_hub.services.ingest.github_ingest import _find_example_dirs

        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            mixed_repo_dir, repo="pipecat-ai/pipecat", commit_sha="abc123"
        )
        lookup = {e.path: e for e in entries}

        example_dirs = _find_example_dirs(mixed_repo_dir)
        for ex_dir in example_dirs:
            rel_path = str(ex_dir.relative_to(mixed_repo_dir))
            assert rel_path in lookup, (
                f"Taxonomy has no entry for {rel_path!r}; "
                f"available: {sorted(lookup.keys())}"
            )


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


class TestTaxonomyBuilderTopicLayout:
    """Phase 1: topic-based examples layout (post-foundational reorg).

    Covers:
    (a) topic-based tree with subdir examples under multiple topics
    (b) topic-based tree where one topic contains flat ``.py`` files
    (c) legacy ``foundational/`` tree still works unchanged
    (d) mixed layout: ``foundational/`` + ``simple-chatbot/`` siblings (v0.0.96)
    (e) ``pipecat-examples``-style root-level layout still works
    (f) repo root with no ``examples/`` dir falls back correctly
    (g) Lookup-key parity vs ``_discover_under_examples``
    """

    # -- fixtures ----------------------------------------------------------

    @pytest.fixture
    def topic_repo_dir(self, tmp_path: Path) -> Path:
        """Topic-based layout: ``examples/<topic>/<example>/``.

        Mirrors post-reorg pipecat main: multiple topic dirs, each with
        one or more example subdirs.
        """
        examples = tmp_path / "examples"

        # function-calling/weather — subdir example
        fc = examples / "function-calling" / "weather"
        fc.mkdir(parents=True)
        (fc / "bot.py").write_text(
            "from pipecat.services.openai import OpenAILLMService\n"
            "from pipecat.services.deepgram import DeepgramSTTService\n"
        )
        (fc / "README.md").write_text(
            "# Weather\n\nFunction-calling example that reports weather.\n"
        )

        # function-calling/calendar — second subdir example under same topic
        cal = examples / "function-calling" / "calendar"
        cal.mkdir(parents=True)
        (cal / "bot.py").write_text(
            "from pipecat.services.openai import OpenAILLMService\n"
        )

        # transports/daily-demo — subdir example under a different topic
        tr = examples / "transports" / "daily-demo"
        tr.mkdir(parents=True)
        (tr / "bot.py").write_text(
            "from pipecat.transports.daily import DailyTransport\n"
        )

        # realtime/voice-agent — yet another topic
        rt = examples / "realtime" / "voice-agent"
        rt.mkdir(parents=True)
        (rt / "main.py").write_text(
            "from pipecat.services.cartesia import CartesiaTTSService\n"
        )

        return tmp_path

    @pytest.fixture
    def topic_repo_with_flat_topic_dir(self, tmp_path: Path) -> Path:
        """Topic-based layout where one topic contains flat ``.py`` files.

        The flat topic dir itself is the example (matches
        ``_discover_under_examples`` behaviour when a topic dir directly
        contains code files).
        """
        examples = tmp_path / "examples"

        # getting-started/ with flat code files at its root — topic is the example
        gs = examples / "getting-started"
        gs.mkdir(parents=True)
        (gs / "hello.py").write_text(
            "from pipecat.pipeline.pipeline import Pipeline\n"
            "Pipeline()\n"
        )
        (gs / "README.md").write_text(
            "# Getting Started\n\nQuickstart snippets.\n"
        )

        # audio/echo-bot — standard subdir-style example under a sibling topic
        au = examples / "audio" / "echo-bot"
        au.mkdir(parents=True)
        (au / "bot.py").write_text(
            "from pipecat.services.deepgram import DeepgramSTTService\n"
        )

        return tmp_path

    @pytest.fixture
    def pipecat_examples_root(self, tmp_path: Path) -> Path:
        """pipecat-examples-style root-level layout (no ``examples/`` dir)."""
        root = tmp_path / "pipecat-examples"
        root.mkdir()

        chatbot = root / "chatbot"
        chatbot.mkdir()
        (chatbot / "main.py").write_text(
            "from pipecat.services.openai import OpenAILLMService\n"
        )
        (chatbot / "README.md").write_text("# Chatbot\n\nA chatbot.\n")

        story = root / "storytelling"
        story.mkdir()
        (story / "app.py").write_text(
            "from pipecat.services.anthropic import AnthropicLLMService\n"
        )

        return root

    @pytest.fixture
    def bare_repo_root(self, tmp_path: Path) -> Path:
        """Repo root with no ``examples/`` dir — must fall back safely."""
        root = tmp_path / "bare-repo"
        root.mkdir()
        # Some code lives at the root, but there is no examples/ dir.
        (root / "main.py").write_text(
            "from pipecat.pipeline.pipeline import Pipeline\n"
            "Pipeline()\n"
        )
        return root

    # -- (a) topic-based tree with subdir examples under multiple topics ---

    def test_topic_layout_produces_entries_for_each_subdir(
        self, topic_repo_dir: Path
    ):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            topic_repo_dir, repo="pipecat-ai/pipecat"
        )
        paths = {e.path for e in entries}
        # One entry per example dir under each topic.
        assert "examples/function-calling/weather" in paths
        assert "examples/function-calling/calendar" in paths
        assert "examples/transports/daily-demo" in paths
        assert "examples/realtime/voice-agent" in paths

    def test_topic_layout_paths_are_relative_to_repo_root(
        self, topic_repo_dir: Path
    ):
        """Every entry.path must equal str(ex_dir.relative_to(repo_root))."""
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            topic_repo_dir, repo="pipecat-ai/pipecat"
        )
        for entry in entries:
            full = topic_repo_dir / entry.path
            assert full.is_dir(), f"entry.path {entry.path!r} does not resolve"
            assert entry.path == str(full.relative_to(topic_repo_dir))

    def test_topic_layout_foundational_class_is_none(self, topic_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            topic_repo_dir, repo="pipecat-ai/pipecat"
        )
        for entry in entries:
            assert entry.foundational_class is None

    def test_topic_layout_capability_tags_non_empty(self, topic_repo_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            topic_repo_dir, repo="pipecat-ai/pipecat"
        )
        for entry in entries:
            assert entry.capabilities, (
                f"Entry {entry.path!r} must carry at least one capability tag"
            )

    def test_topic_layout_tag_includes_topic_name(self, topic_repo_dir: Path):
        """Capability tags start from topic dir name.

        Unknown topics (without an override map entry) should pass through
        as a single-tag value. Known compound topics may expand.
        """
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            topic_repo_dir, repo="pipecat-ai/pipecat"
        )
        by_path = {e.path: e for e in entries}

        tr_tags = {t.name for t in by_path["examples/transports/daily-demo"].capabilities}
        assert "transports" in tr_tags

        # function-calling is one of the documented override-map entries
        fc_tags = {t.name for t in by_path["examples/function-calling/weather"].capabilities}
        assert "function-calling" in fc_tags

    # -- (b) topic-based tree where a topic contains flat .py files --------

    def test_flat_topic_dir_itself_becomes_the_example(
        self, topic_repo_with_flat_topic_dir: Path
    ):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            topic_repo_with_flat_topic_dir, repo="pipecat-ai/pipecat"
        )
        paths = {e.path for e in entries}
        # getting-started has flat code files → topic dir itself is the example
        assert "examples/getting-started" in paths
        # audio/echo-bot is a nested subdir example
        assert "examples/audio/echo-bot" in paths

    def test_flat_topic_dir_entry_has_capabilities(
        self, topic_repo_with_flat_topic_dir: Path
    ):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            topic_repo_with_flat_topic_dir, repo="pipecat-ai/pipecat"
        )
        by_path = {e.path: e for e in entries}
        gs = by_path["examples/getting-started"]
        assert gs.capabilities
        tag_names = {t.name for t in gs.capabilities}
        assert "getting-started" in tag_names

    # -- (c) legacy foundational/ tree still works unchanged ---------------

    def test_legacy_foundational_layout_unchanged(
        self, foundational_dir: Path
    ):
        """Legacy ``examples/foundational/`` tree keeps producing the same
        foundational-class entries with unchanged path prefix and metadata.
        """
        repo_root = foundational_dir.parent.parent
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            repo_root, repo="pipecat-ai/pipecat"
        )
        assert len(entries) == 3
        for entry in entries:
            assert entry.path.startswith("examples/foundational/")
            assert entry.foundational_class is not None

    # -- (d) mixed layout: foundational/ + simple-chatbot sibling (v0.0.96) -

    def test_mixed_v0096_layout_foundational_plus_sibling(self, tmp_path: Path):
        """At pins like ``v0.0.96``, ``examples/foundational/`` coexists with
        non-numbered sibling dirs such as ``simple-chatbot/``. Both must
        produce taxonomy entries.
        """
        # Foundational numbered example
        f = tmp_path / "examples" / "foundational" / "01-hello"
        f.mkdir(parents=True)
        (f / "bot.py").write_text(
            "from pipecat.pipeline.pipeline import Pipeline\nPipeline()\n"
        )
        (f / "README.md").write_text("# Hello\n\nHello example.\n")

        # v0.0.96-era sibling: simple-chatbot
        sc = tmp_path / "examples" / "simple-chatbot"
        sc.mkdir(parents=True)
        (sc / "bot.py").write_text(
            "from pipecat.services.openai import OpenAILLMService\n"
        )
        (sc / "README.md").write_text("# Simple Chatbot\n\nA simple chatbot.\n")

        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            tmp_path, repo="pipecat-ai/pipecat"
        )

        paths = {e.path for e in entries}
        assert "examples/foundational/01-hello" in paths
        assert "examples/simple-chatbot" in paths

        by_path = {e.path: e for e in entries}
        assert by_path["examples/foundational/01-hello"].foundational_class == "01-hello"
        assert by_path["examples/simple-chatbot"].foundational_class is None

    # -- (e) pipecat-examples-style root-level layout still works ----------

    def test_root_level_layout_unchanged(self, pipecat_examples_root: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            pipecat_examples_root, repo="pipecat-ai/pipecat-examples"
        )
        ids = {e.example_id for e in entries}
        assert "example-chatbot" in ids
        assert "example-storytelling" in ids
        for entry in entries:
            assert entry.foundational_class is None

    # -- (f) repo root with no examples/ dir falls back correctly ---------

    def test_no_examples_dir_fallback(self, bare_repo_root: Path):
        """Repo with no ``examples/`` dir must not raise and must not emit
        entries with a stale ``examples/...`` path prefix.
        """
        builder = TaxonomyBuilder()
        # Must not raise
        entries = builder.build_from_directory(
            bare_repo_root, repo="org/bare-repo"
        )
        # Fallback is either empty or treats repo root as a single example
        # (existing behaviour via ``build_from_examples_repo``/root entry).
        # Whichever path is taken, no entry should claim an ``examples/`` path.
        for entry in entries:
            assert not entry.path.startswith("examples/"), (
                f"Fallback emitted an entry with stale examples/ prefix: {entry.path!r}"
            )

    # -- (g) Lookup-key parity: _discover_under_examples ↔ taxonomy keys ---

    def test_lookup_key_parity_topic_layout(self, topic_repo_dir: Path):
        """Seam 1 contract: every dir returned by ``_discover_under_examples``
        must have a matching ``taxonomy_lookup[rel]`` entry built from the
        builder output. This is the unit contract for Seam 1.
        """
        from pipecat_context_hub.services.ingest.github_ingest import (
            _discover_under_examples,
        )

        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            topic_repo_dir, repo="pipecat-ai/pipecat", commit_sha="abc123"
        )
        taxonomy_lookup = {e.path: e for e in entries}

        discovered = _discover_under_examples(topic_repo_dir / "examples")
        assert discovered, "fixture must produce at least one discovered dir"
        for ex_dir in discovered:
            rel = str(ex_dir.relative_to(topic_repo_dir))
            assert rel in taxonomy_lookup, (
                f"No taxonomy entry for discovered dir {rel!r}; "
                f"available keys: {sorted(taxonomy_lookup.keys())}"
            )

    def test_lookup_key_parity_flat_topic_layout(
        self, topic_repo_with_flat_topic_dir: Path
    ):
        """Parity test (g) also covers the flat-topic-dir case."""
        from pipecat_context_hub.services.ingest.github_ingest import (
            _discover_under_examples,
        )

        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            topic_repo_with_flat_topic_dir, repo="pipecat-ai/pipecat"
        )
        taxonomy_lookup = {e.path: e for e in entries}

        discovered = _discover_under_examples(
            topic_repo_with_flat_topic_dir / "examples"
        )
        assert discovered
        for ex_dir in discovered:
            rel = str(ex_dir.relative_to(topic_repo_with_flat_topic_dir))
            assert rel in taxonomy_lookup, (
                f"No taxonomy entry for discovered dir {rel!r}; "
                f"available keys: {sorted(taxonomy_lookup.keys())}"
            )

    def test_lookup_key_parity_mixed_v0096_layout(self, tmp_path: Path):
        """Parity test (g) extended to the v0.0.96 mixed layout."""
        from pipecat_context_hub.services.ingest.github_ingest import (
            _discover_under_examples,
        )

        f = tmp_path / "examples" / "foundational" / "01-hello"
        f.mkdir(parents=True)
        (f / "bot.py").write_text(
            "from pipecat.pipeline.pipeline import Pipeline\nPipeline()\n"
        )
        sc = tmp_path / "examples" / "simple-chatbot"
        sc.mkdir(parents=True)
        (sc / "bot.py").write_text(
            "from pipecat.services.openai import OpenAILLMService\n"
        )

        builder = TaxonomyBuilder()
        entries = builder.build_from_directory(
            tmp_path, repo="pipecat-ai/pipecat"
        )
        taxonomy_lookup = {e.path: e for e in entries}

        discovered = _discover_under_examples(tmp_path / "examples")
        assert discovered
        for ex_dir in discovered:
            rel = str(ex_dir.relative_to(tmp_path))
            assert rel in taxonomy_lookup, (
                f"No taxonomy entry for discovered dir {rel!r}; "
                f"available keys: {sorted(taxonomy_lookup.keys())}"
            )


def test_no_junk_entries_from_repo_root(tmp_path: Path):
    """Phase 2: ``build_from_directory`` on a packaged-repo root with
    ``src/``, ``tests/``, ``docs/`` and a real ``examples/foo/bot.py`` must
    yield exactly one entry for ``examples/foo`` and zero entries for the
    non-example root siblings.
    """
    # Packaged-project markers: src/ + pyproject.toml trigger
    # require_example_markers=True in the fallback.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'pkg'\n")

    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "module.py").write_text(
        "from pipecat.pipeline.pipeline import Pipeline\nPipeline()\n"
    )

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_something.py").write_text("def test_ok():\n    assert True\n")

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# Docs\n")

    foo = tmp_path / "examples" / "foo"
    foo.mkdir(parents=True)
    (foo / "bot.py").write_text(
        "from pipecat.services.openai import OpenAILLMService\n"
    )
    (foo / "README.md").write_text("# Foo\n\nFoo example.\n")

    builder = TaxonomyBuilder()
    entries = builder.build_from_directory(
        tmp_path, repo="org/packaged-project", commit_sha="abc123"
    )

    paths = {e.path for e in entries}

    # Exactly one entry for examples/foo.
    foo_entries = [e for e in entries if e.path == "examples/foo"]
    assert len(foo_entries) == 1, (
        f"Expected exactly one entry for 'examples/foo'; got paths={sorted(paths)}"
    )

    # No junk entries for packaged-project sibling dirs.
    for junk in ("src", "tests", "docs"):
        assert junk not in paths, (
            f"Fallback emitted junk entry for {junk!r}; paths={sorted(paths)}"
        )


class TestBuildFromExamplesRepoExampleMarkers:
    """Phase 2: ``build_from_examples_repo(require_example_markers=...)``.

    When ``require_example_markers=True``, well-known non-example root dirs
    (``src``, ``tests``, ``docs``, ``scripts``, ``dashboard``, ``.github``,
    ``.claude``) are skipped in addition to the existing hidden/pycache
    rules. Default (``False``) preserves backward compatibility — those
    dirs are **not** skipped.
    """

    @pytest.fixture
    def repo_with_non_example_siblings(self, tmp_path: Path) -> Path:
        """Root containing a real example plus many junk sibling dirs."""
        root = tmp_path / "packaged-repo"
        root.mkdir()

        # Real example sibling
        real = root / "real-example"
        real.mkdir()
        (real / "main.py").write_text(
            "from pipecat.services.openai import OpenAILLMService\n"
        )
        (real / "README.md").write_text("# Real\n\nReal example.\n")

        # Non-example sibling dirs that Phase 2 should skip when
        # ``require_example_markers=True``.
        for junk in ("src", "tests", "docs", "scripts", "dashboard"):
            d = root / junk
            d.mkdir()
            (d / "placeholder.py").write_text("# placeholder\n")

        # Dotdir non-example siblings — already skipped by the existing
        # ``.*`` rule, but we want them to stay skipped under the new
        # ``require_example_markers=True`` path too.
        for dotdir in (".github", ".claude"):
            d = root / dotdir
            d.mkdir()
            (d / "placeholder.yml").write_text("x: 1\n")

        return root

    def test_require_example_markers_true_skips_packaged_project_dirs(
        self, repo_with_non_example_siblings: Path
    ):
        builder = TaxonomyBuilder()
        entries = builder.build_from_examples_repo(
            repo_with_non_example_siblings,
            repo="org/packaged-repo",
            require_example_markers=True,
        )
        ids = {e.example_id for e in entries}
        paths = {e.path for e in entries}

        # Real example still emitted.
        assert "example-real-example" in ids

        # Well-known non-example dirs are skipped.
        for junk in ("src", "tests", "docs", "scripts", "dashboard",
                     ".github", ".claude"):
            assert f"example-{junk}" not in ids, (
                f"{junk!r} must be skipped with require_example_markers=True"
            )
            assert junk not in paths

    def test_require_example_markers_default_keeps_backward_compat(
        self, repo_with_non_example_siblings: Path
    ):
        """Default ``require_example_markers=False`` must NOT skip the new
        list — backward compat with existing ``pipecat-examples`` callers.
        """
        builder = TaxonomyBuilder()
        entries = builder.build_from_examples_repo(
            repo_with_non_example_siblings,
            repo="org/packaged-repo",
        )
        ids = {e.example_id for e in entries}

        # Real example still emitted.
        assert "example-real-example" in ids

        # The non-dotdir siblings are treated as examples by default
        # (backward-compatible behaviour).
        for junk in ("src", "tests", "docs", "scripts", "dashboard"):
            assert f"example-{junk}" in ids, (
                f"Default (require_example_markers=False) must not skip "
                f"{junk!r} — backward compat broken"
            )

        # Dotdirs remain skipped by the pre-existing rule regardless of the
        # new flag.
        assert "example-.github" not in ids
        assert "example-.claude" not in ids


class TestTaxonomyEntryRoundTrip:
    """Verify that taxonomy entries produced by the builder round-trip through JSON."""

    def test_round_trip(self, foundational_dir: Path):
        builder = TaxonomyBuilder()
        entries = builder.build_from_foundational(foundational_dir)
        for entry in entries:
            json_str = entry.model_dump_json()
            rebuilt = TaxonomyEntry.model_validate_json(json_str)
            assert rebuilt == entry
