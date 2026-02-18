"""Docs crawler for docs.pipecat.ai.

Fetches pages from the Pipecat documentation site, converts HTML to markdown,
chunks into sections respecting token limits, and writes ChunkedRecord objects
via an IndexWriter.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify as md  # type: ignore[import-untyped]

from pipecat_context_hub.shared.config import ChunkingConfig, SourceConfig
from pipecat_context_hub.shared.interfaces import IndexWriter
from pipecat_context_hub.shared.types import ChunkedRecord, IngestResult

logger = logging.getLogger(__name__)

# Rough approximation: 1 token ≈ 4 characters for English text.
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    """Estimate token count from character length."""
    return len(text) // _CHARS_PER_TOKEN


def _make_chunk_id(source_url: str, section_path: str, chunk_index: int) -> str:
    """Deterministic chunk ID from source URL, section heading path, and index."""
    raw = f"{source_url}|{section_path}|{chunk_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _extract_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Extract internal links from the page for crawling."""
    links: list[str] = []
    parsed_base = urlparse(base_url)
    for a_tag in soup.find_all("a", href=True):
        href = str(a_tag["href"])
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        # Only follow links on the same host, skip anchors-only and non-http
        if parsed.hostname == parsed_base.hostname and parsed.scheme in ("http", "https"):
            # Strip fragment
            clean = parsed._replace(fragment="").geturl()
            links.append(clean)
    return links


def _html_to_markdown(html: str) -> str:
    """Convert HTML to markdown, stripping nav/header/footer chrome."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove navigation, header, footer, script, style
    for tag_name in ("nav", "header", "footer", "script", "style", "noscript"):
        for tag in soup.find_all(tag_name):
            tag.decompose()
    # Try to find main content area
    main = soup.find("main") or soup.find("article")
    if isinstance(main, Tag):
        target = main
    else:
        target = soup
    result: str = md(str(target), heading_style="ATX", strip=["img"])
    # Normalize whitespace: collapse multiple blank lines into two
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _split_into_sections(markdown: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) sections.

    Returns a list of (section_heading, section_content) tuples.
    The first entry may have heading="" if there's content before any heading.
    """
    # Split on markdown headings (## or ###, etc.)
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    parts: list[tuple[str, str]] = []
    last_end = 0
    last_heading = ""

    for match in heading_pattern.finditer(markdown):
        # Content before this heading belongs to the previous section
        content_before = markdown[last_end : match.start()].strip()
        if content_before or last_heading:
            parts.append((last_heading, content_before))
        last_heading = match.group(2).strip()
        last_end = match.end()

    # Remaining content after the last heading
    remaining = markdown[last_end:].strip()
    if remaining or last_heading:
        parts.append((last_heading, remaining))

    # If no sections found, treat entire content as one section
    if not parts and markdown.strip():
        parts.append(("", markdown.strip()))

    return parts


def _chunk_section(
    section_content: str,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Split a section into chunks respecting token limits.

    Uses paragraph boundaries when possible, falls back to sentence splitting.
    """
    if _estimate_tokens(section_content) <= max_tokens:
        return [section_content] if section_content.strip() else []

    max_chars = max_tokens * _CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * _CHARS_PER_TOKEN

    # Split by paragraphs first
    paragraphs = re.split(r"\n\n+", section_content)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        if current_len + para_len + 2 > max_chars and current:
            # Flush current chunk
            chunk_text = "\n\n".join(current)
            chunks.append(chunk_text)
            # Overlap: keep last paragraph(s) that fit in overlap
            overlap_parts: list[str] = []
            overlap_len = 0
            for p in reversed(current):
                if overlap_len + len(p) + 2 <= overlap_chars:
                    overlap_parts.insert(0, p)
                    overlap_len += len(p) + 2
                else:
                    break
            current = overlap_parts + [para]
            current_len = sum(len(p) for p in current) + 2 * (len(current) - 1)
        else:
            current.append(para)
            current_len += para_len + (2 if current_len > 0 else 0)

    if current:
        chunk_text = "\n\n".join(current)
        if chunk_text.strip():
            chunks.append(chunk_text)

    return chunks


def chunk_markdown(
    markdown: str,
    source_url: str,
    max_tokens: int = 512,
    overlap_tokens: int = 50,
) -> list[ChunkedRecord]:
    """Chunk markdown into ChunkedRecord objects.

    Section-aware: splits on headings first, then chunks each section
    if it exceeds the token limit.
    """
    sections = _split_into_sections(markdown)
    records: list[ChunkedRecord] = []
    now = datetime.now(timezone.utc)
    url_path = urlparse(source_url).path

    for section_heading, section_body in sections:
        # Build full section text: include heading in content for context
        if section_heading:
            full_text = f"## {section_heading}\n\n{section_body}" if section_body else f"## {section_heading}"
        else:
            full_text = section_body

        if not full_text.strip():
            continue

        section_path = section_heading or "intro"
        chunks = _chunk_section(full_text, max_tokens, overlap_tokens)

        for idx, chunk_text in enumerate(chunks):
            chunk_id = _make_chunk_id(source_url, section_path, idx)
            record = ChunkedRecord(
                chunk_id=chunk_id,
                content=chunk_text,
                content_type="doc",
                source_url=source_url,
                path=url_path,
                indexed_at=now,
                metadata={"section": section_heading},
            )
            records.append(record)

    return records


class DocsCrawler:
    """Crawler for docs.pipecat.ai that implements the Ingester protocol.

    Fetches pages, converts HTML to markdown, chunks per policy, and writes
    ChunkedRecord objects via an IndexWriter.
    """

    def __init__(
        self,
        index_writer: IndexWriter,
        source_config: SourceConfig | None = None,
        chunking_config: ChunkingConfig | None = None,
        *,
        max_pages: int = 200,
    ) -> None:
        self._writer = index_writer
        self._source = source_config or SourceConfig()
        self._chunking = chunking_config or ChunkingConfig()
        self._max_pages = max_pages
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": "PipecatContextHub/0.1"},
            )
        return self._client

    async def _fetch_page(self, url: str) -> str | None:
        """Fetch a single page, returning HTML or None on error."""
        client = await self._get_client()
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch %s: %s", url, e)
            return None

    async def _crawl_site(self) -> list[tuple[str, str]]:
        """Crawl the docs site, returning (url, html) pairs."""
        base_url = self._source.docs_url
        visited: set[str] = set()
        to_visit: list[str] = [base_url]
        results: list[tuple[str, str]] = []

        while to_visit and len(visited) < self._max_pages:
            url = to_visit.pop(0)
            if url in visited:
                continue
            visited.add(url)

            html = await self._fetch_page(url)
            if html is None:
                continue

            results.append((url, html))

            # Extract links for further crawling
            soup = BeautifulSoup(html, "html.parser")
            for link in _extract_links(soup, url):
                if link not in visited:
                    visited.add(link)
                    to_visit.append(link)

        return results

    def _process_page(self, url: str, html: str) -> list[ChunkedRecord]:
        """Convert a single page's HTML into chunked records."""
        markdown = _html_to_markdown(html)
        if not markdown.strip():
            return []
        return chunk_markdown(
            markdown,
            source_url=url,
            max_tokens=self._chunking.doc_max_tokens,
            overlap_tokens=self._chunking.doc_overlap_tokens,
        )

    async def ingest(self) -> IngestResult:
        """Run a full ingestion pass over the docs site."""
        start = time.monotonic()
        errors: list[str] = []
        all_records: list[ChunkedRecord] = []

        try:
            pages = await self._crawl_site()
        except Exception as e:
            return IngestResult(
                source=self._source.docs_url,
                errors=[f"Crawl failed: {e}"],
                duration_seconds=time.monotonic() - start,
            )

        for url, html in pages:
            try:
                records = self._process_page(url, html)
                all_records.extend(records)
            except Exception as e:
                errors.append(f"Processing {url}: {e}")

        upserted = 0
        if all_records:
            try:
                upserted = await self._writer.upsert(all_records)
            except Exception as e:
                errors.append(f"Upsert failed: {e}")

        duration = time.monotonic() - start
        return IngestResult(
            source=self._source.docs_url,
            records_upserted=upserted,
            errors=errors,
            duration_seconds=duration,
        )

    async def refresh(self) -> IngestResult:
        """Incremental refresh (identical to ingest in v0)."""
        return await self.ingest()

    async def ingest_urls(self, urls: Sequence[str]) -> IngestResult:
        """Ingest specific URLs without crawling for links.

        Useful for targeted re-indexing or testing.
        """
        start = time.monotonic()
        errors: list[str] = []
        all_records: list[ChunkedRecord] = []

        for url in urls:
            html = await self._fetch_page(url)
            if html is None:
                errors.append(f"Failed to fetch {url}")
                continue
            try:
                records = self._process_page(url, html)
                all_records.extend(records)
            except Exception as e:
                errors.append(f"Processing {url}: {e}")

        upserted = 0
        if all_records:
            try:
                upserted = await self._writer.upsert(all_records)
            except Exception as e:
                errors.append(f"Upsert failed: {e}")

        duration = time.monotonic() - start
        return IngestResult(
            source=self._source.docs_url,
            records_upserted=upserted,
            errors=errors,
            duration_seconds=duration,
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
