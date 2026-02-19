"""Docs ingester for docs.pipecat.ai via llms-full.txt.

Fetches the pre-rendered llms-full.txt file (all documentation pages as
concatenated markdown), splits into per-page sections, cleans Mintlify
XML-like tags, chunks respecting token limits, and writes ChunkedRecord
objects via an IndexWriter.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from pipecat_context_hub.shared.config import ChunkingConfig, SourceConfig
from pipecat_context_hub.shared.interfaces import IndexWriter
from pipecat_context_hub.shared.types import ChunkedRecord, IngestResult

logger = logging.getLogger(__name__)

# Rough approximation: 1 token ≈ 4 characters for English text.
_CHARS_PER_TOKEN = 4

# ---------------------------------------------------------------------------
# Mintlify tag handling
# ---------------------------------------------------------------------------

_ADMONITION_TAGS: frozenset[str] = frozenset({"Note", "Warning", "Tip", "Info"})

_STRIP_TAGS: frozenset[str] = frozenset({
    "ParamField", "Card", "CardGroup", "Steps", "Step", "Tabs", "Tab",
    "Accordion", "AccordionGroup", "CodeGroup", "Frame", "Expandable",
    "ResponseField", "Icon",
})

# Pre-compiled patterns for tag cleaning
# Admonition opening tags may have attributes: <Note type="warning">
_ADMONITION_RE: dict[str, re.Pattern[str]] = {
    tag: re.compile(rf"<{tag}(?:\s[^>]*)?>(.+?)</{tag}>", re.DOTALL)
    for tag in _ADMONITION_TAGS
}

_STRIP_TAG_ALT = "|".join(_STRIP_TAGS)
_ALL_TAG_NAMES = "|".join(_STRIP_TAGS | _ADMONITION_TAGS)
_OPEN_TAG_RE = re.compile(
    rf"<(?:{_STRIP_TAG_ALT})(?:\s[^>]*)?\s*/?>",
)
_CLOSE_TAG_RE = re.compile(rf"</(?:{_ALL_TAG_NAMES})>")
_COLLAPSE_BLANKS_RE = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Estimate token count from character length."""
    return len(text) // _CHARS_PER_TOKEN


def _make_chunk_id(source_url: str, section_path: str, chunk_index: int) -> str:
    """Deterministic chunk ID from source URL, section heading path, and index."""
    raw = f"{source_url}|{section_path}|{chunk_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _split_into_pages(full_text: str) -> list[tuple[str, str, str]]:
    """Split llms-full.txt into ``(title, source_url, body)`` tuples.

    Page boundaries are identified by a line starting with ``# ``
    immediately followed by a line starting with ``Source: ``.
    """
    lines = full_text.split("\n")
    boundary_indices: list[int] = []

    for i in range(len(lines) - 1):
        if lines[i].startswith("# ") and lines[i + 1].startswith("Source: "):
            boundary_indices.append(i)

    pages: list[tuple[str, str, str]] = []
    for idx, start in enumerate(boundary_indices):
        title = lines[start][2:].strip()
        source_url = lines[start + 1][len("Source: "):].strip()
        body_start = start + 2
        body_end = (
            boundary_indices[idx + 1]
            if idx + 1 < len(boundary_indices)
            else len(lines)
        )
        body = "\n".join(lines[body_start:body_end]).strip()
        pages.append((title, source_url, body))

    return pages


def _make_admonition(match: re.Match[str], tag_name: str) -> str:
    """Convert an admonition tag match to a blockquote."""
    inner = match.group(1).strip()
    bq_lines = [
        f"> {line}" if line.strip() else ">" for line in inner.split("\n")
    ]
    # Replace the first line prefix with the bold label
    if bq_lines:
        bq_lines[0] = f"> **{tag_name}:** {bq_lines[0][2:]}"
    return "\n".join(bq_lines)


def _clean_mintlify_tags(text: str) -> str:
    """Convert Mintlify XML-like tags to clean markdown.

    - Admonition tags (Note, Warning, Tip, Info) become blockquotes.
    - Structural/UI tags are stripped, preserving inner content.
    """
    for tag_name, pattern in _ADMONITION_RE.items():
        text = pattern.sub(
            lambda m, t=tag_name: _make_admonition(m, t),  # type: ignore[misc]
            text,
        )

    # Strip remaining structural tags but keep inner content
    text = _OPEN_TAG_RE.sub("", text)
    text = _CLOSE_TAG_RE.sub("", text)

    # Collapse resulting blank lines
    text = _COLLAPSE_BLANKS_RE.sub("\n\n", text)
    return text.strip()


_FENCE_RE = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)


def _fenced_ranges(markdown: str) -> list[tuple[int, int]]:
    """Return (start, end) byte ranges of fenced code blocks."""
    ranges: list[tuple[int, int]] = []
    it = _FENCE_RE.finditer(markdown)
    for open_match in it:
        fence_char = open_match.group(1)[0]
        fence_len = len(open_match.group(1))
        # Find the matching closing fence
        for close_match in it:
            if (
                close_match.group(1)[0] == fence_char
                and len(close_match.group(1)) >= fence_len
            ):
                ranges.append((open_match.start(), close_match.end()))
                break
    return ranges


def _inside_fence(pos: int, ranges: list[tuple[int, int]]) -> bool:
    """Check if a position falls inside any fenced code block."""
    for start, end in ranges:
        if start <= pos < end:
            return True
    return False


def _split_into_sections(markdown: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) sections.

    Returns a list of (section_heading, section_content) tuples.
    The first entry may have heading="" if there's content before any heading.
    Headings inside fenced code blocks are ignored.
    """
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    fences = _fenced_ranges(markdown)
    parts: list[tuple[str, str]] = []
    last_end = 0
    last_heading = ""

    for match in heading_pattern.finditer(markdown):
        if _inside_fence(match.start(), fences):
            continue
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
    # Global counter ensures unique IDs even when section headings repeat
    global_chunk_idx = 0

    for section_heading, section_body in sections:
        # Build full section text: include heading in content for context
        if section_heading:
            full_text = f"## {section_heading}\n\n{section_body}" if section_body else f"## {section_heading}"
        else:
            full_text = section_body

        if not full_text.strip():
            continue

        chunks = _chunk_section(full_text, max_tokens, overlap_tokens)

        for chunk_text in chunks:
            chunk_id = _make_chunk_id(source_url, "chunk", global_chunk_idx)
            global_chunk_idx += 1
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


# ---------------------------------------------------------------------------
# DocsCrawler class
# ---------------------------------------------------------------------------


class DocsCrawler:
    """Docs ingester that fetches llms-full.txt from docs.pipecat.ai.

    Downloads the pre-rendered documentation file, splits into per-page
    sections, cleans Mintlify tags, chunks per policy, and writes
    ChunkedRecord objects via an IndexWriter.
    """

    def __init__(
        self,
        index_writer: IndexWriter,
        source_config: SourceConfig | None = None,
        chunking_config: ChunkingConfig | None = None,
    ) -> None:
        self._writer = index_writer
        self._source = source_config or SourceConfig()
        self._chunking = chunking_config or ChunkingConfig()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=60.0,
                follow_redirects=True,
                headers={"User-Agent": "PipecatContextHub/0.1"},
            )
        return self._client

    async def _fetch_llms_txt(self) -> str:
        """Fetch the llms-full.txt file from docs.pipecat.ai."""
        client = await self._get_client()
        response = await client.get(self._source.docs_llms_txt_url)
        response.raise_for_status()
        return response.text

    async def ingest(self) -> IngestResult:
        """Fetch llms-full.txt and ingest all documentation pages."""
        start = time.monotonic()
        errors: list[str] = []
        all_records: list[ChunkedRecord] = []

        try:
            raw_text = await self._fetch_llms_txt()
        except Exception as e:
            return IngestResult(
                source=self._source.docs_url,
                errors=[f"Failed to fetch llms-full.txt: {e}"],
                duration_seconds=time.monotonic() - start,
            )

        pages = _split_into_pages(raw_text)
        logger.info("Parsed %d pages from llms-full.txt", len(pages))

        for title, source_url, body in pages:
            try:
                cleaned = _clean_mintlify_tags(body)
                full_content = f"# {title}\n\n{cleaned}" if cleaned else f"# {title}"
                records = chunk_markdown(
                    full_content,
                    source_url=source_url,
                    max_tokens=self._chunking.doc_max_tokens,
                    overlap_tokens=self._chunking.doc_overlap_tokens,
                )
                all_records.extend(records)
            except Exception as e:
                errors.append(f"Processing page '{title}': {e}")

        upserted = 0
        if all_records:
            try:
                upserted = await self._writer.upsert(all_records)
            except Exception as e:
                errors.append(f"Upsert failed: {e}")

        duration = time.monotonic() - start
        logger.info(
            "Docs ingest complete: %d pages, %d chunks, %.1fs",
            len(pages),
            upserted,
            duration,
        )
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
