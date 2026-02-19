"""Tests for the docs crawler service (llms-full.txt ingester)."""

from __future__ import annotations

from datetime import timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from pipecat_context_hub.services.ingest.docs_crawler import (
    DocsCrawler,
    _clean_mintlify_tags,
    _make_chunk_id,
    _split_into_pages,
    _split_into_sections,
    chunk_markdown,
)
from pipecat_context_hub.shared.config import ChunkingConfig, SourceConfig
from pipecat_context_hub.shared.types import ChunkedRecord, IngestResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_LLMS_TXT = """\
# Getting Started
Source: https://docs.pipecat.ai/guides/getting-started

Welcome to Pipecat. Install with pip:

```python
pip install pipecat-ai
```

## Configuration

Configure your pipeline by creating a config file.

<Note>Make sure to set your API keys before running.</Note>

<ParamField type="string">
  The API key for authentication.
</ParamField>

## Running

Run your bot with the following command:

```python
python bot.py
```

# API Reference
Source: https://docs.pipecat.ai/api/reference

The Pipecat API reference.

## Pipeline

<Warning>This API is in beta.</Warning>

The Pipeline class is the core abstraction.

# Telephony
Source: https://docs.pipecat.ai/guides/telephony

Set up telephony integrations.

## Twilio

<Tip>Use websockets for best performance.</Tip>

<Card title="Twilio Guide" href="/twilio">
  Step-by-step Twilio setup.
</Card>
"""


@pytest.fixture
def mock_writer() -> AsyncMock:
    writer = AsyncMock()
    writer.upsert = AsyncMock(return_value=5)
    writer.delete_by_source = AsyncMock(return_value=0)
    return writer


@pytest.fixture
def source_config() -> SourceConfig:
    return SourceConfig(docs_url="https://docs.pipecat.ai/")


@pytest.fixture
def chunking_config() -> ChunkingConfig:
    return ChunkingConfig(doc_max_tokens=512, doc_overlap_tokens=50)


@pytest.fixture
def crawler(
    mock_writer: AsyncMock,
    source_config: SourceConfig,
    chunking_config: ChunkingConfig,
) -> DocsCrawler:
    return DocsCrawler(
        index_writer=mock_writer,
        source_config=source_config,
        chunking_config=chunking_config,
    )


# ---------------------------------------------------------------------------
# Unit tests: _make_chunk_id
# ---------------------------------------------------------------------------


class TestMakeChunkId:
    def test_deterministic(self):
        """Same inputs produce the same chunk_id."""
        id1 = _make_chunk_id("https://docs.pipecat.ai/foo", "section-a", 0)
        id2 = _make_chunk_id("https://docs.pipecat.ai/foo", "section-a", 0)
        assert id1 == id2

    def test_different_inputs_different_ids(self):
        """Different inputs produce different chunk_ids."""
        id1 = _make_chunk_id("https://docs.pipecat.ai/foo", "section-a", 0)
        id2 = _make_chunk_id("https://docs.pipecat.ai/foo", "section-b", 0)
        id3 = _make_chunk_id("https://docs.pipecat.ai/foo", "section-a", 1)
        assert id1 != id2
        assert id1 != id3

    def test_returns_hex_string(self):
        result = _make_chunk_id("url", "section", 0)
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)


# ---------------------------------------------------------------------------
# Unit tests: _split_into_pages
# ---------------------------------------------------------------------------


class TestSplitIntoPages:
    def test_splits_multiple_pages(self):
        pages = _split_into_pages(SAMPLE_LLMS_TXT)
        assert len(pages) == 3

    def test_extracts_titles(self):
        pages = _split_into_pages(SAMPLE_LLMS_TXT)
        assert pages[0][0] == "Getting Started"
        assert pages[1][0] == "API Reference"
        assert pages[2][0] == "Telephony"

    def test_extracts_source_urls(self):
        pages = _split_into_pages(SAMPLE_LLMS_TXT)
        assert pages[0][1] == "https://docs.pipecat.ai/guides/getting-started"
        assert pages[1][1] == "https://docs.pipecat.ai/api/reference"
        assert pages[2][1] == "https://docs.pipecat.ai/guides/telephony"

    def test_body_excludes_title_and_source(self):
        pages = _split_into_pages(SAMPLE_LLMS_TXT)
        body = pages[0][2]
        assert not body.startswith("# Getting Started")
        assert "Source:" not in body

    def test_body_has_content(self):
        pages = _split_into_pages(SAMPLE_LLMS_TXT)
        assert "Welcome to Pipecat" in pages[0][2]
        assert "Pipeline class" in pages[1][2]

    def test_hash_comment_in_code_not_boundary(self):
        text = (
            "# Page\nSource: https://docs.pipecat.ai/page\n\n"
            "```python\n# this is a comment\ncode = 1\n```\n"
        )
        pages = _split_into_pages(text)
        assert len(pages) == 1
        assert "# this is a comment" in pages[0][2]

    def test_empty_input(self):
        assert _split_into_pages("") == []

    def test_no_source_line(self):
        """A heading without a Source: line is not a page boundary."""
        text = "# Just a heading\nSome content.\n"
        assert _split_into_pages(text) == []


# ---------------------------------------------------------------------------
# Unit tests: _clean_mintlify_tags
# ---------------------------------------------------------------------------


class TestCleanMintlifyTags:
    def test_strips_paramfield(self):
        text = '<ParamField type="string">\n  API key\n</ParamField>'
        result = _clean_mintlify_tags(text)
        assert "<ParamField" not in result
        assert "API key" in result

    def test_converts_note_to_blockquote(self):
        text = "<Note>Important info here.</Note>"
        result = _clean_mintlify_tags(text)
        assert "<Note>" not in result
        assert "**Note:**" in result
        assert "Important info" in result

    def test_converts_warning_to_blockquote(self):
        text = "<Warning>Be careful!</Warning>"
        result = _clean_mintlify_tags(text)
        assert "<Warning>" not in result
        assert "**Warning:**" in result
        assert "Be careful" in result

    def test_converts_tip_to_blockquote(self):
        text = "<Tip>Useful hint.</Tip>"
        result = _clean_mintlify_tags(text)
        assert "<Tip>" not in result
        assert "**Tip:**" in result

    def test_converts_info_to_blockquote(self):
        text = "<Info>Background info.</Info>"
        result = _clean_mintlify_tags(text)
        assert "**Info:**" in result

    def test_admonition_with_attributes(self):
        text = '<Note type="warning">Be careful here.</Note>'
        result = _clean_mintlify_tags(text)
        assert "<Note" not in result
        assert "**Note:**" in result
        assert "Be careful here." in result

    def test_warning_with_attributes(self):
        text = '<Warning title="Deprecation">Old API.</Warning>'
        result = _clean_mintlify_tags(text)
        assert "<Warning" not in result
        assert "**Warning:**" in result
        assert "Old API." in result

    def test_strips_card_tags(self):
        text = '<Card title="Foo" href="/bar">\n  Content here.\n</Card>'
        result = _clean_mintlify_tags(text)
        assert "<Card" not in result
        assert "</Card>" not in result
        assert "Content here." in result

    def test_strips_cardgroup(self):
        text = "<CardGroup>\n<Card>A</Card>\n<Card>B</Card>\n</CardGroup>"
        result = _clean_mintlify_tags(text)
        assert "<CardGroup>" not in result
        assert "A" in result
        assert "B" in result

    def test_strips_tabs(self):
        text = "<Tabs>\n<Tab>\nContent\n</Tab>\n</Tabs>"
        result = _clean_mintlify_tags(text)
        assert "<Tabs>" not in result
        assert "<Tab>" not in result
        assert "Content" in result

    def test_strips_steps(self):
        text = "<Steps>\n<Step>\nDo this\n</Step>\n</Steps>"
        result = _clean_mintlify_tags(text)
        assert "<Steps>" not in result
        assert "Do this" in result

    def test_strips_self_closing_tags(self):
        text = 'Before <Icon name="check" /> after.'
        result = _clean_mintlify_tags(text)
        assert "<Icon" not in result
        assert "Before" in result
        assert "after." in result

    def test_preserves_plain_markdown(self):
        text = "Regular markdown with **bold** and `code`."
        assert _clean_mintlify_tags(text) == text

    def test_collapses_blank_lines(self):
        text = "<Card>A</Card>\n\n\n\n<Card>B</Card>"
        result = _clean_mintlify_tags(text)
        assert "\n\n\n" not in result


# ---------------------------------------------------------------------------
# Unit tests: _split_into_sections
# ---------------------------------------------------------------------------


class TestSplitIntoSections:
    def test_splits_on_headings(self):
        md_text = "# Intro\n\nSome text.\n\n## Details\n\nMore text."
        sections = _split_into_sections(md_text)
        assert len(sections) == 2
        assert sections[0][0] == "Intro"
        assert sections[1][0] == "Details"

    def test_content_before_heading(self):
        md_text = "Preamble text.\n\n# First Heading\n\nBody."
        sections = _split_into_sections(md_text)
        assert len(sections) == 2
        assert sections[0][0] == ""  # no heading for preamble
        assert "Preamble" in sections[0][1]
        assert sections[1][0] == "First Heading"

    def test_no_headings(self):
        md_text = "Just a paragraph with no headings."
        sections = _split_into_sections(md_text)
        assert len(sections) == 1
        assert sections[0][0] == ""
        assert "paragraph" in sections[0][1]

    def test_empty_input(self):
        sections = _split_into_sections("")
        assert sections == []

    def test_ignores_headings_in_fenced_code_blocks(self):
        md_text = "# Real Heading\n\nSome text.\n\n```python\n# this is a comment\ncode = 1\n```\n\n## Another Heading\n\nMore text."
        sections = _split_into_sections(md_text)
        headings = [h for h, _ in sections]
        assert "this is a comment" not in headings
        assert "Real Heading" in headings
        assert "Another Heading" in headings

    def test_ignores_headings_in_tilde_fenced_blocks(self):
        md_text = "# Title\n\nIntro.\n\n~~~\n# comment in tilde fence\n~~~\n\n## End\n\nDone."
        sections = _split_into_sections(md_text)
        headings = [h for h, _ in sections]
        assert "comment in tilde fence" not in headings
        assert "Title" in headings
        assert "End" in headings

    def test_fenced_block_content_preserved_in_body(self):
        md_text = "# Title\n\n```\n# comment\ncode\n```"
        sections = _split_into_sections(md_text)
        assert len(sections) == 1
        assert "# comment" in sections[0][1]
        assert "code" in sections[0][1]

    def test_multiple_fenced_blocks(self):
        md_text = "# A\n\n```\n# not a heading\n```\n\n## B\n\n```\n# also not\n```"
        sections = _split_into_sections(md_text)
        headings = [h for h, _ in sections]
        assert headings == ["A", "B"]


# ---------------------------------------------------------------------------
# Unit tests: chunk_markdown
# ---------------------------------------------------------------------------


class TestChunkMarkdown:
    def test_produces_records(self):
        records = chunk_markdown(
            "# Title\n\nSome content here.",
            source_url="https://docs.pipecat.ai/guide",
        )
        assert len(records) >= 1
        assert all(isinstance(r, ChunkedRecord) for r in records)

    def test_record_fields(self):
        records = chunk_markdown(
            "# Title\n\nSome content here.",
            source_url="https://docs.pipecat.ai/guide",
        )
        r = records[0]
        assert r.content_type == "doc"
        assert r.source_url == "https://docs.pipecat.ai/guide"
        assert r.path == "/guide"
        assert r.indexed_at.tzinfo == timezone.utc
        assert r.chunk_id  # non-empty
        assert "section" in r.metadata

    def test_idempotent_chunk_ids(self):
        """Re-chunking same content produces same IDs."""
        md_text = "# Title\n\nSome content.\n\n## Section\n\nMore content."
        records1 = chunk_markdown(md_text, source_url="https://docs.pipecat.ai/page")
        records2 = chunk_markdown(md_text, source_url="https://docs.pipecat.ai/page")
        ids1 = [r.chunk_id for r in records1]
        ids2 = [r.chunk_id for r in records2]
        assert ids1 == ids2

    def test_respects_max_tokens(self):
        # Create content that exceeds the limit
        long_para = "word " * 200  # ~200 tokens at 4 chars/token ≈ 250 tokens
        md_text = f"# Title\n\n{long_para}\n\n{long_para}\n\n{long_para}"
        records = chunk_markdown(
            md_text,
            source_url="https://docs.pipecat.ai/long",
            max_tokens=100,
            overlap_tokens=10,
        )
        assert len(records) > 1

    def test_empty_markdown(self):
        records = chunk_markdown("", source_url="https://docs.pipecat.ai/empty")
        assert records == []

    def test_multiple_sections(self):
        md_text = "# First\n\nContent A.\n\n## Second\n\nContent B.\n\n## Third\n\nContent C."
        records = chunk_markdown(md_text, source_url="https://docs.pipecat.ai/multi")
        assert len(records) == 3
        assert "First" in records[0].content
        assert "Second" in records[1].content
        assert "Third" in records[2].content


# ---------------------------------------------------------------------------
# Unit tests: DocsCrawler
# ---------------------------------------------------------------------------


class TestDocsCrawlerIngest:
    async def test_ingest_calls_writer(self, crawler: DocsCrawler, mock_writer: AsyncMock):
        """Ingest fetches llms-full.txt and upserts records."""
        with patch.object(crawler, "_fetch_llms_txt", return_value=SAMPLE_LLMS_TXT):
            result = await crawler.ingest()

        assert isinstance(result, IngestResult)
        assert result.source == "https://docs.pipecat.ai/"
        assert result.records_upserted == 5
        assert result.errors == []
        assert result.duration_seconds > 0
        mock_writer.upsert.assert_called_once()

        records: list[ChunkedRecord] = mock_writer.upsert.call_args[0][0]
        assert len(records) > 0
        for record in records:
            assert record.content_type == "doc"
            assert record.chunk_id

    async def test_ingest_produces_records_for_all_pages(
        self, crawler: DocsCrawler, mock_writer: AsyncMock,
    ):
        """Records span all pages in the llms-full.txt file."""
        with patch.object(crawler, "_fetch_llms_txt", return_value=SAMPLE_LLMS_TXT):
            await crawler.ingest()

        records: list[ChunkedRecord] = mock_writer.upsert.call_args[0][0]
        source_urls = {r.source_url for r in records}
        assert "https://docs.pipecat.ai/guides/getting-started" in source_urls
        assert "https://docs.pipecat.ai/api/reference" in source_urls
        assert "https://docs.pipecat.ai/guides/telephony" in source_urls

    async def test_ingest_cleans_mintlify_tags(
        self, crawler: DocsCrawler, mock_writer: AsyncMock,
    ):
        """Mintlify tags are cleaned from record content."""
        with patch.object(crawler, "_fetch_llms_txt", return_value=SAMPLE_LLMS_TXT):
            await crawler.ingest()

        records: list[ChunkedRecord] = mock_writer.upsert.call_args[0][0]
        all_content = " ".join(r.content for r in records)
        assert "<ParamField" not in all_content
        assert "<Card" not in all_content
        assert "</Card>" not in all_content

    async def test_ingest_handles_fetch_failure(self, crawler: DocsCrawler):
        """Ingest handles fetch exceptions gracefully."""
        with patch.object(
            crawler, "_fetch_llms_txt", side_effect=httpx.HTTPError("timeout"),
        ):
            result = await crawler.ingest()

        assert result.records_upserted == 0
        assert len(result.errors) == 1
        assert "Failed to fetch llms-full.txt" in result.errors[0]

    async def test_ingest_handles_upsert_failure(
        self, crawler: DocsCrawler, mock_writer: AsyncMock,
    ):
        """Ingest handles upsert exceptions."""
        mock_writer.upsert.side_effect = RuntimeError("DB error")

        with patch.object(crawler, "_fetch_llms_txt", return_value=SAMPLE_LLMS_TXT):
            result = await crawler.ingest()

        assert "Upsert failed" in result.errors[0]

class TestDocsCrawlerProtocol:
    def test_implements_ingester_protocol(self, mock_writer: AsyncMock):
        """DocsCrawler satisfies the Ingester protocol."""
        from pipecat_context_hub.shared.interfaces import Ingester

        crawler = DocsCrawler(index_writer=mock_writer)
        assert hasattr(crawler, "ingest")
        assert callable(crawler.ingest)
        # Structural subtyping check: should be assignable to Ingester
        _ingester: Ingester = crawler  # noqa: F841


class TestDocsCrawlerFetchLlmsTxt:
    async def test_fetch_success(self, crawler: DocsCrawler):
        """_fetch_llms_txt returns text on success."""
        request = httpx.Request("GET", "https://docs.pipecat.ai/llms-full.txt")
        mock_response = httpx.Response(200, text="# Page\nSource: url\n\nBody", request=request)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        crawler._client = mock_client

        result = await crawler._fetch_llms_txt()
        assert "# Page" in result

    async def test_fetch_error_raises(self, crawler: DocsCrawler):
        """_fetch_llms_txt raises on HTTP errors."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=httpx.ConnectError("connection refused"),
        )
        crawler._client = mock_client

        with pytest.raises(httpx.ConnectError):
            await crawler._fetch_llms_txt()
