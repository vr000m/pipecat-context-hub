"""Tests for the docs crawler service."""

from __future__ import annotations

from datetime import timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from pipecat_context_hub.services.ingest.docs_crawler import (
    DocsCrawler,
    _extract_links,
    _html_to_markdown,
    _make_chunk_id,
    _split_into_sections,
    chunk_markdown,
)
from pipecat_context_hub.shared.config import ChunkingConfig, SourceConfig
from pipecat_context_hub.shared.types import ChunkedRecord, IngestResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
<nav><a href="/nav-link">Nav</a></nav>
<header><h1>Site Header</h1></header>
<main>
  <h1>Getting Started</h1>
  <p>Welcome to Pipecat. Install with pip:</p>
  <pre><code>pip install pipecat-ai</code></pre>

  <h2>Configuration</h2>
  <p>Configure your pipeline by creating a config file.</p>

  <h2>Running</h2>
  <p>Run your bot with the following command:</p>
  <pre><code>python bot.py</code></pre>
</main>
<footer>Copyright 2026</footer>
</body>
</html>
"""

SAMPLE_HTML_NO_MAIN = """
<html>
<body>
<h1>Simple Page</h1>
<p>Some content here.</p>
</body>
</html>
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
        max_pages=10,
    )


# ---------------------------------------------------------------------------
# Unit tests: helper functions
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


class TestHtmlToMarkdown:
    def test_strips_nav_header_footer(self):
        result = _html_to_markdown(SAMPLE_HTML)
        assert "Nav" not in result
        assert "Site Header" not in result
        assert "Copyright" not in result

    def test_preserves_content(self):
        result = _html_to_markdown(SAMPLE_HTML)
        assert "Getting Started" in result
        assert "Configuration" in result
        assert "pip install pipecat-ai" in result

    def test_uses_main_when_available(self):
        result = _html_to_markdown(SAMPLE_HTML)
        # Main content is preserved
        assert "Welcome to Pipecat" in result

    def test_fallback_without_main(self):
        result = _html_to_markdown(SAMPLE_HTML_NO_MAIN)
        assert "Simple Page" in result
        assert "Some content" in result

    def test_empty_html(self):
        result = _html_to_markdown("")
        assert result == ""


class TestExtractLinks:
    def test_extracts_internal_links(self):
        from bs4 import BeautifulSoup

        html = '<a href="/guides/getting-started">Guide</a><a href="/api/overview">API</a>'
        soup = BeautifulSoup(html, "html.parser")
        links = _extract_links(soup, "https://docs.pipecat.ai/")
        assert "https://docs.pipecat.ai/guides/getting-started" in links
        assert "https://docs.pipecat.ai/api/overview" in links

    def test_skips_external_links(self):
        from bs4 import BeautifulSoup

        html = '<a href="https://github.com/foo">External</a>'
        soup = BeautifulSoup(html, "html.parser")
        links = _extract_links(soup, "https://docs.pipecat.ai/")
        assert len(links) == 0

    def test_strips_fragments(self):
        from bs4 import BeautifulSoup

        html = '<a href="/guide#section">Link</a>'
        soup = BeautifulSoup(html, "html.parser")
        links = _extract_links(soup, "https://docs.pipecat.ai/")
        assert all("#" not in link for link in links)


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
        """Ingest fetches pages and upserts records."""
        html_response = SAMPLE_HTML

        with patch.object(crawler, "_crawl_site", return_value=[
            ("https://docs.pipecat.ai/", html_response),
        ]):
            result = await crawler.ingest()

        assert isinstance(result, IngestResult)
        assert result.source == "https://docs.pipecat.ai/"
        assert result.records_upserted == 5
        assert result.errors == []
        assert result.duration_seconds > 0
        mock_writer.upsert.assert_called_once()

        # Verify the records passed to upsert
        call_args = mock_writer.upsert.call_args
        records: list[ChunkedRecord] = call_args[0][0]
        assert len(records) > 0
        for record in records:
            assert record.content_type == "doc"
            assert record.source_url == "https://docs.pipecat.ai/"
            assert record.chunk_id

    async def test_ingest_handles_crawl_failure(self, crawler: DocsCrawler):
        """Ingest handles crawl exceptions gracefully."""
        with patch.object(crawler, "_crawl_site", side_effect=RuntimeError("network error")):
            result = await crawler.ingest()

        assert result.records_upserted == 0
        assert len(result.errors) == 1
        assert "Crawl failed" in result.errors[0]

    async def test_ingest_handles_processing_error(
        self, crawler: DocsCrawler, mock_writer: AsyncMock,
    ):
        """Ingest handles per-page processing errors."""
        with patch.object(crawler, "_crawl_site", return_value=[
            ("https://docs.pipecat.ai/good", SAMPLE_HTML),
            ("https://docs.pipecat.ai/bad", "valid html"),
        ]):
            with patch.object(
                crawler, "_process_page",
                side_effect=[RuntimeError("parse error"), []],
            ):
                result = await crawler.ingest()

        assert len(result.errors) == 1
        assert "Processing" in result.errors[0]

    async def test_ingest_handles_upsert_failure(
        self, crawler: DocsCrawler, mock_writer: AsyncMock,
    ):
        """Ingest handles upsert exceptions."""
        mock_writer.upsert.side_effect = RuntimeError("DB error")

        with patch.object(crawler, "_crawl_site", return_value=[
            ("https://docs.pipecat.ai/", SAMPLE_HTML),
        ]):
            result = await crawler.ingest()

        assert "Upsert failed" in result.errors[0]

    async def test_refresh_delegates_to_ingest(
        self, crawler: DocsCrawler, mock_writer: AsyncMock,
    ):
        """refresh() is identical to ingest() in v0."""
        with patch.object(crawler, "_crawl_site", return_value=[
            ("https://docs.pipecat.ai/", SAMPLE_HTML),
        ]):
            result = await crawler.refresh()

        assert isinstance(result, IngestResult)
        assert result.records_upserted == 5


class TestDocsCrawlerIngestUrls:
    async def test_ingest_urls(self, crawler: DocsCrawler, mock_writer: AsyncMock):
        """ingest_urls fetches specific URLs without crawling."""
        mock_response = AsyncMock()
        mock_response.text = SAMPLE_HTML
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None

        with patch.object(crawler, "_fetch_page", return_value=SAMPLE_HTML):
            result = await crawler.ingest_urls(["https://docs.pipecat.ai/guide"])

        assert result.records_upserted == 5
        assert result.errors == []
        mock_writer.upsert.assert_called_once()

    async def test_ingest_urls_handles_fetch_failure(
        self, crawler: DocsCrawler, mock_writer: AsyncMock,
    ):
        """ingest_urls records errors for failed fetches."""
        with patch.object(crawler, "_fetch_page", return_value=None):
            result = await crawler.ingest_urls(["https://docs.pipecat.ai/missing"])

        assert len(result.errors) == 1
        assert "Failed to fetch" in result.errors[0]


class TestDocsCrawlerProtocol:
    def test_implements_ingester_protocol(self, mock_writer: AsyncMock):
        """DocsCrawler satisfies the Ingester protocol."""
        from pipecat_context_hub.shared.interfaces import Ingester

        crawler = DocsCrawler(index_writer=mock_writer)
        # Protocol compliance: has ingest() and refresh()
        assert hasattr(crawler, "ingest")
        assert hasattr(crawler, "refresh")
        assert callable(crawler.ingest)
        assert callable(crawler.refresh)
        # Structural subtyping check: should be assignable to Ingester
        _ingester: Ingester = crawler  # noqa: F841


class TestDocsCrawlerFetchPage:
    async def test_fetch_page_success(self, crawler: DocsCrawler):
        """fetch_page returns HTML on success."""
        request = httpx.Request("GET", "https://docs.pipecat.ai/")
        mock_response = httpx.Response(200, text="<html>ok</html>", request=request)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        crawler._client = mock_client

        result = await crawler._fetch_page("https://docs.pipecat.ai/")
        assert result == "<html>ok</html>"

    async def test_fetch_page_error_returns_none(self, crawler: DocsCrawler):
        """fetch_page returns None on HTTP errors."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        crawler._client = mock_client

        result = await crawler._fetch_page("https://docs.pipecat.ai/missing")
        assert result is None


# ---------------------------------------------------------------------------
# Integration test: real HTTP fetch
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    True,  # Set to False to run integration test manually
    reason="Integration test — requires network access to docs.pipecat.ai",
)
class TestDocsCrawlerIntegration:
    async def test_fetch_real_page(self):
        """Fetch a real page from docs.pipecat.ai and produce valid records."""
        mock_writer = AsyncMock()
        mock_writer.upsert = AsyncMock(return_value=0)
        crawler = DocsCrawler(index_writer=mock_writer, max_pages=1)

        try:
            result = await crawler.ingest_urls(["https://docs.pipecat.ai/"])
            assert result.errors == [] or len(result.errors) == 0

            call_args = mock_writer.upsert.call_args
            if call_args:
                records: list[ChunkedRecord] = call_args[0][0]
                assert len(records) > 0
                for record in records:
                    assert record.content_type == "doc"
                    assert record.source_url == "https://docs.pipecat.ai/"
                    assert record.chunk_id
                    assert record.indexed_at.tzinfo == timezone.utc
        finally:
            await crawler.close()
