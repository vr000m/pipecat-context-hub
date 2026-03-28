"""Parse RST type definitions from reStructuredText documentation files.

Extracts structured type definitions from ``.. list-table::`` directives,
inline union/enum literals, and prose alias descriptions found in RST files
like ``daily-co/daily-python``'s ``docs/src/types.rst``.

Each parsed type becomes a dict suitable for building a ``ChunkedRecord``
with ``chunk_type="type_definition"`` and ``content_type="source"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# RST inline markup patterns to strip
_RST_ROLE_RE = re.compile(r":(class|func|meth|attr|mod|ref|doc):`([^`]+)`")  # :class:`Foo`
_RST_LINK_RE = re.compile(r"`([^<`]+)\s*<[^>]+>`_")  # `Text <url>`_
_RST_REF_RE = re.compile(r"`([^`]+)`_")  # `TypeName`_
_RST_EMPHASIS_RE = re.compile(r"\*([^*]+)\*")  # *italic*
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # control chars

# Max lengths for sanitization (non-AST ingestion source)
_MAX_TYPE_NAME_LEN = 128
_MAX_FIELD_KEY_LEN = 64
_MAX_VALUE_TYPE_LEN = 256
_MAX_DESCRIPTION_LEN = 2048
_MAX_RST_REFS = 50


@dataclass
class RstField:
    """A single key-value field in a dict type definition."""

    key: str
    value_type: str


@dataclass
class RstTypeDefinition:
    """A parsed RST type definition."""

    name: str
    line_start: int
    line_end: int
    kind: str  # "dict", "dict_or", "enum", "alias"
    fields: list[RstField] = field(default_factory=list)
    alternatives: list[list[RstField]] = field(default_factory=list)
    description: str = ""  # For enum/alias types
    rst_refs: list[str] = field(default_factory=list)

    def render_content(self, module_path: str) -> str:
        """Render a human-readable content string for indexing."""
        lines = [f"# Type: {self.name}", f"Module: {module_path}", ""]

        if self.kind == "dict":
            lines.append("Dict type with fields:")
            for f in self.fields:
                lines.append(f'- "{f.key}": {f.value_type}')
        elif self.kind == "dict_or":
            for i, alt in enumerate(self.alternatives):
                if i > 0:
                    lines.append("")
                    lines.append("Or:")
                lines.append("Dict type with fields:")
                for f in alt:
                    lines.append(f'- "{f.key}": {f.value_type}')
        elif self.kind == "enum":
            lines.append(f"Enum: {self.description}")
        elif self.kind == "alias":
            # Never render alias prose in model-facing content — it is
            # untrusted free-form text from cloned repos.
            lines.append("Alias type (see source for details)")

        if self.rst_refs:
            lines.append("")
            lines.append(f"References: {', '.join(self.rst_refs)}")

        return "\n".join(lines)


def _strip_rst_markup(text: str) -> str:
    """Remove RST inline markup, preserving the display text."""
    text = _RST_ROLE_RE.sub(r"\2", text)  # :class:`Foo` → Foo
    text = _RST_LINK_RE.sub(r"\1", text)  # `Text <url>`_ → Text
    text = _RST_REF_RE.sub(r"\1", text)  # `TypeName`_ → TypeName
    text = _RST_EMPHASIS_RE.sub(r"\1", text)  # *italic* → italic
    text = _CONTROL_CHAR_RE.sub("", text)  # strip control characters
    return text.strip()


def _extract_rst_refs(text: str) -> list[str]:
    """Extract cross-referenced type names from RST text."""
    refs: list[str] = []
    for match in _RST_REF_RE.finditer(text):
        ref = _sanitize_name(match.group(1).strip(), _MAX_TYPE_NAME_LEN)
        # Skip external links (contain <url>)
        if "<" not in ref and ref not in refs:
            refs.append(ref)
            if len(refs) >= _MAX_RST_REFS:
                break
    return refs


def _sanitize_name(name: str, max_len: int) -> str:
    """Normalize and length-limit a name for safe indexing."""
    name = _CONTROL_CHAR_RE.sub("", name).strip()
    if len(name) > max_len:
        name = name[:max_len]
    return name


def parse_rst_types(rst_path: Path) -> list[RstTypeDefinition]:
    """Parse RST type definitions from a file.

    Returns a list of ``RstTypeDefinition`` objects, one per type section
    found in the RST file. Only processes files that contain ``.. list-table::``
    or type-like section headers with underline patterns.
    """
    try:
        text = rst_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    lines = text.splitlines()
    if not lines:
        return []

    types: list[RstTypeDefinition] = []
    i = 0

    while i < len(lines):
        # Look for RST anchor: .. _TypeName:
        anchor_match = re.match(r"^\.\. _(\w+):\s*$", lines[i])
        if not anchor_match:
            i += 1
            continue

        type_name = _sanitize_name(anchor_match.group(1), _MAX_TYPE_NAME_LEN)
        anchor_line = i
        i += 1

        # Skip blank lines after anchor
        while i < len(lines) and not lines[i].strip():
            i += 1

        if i >= len(lines):
            break

        # Expect the type name as a heading — skip it.
        # Assumption: every anchor is followed by a heading line + underline.
        # If the format changes (e.g. anchor directly before a directive),
        # the heading line would be mis-skipped.
        i += 1

        # Skip the underline (dashes)
        if i < len(lines) and re.match(r"^-{3,}\s*$", lines[i]):
            i += 1

        # Skip blank lines after heading
        while i < len(lines) and not lines[i].strip():
            i += 1

        if i >= len(lines):
            break

        # Determine what follows: list-table, enum literal, or prose
        section_start = i

        # Find the end of this section (next anchor or end of file)
        section_end = len(lines)
        for j in range(i, len(lines)):
            if re.match(r"^\.\. _\w+:\s*$", lines[j]):
                section_end = j
                break

        section_text = "\n".join(lines[section_start:section_end])
        raw_section_text = section_text  # Keep for ref extraction

        # Check for list-table(s)
        table_starts = [
            k for k in range(section_start, section_end)
            if lines[k].strip().startswith(".. list-table::")
        ]

        if table_starts:
            all_refs = _extract_rst_refs(raw_section_text)

            # Check for "or" pattern (multiple tables under same heading)
            has_or = any(
                lines[k].strip().lower() == "or"
                for k in range(section_start, section_end)
            )

            if has_or and len(table_starts) > 1:
                # Parse each table as an alternative
                alternatives: list[list[RstField]] = []
                for ts in table_starts:
                    fields = _parse_list_table(lines, ts, section_end)
                    if fields:
                        alternatives.append(fields)
                typedef = RstTypeDefinition(
                    name=type_name,
                    line_start=anchor_line + 1,
                    line_end=section_end,
                    kind="dict_or",
                    alternatives=alternatives,
                    rst_refs=all_refs,
                )
            else:
                # Single table
                fields = _parse_list_table(lines, table_starts[0], section_end)
                typedef = RstTypeDefinition(
                    name=type_name,
                    line_start=anchor_line + 1,
                    line_end=section_end,
                    kind="dict",
                    fields=fields,
                    rst_refs=all_refs,
                )
            types.append(typedef)
        else:
            # Non-table content: enum/union or prose alias.
            # Split into headline (first non-empty line — the type definition)
            # and prose (remaining lines — untrusted explanatory text).
            # Only the headline goes into indexed content; prose is metadata-only.
            content = section_text.strip()
            stripped = _strip_rst_markup(content)
            content_lines = [ln for ln in stripped.splitlines() if ln.strip()]
            headline = _sanitize_name(content_lines[0], _MAX_DESCRIPTION_LEN) if content_lines else ""
            refs = _extract_rst_refs(raw_section_text)

            if "|" in headline:
                # Looks like an enum/union: "value1" | "value2" | ...
                typedef = RstTypeDefinition(
                    name=type_name,
                    line_start=anchor_line + 1,
                    line_end=section_end,
                    kind="enum",
                    description=headline,
                    rst_refs=refs,
                )
            else:
                # Prose alias
                typedef = RstTypeDefinition(
                    name=type_name,
                    line_start=anchor_line + 1,
                    line_end=section_end,
                    kind="alias",
                    description="",  # never store alias prose — untrusted text
                    rst_refs=refs,
                )
            types.append(typedef)

        i = section_end

    return types


def _parse_list_table(
    lines: list[str], table_start: int, section_end: int
) -> list[RstField]:
    """Parse a ``.. list-table::`` directive into key-value fields."""
    fields: list[RstField] = []
    i = table_start + 1

    # Skip directive options (:widths:, :header-rows:, etc.)
    while i < section_end and lines[i].strip().startswith(":"):
        i += 1

    # Skip blank lines
    while i < section_end and not lines[i].strip():
        i += 1

    # Parse rows: each row starts with "   * - "
    # Header row (Key/Value) is skipped
    in_header = True
    current_key: str | None = None
    current_value_lines: list[str] = []

    while i < section_end:
        line = lines[i]
        stripped = line.strip()

        # New row marker
        if stripped.startswith("* -"):
            if current_key is not None and not in_header:
                # Save previous field
                raw_value = " ".join(current_value_lines)
                clean_value = _strip_rst_markup(raw_value)
                clean_value = _sanitize_name(clean_value, _MAX_VALUE_TYPE_LEN)
                fields.append(RstField(
                    key=_sanitize_name(current_key, _MAX_FIELD_KEY_LEN),
                    value_type=clean_value,
                ))

            raw_key = stripped[3:].strip().strip('"')
            if raw_key in ("Key", "Value"):  # case-sensitive to avoid collisions with data fields
                in_header = True
                current_key = None
            else:
                in_header = False
                current_key = _strip_rst_markup(raw_key).strip('"')
            current_value_lines = []
            i += 1
            continue

        # Value continuation: "     - value"
        if stripped.startswith("- ") and current_key is not None:
            current_value_lines.append(stripped[2:].strip())
            i += 1
            continue

        # Bare "or" line separates alternative tables — end this table
        if stripped.lower() == "or":
            break

        # Continuation of value on next line (indented)
        if current_value_lines and stripped and not stripped.startswith(".."):
            current_value_lines.append(stripped)
            i += 1
            continue

        # End of table (new directive or blank section)
        if stripped.startswith("..") or (not stripped and i + 1 < section_end and not lines[i + 1].strip()):
            break

        i += 1

    # Save last field
    if current_key is not None and not in_header:
        raw_value = " ".join(current_value_lines)
        clean_value = _strip_rst_markup(raw_value)
        clean_value = _sanitize_name(clean_value, _MAX_VALUE_TYPE_LEN)
        fields.append(RstField(
            key=_sanitize_name(current_key, _MAX_FIELD_KEY_LEN),
            value_type=clean_value,
        ))

    return fields
