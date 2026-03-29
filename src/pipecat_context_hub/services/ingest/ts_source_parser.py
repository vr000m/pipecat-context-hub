"""Parse TypeScript source files for exported API declarations.

Regex-based extraction of exported interfaces, classes, type aliases,
functions, enums, and typed const exports from ``.ts`` / ``.tsx`` files.
JSDoc comments immediately preceding declarations are included in snippets.

Each parsed declaration becomes a dict suitable for building a
``ChunkedRecord`` with ``content_type="source"`` and
``metadata["language"]="typescript"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# JSDoc block: /** ... */ (possibly multi-line)
_JSDOC_RE = re.compile(
    r"/\*\*\s*\n(?P<body>(?:\s*\*.*\n)*?)\s*\*/\s*\n",
)

# Exported interface (with optional extends)
_INTERFACE_RE = re.compile(
    r"^export\s+(?:default\s+)?interface\s+(?P<name>\w+)"
    r"(?:\s*<[^{]*>)?"  # optional generics
    r"(?:\s+extends\s+(?P<bases>[^{]+))?"
    r"\s*\{",
    re.MULTILINE,
)

# Exported class (with optional extends/implements)
_CLASS_RE = re.compile(
    r"^export\s+(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>\w+)"
    r"(?:\s*<[^{]*>)?"  # optional generics
    r"(?:\s+extends\s+(?P<base>[^\s{]+(?:<[^{]*>)?))?"
    r"(?:\s+implements\s+(?P<ifaces>[^{]+))?"
    r"\s*\{",
    re.MULTILINE,
)

# Exported type alias
_TYPE_ALIAS_RE = re.compile(
    r"^export\s+(?:default\s+)?type\s+(?P<name>\w+)"
    r"(?:\s*<[^=]*>)?"  # optional generics
    r"\s*=\s*",
    re.MULTILINE,
)

# Exported function
_FUNCTION_RE = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?function\s+(?P<name>\w+)"
    r"\s*(?P<generics><[^(]*>)?"
    r"\s*\(",
    re.MULTILINE,
)

# Exported enum
_ENUM_RE = re.compile(
    r"^export\s+(?:const\s+)?enum\s+(?P<name>\w+)\s*\{",
    re.MULTILINE,
)

# Exported const with type annotation
_CONST_RE = re.compile(
    r"^export\s+(?:default\s+)?const\s+(?P<name>\w+)"
    r"\s*:\s*(?P<type>[^=]+?)\s*=",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TsDeclaration:
    """A single parsed TypeScript exported declaration."""

    name: str
    kind: str  # "interface", "class", "type_alias", "function", "enum", "const"
    line_start: int  # 1-based
    line_end: int  # 1-based
    body: str  # the full declaration source
    jsdoc: str = ""  # JSDoc comment if present
    base_classes: list[str] = field(default_factory=list)
    is_abstract: bool = False

    @property
    def chunk_type(self) -> str:
        """Map TS kind to existing chunk_type values."""
        mapping = {
            "interface": "class_overview",
            "class": "class_overview",
            "type_alias": "type_definition",
            "function": "function",
            "enum": "type_definition",
            "const": "function",
        }
        return mapping[self.kind]

    def render_snippet(self, module_path: str) -> str:
        """Render a human-readable content string for indexing."""
        parts: list[str] = []

        # Header
        kind_label = {
            "interface": "Interface",
            "class": "Class",
            "type_alias": "Type",
            "function": "Function",
            "enum": "Enum",
            "const": "Const",
        }[self.kind]
        parts.append(f"# {kind_label}: {self.name}")
        parts.append(f"Module: {module_path}")

        if self.base_classes:
            label = "Extends" if self.kind == "class" else "Extends"
            parts.append(f"{label}: {', '.join(self.base_classes)}")

        if self.is_abstract:
            parts.append("Abstract: yes")

        # JSDoc
        if self.jsdoc:
            parts.append(f"\n{self.jsdoc}")

        # Source
        parts.append(f"\n```typescript\n{self.body}\n```")

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Brace-matching helper
# ---------------------------------------------------------------------------

def _find_matching_brace(source: str, open_pos: int) -> int:
    """Find the position of the closing brace matching the one at open_pos.

    Handles nested braces, strings (single/double/backtick), template
    literals, and single/multi-line comments. Returns the index of the
    closing ``}`` or ``len(source)`` if no match is found.
    """
    depth = 1
    i = open_pos + 1
    n = len(source)

    while i < n and depth > 0:
        ch = source[i]

        # Skip string literals
        if ch in ('"', "'", "`"):
            quote = ch
            i += 1
            while i < n:
                if source[i] == "\\" and i + 1 < n:
                    i += 2  # skip escaped char
                    continue
                if source[i] == quote:
                    break
                # Template literal nested expressions
                if quote == "`" and source[i] == "$" and i + 1 < n and source[i + 1] == "{":
                    i += 2
                    depth += 1
                    break
                i += 1
            i += 1
            continue

        # Skip line comments
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            i = source.find("\n", i)
            if i == -1:
                break
            i += 1
            continue

        # Skip block comments
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            i = end + 2 if end != -1 else n
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1

        i += 1

    return i - 1 if depth == 0 else n


def _find_type_end(source: str, start: int) -> int:
    """Find where a type alias definition ends (at a top-level semicolon or newline).

    Handles braces (object types), parentheses (function types), angle
    brackets (generics), and string literals so they don't end the scan
    prematurely.
    """
    i = start
    n = len(source)
    depth_brace = 0
    depth_paren = 0
    depth_angle = 0

    while i < n:
        ch = source[i]

        # Skip string literals
        if ch in ('"', "'", "`"):
            quote = ch
            i += 1
            while i < n:
                if source[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if source[i] == quote:
                    break
                i += 1
            i += 1
            continue

        # Skip line comments
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            line_end = source.find("\n", i)
            if line_end == -1:
                return n - 1
            i = line_end + 1
            continue

        # Skip block comments
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            i = end + 2 if end != -1 else n
            continue

        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
        elif ch == "<":
            depth_angle += 1
        elif ch == ">":
            depth_angle = max(0, depth_angle - 1)

        # Semicolon at top level ends the type
        if ch == ";" and depth_brace == 0 and depth_paren == 0 and depth_angle == 0:
            return i

        # Newline at top level (after closing all nesting) also ends
        if ch == "\n" and depth_brace == 0 and depth_paren == 0 and depth_angle == 0:
            # But only if the next non-whitespace line doesn't start with |
            # (union type continuation)
            rest = source[i + 1:].lstrip(" \t")
            if not rest.startswith("|") and not rest.startswith("&"):
                return i

        i += 1

    return n - 1


# ---------------------------------------------------------------------------
# JSDoc extraction
# ---------------------------------------------------------------------------

def _extract_jsdoc_before(source: str, decl_start: int) -> str:
    """Extract JSDoc comment immediately before a declaration.

    Returns the cleaned JSDoc text (without leading ``*``) or empty string.
    """
    # Look backwards from decl_start for a JSDoc block ending with */
    prefix = source[:decl_start].rstrip()
    if not prefix.endswith("*/"):
        return ""

    # Find the start of this comment
    comment_end = len(prefix)
    comment_start = prefix.rfind("/**")
    if comment_start == -1:
        return ""

    # Make sure there's only whitespace between comment end and decl
    between = source[comment_end:decl_start].strip()
    if between:
        return ""

    block = prefix[comment_start:comment_end]

    # Handle single-line JSDoc: /** text */
    single_line = re.match(r"^/\*\*\s*(.*?)\s*\*/$", block.strip())
    if single_line:
        return single_line.group(1)

    # Multi-line: remove /** and */, strip leading * from each line
    lines = block.splitlines()
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped in ("/**", "*/"):
            continue
        # Remove leading *
        if stripped.startswith("* "):
            cleaned.append(stripped[2:])
        elif stripped.startswith("*"):
            cleaned.append(stripped[1:])
        else:
            cleaned.append(stripped)

    return "\n".join(cleaned).strip()


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def _line_number(source: str, pos: int) -> int:
    """Convert a character position to a 1-based line number."""
    return source[:pos].count("\n") + 1


def parse_ts_source(source: str) -> list[TsDeclaration]:
    """Parse exported TypeScript declarations from source text.

    Returns a list of ``TsDeclaration`` objects, one per exported
    interface, class, type alias, function, enum, or typed const found.
    """
    declarations: list[TsDeclaration] = []

    # --- Interfaces ---
    for m in _INTERFACE_RE.finditer(source):
        name = m.group("name")
        bases_str = m.group("bases")
        bases = [b.strip() for b in bases_str.split(",")] if bases_str else []

        brace_pos = source.index("{", m.start())
        end_pos = _find_matching_brace(source, brace_pos)
        body = source[m.start():end_pos + 1]
        jsdoc = _extract_jsdoc_before(source, m.start())

        declarations.append(TsDeclaration(
            name=name,
            kind="interface",
            line_start=_line_number(source, m.start()),
            line_end=_line_number(source, end_pos),
            body=body,
            jsdoc=jsdoc,
            base_classes=bases,
        ))

    # --- Classes ---
    for m in _CLASS_RE.finditer(source):
        name = m.group("name")
        base = m.group("base")
        ifaces_str = m.group("ifaces")
        bases: list[str] = []
        if base:
            # Strip generics from base class for clean metadata
            bases.append(re.sub(r"<.*>", "", base).strip())
        if ifaces_str:
            bases.extend(b.strip() for b in ifaces_str.split(","))

        is_abstract = "abstract" in source[m.start():m.start() + len("export abstract class ") + 10]

        brace_pos = source.index("{", m.start())
        end_pos = _find_matching_brace(source, brace_pos)
        body = source[m.start():end_pos + 1]
        jsdoc = _extract_jsdoc_before(source, m.start())

        declarations.append(TsDeclaration(
            name=name,
            kind="class",
            line_start=_line_number(source, m.start()),
            line_end=_line_number(source, end_pos),
            body=body,
            jsdoc=jsdoc,
            base_classes=bases,
            is_abstract=is_abstract,
        ))

    # --- Type aliases ---
    for m in _TYPE_ALIAS_RE.finditer(source):
        name = m.group("name")
        type_start = m.end()
        type_end = _find_type_end(source, type_start)
        body = source[m.start():type_end + 1].rstrip()
        # Remove trailing semicolons for cleaner display
        if body.endswith(";"):
            body = body[:-1].rstrip()
        jsdoc = _extract_jsdoc_before(source, m.start())

        declarations.append(TsDeclaration(
            name=name,
            kind="type_alias",
            line_start=_line_number(source, m.start()),
            line_end=_line_number(source, type_end),
            body=body,
            jsdoc=jsdoc,
        ))

    # --- Functions ---
    for m in _FUNCTION_RE.finditer(source):
        name = m.group("name")
        # Find the function body (opening brace after params)
        # First find the closing paren of params
        paren_start = source.index("(", m.start())
        paren_depth = 0
        i = paren_start
        while i < len(source):
            if source[i] == "(":
                paren_depth += 1
            elif source[i] == ")":
                paren_depth -= 1
                if paren_depth == 0:
                    break
            i += 1

        # Find the opening brace after return type annotation
        brace_idx = source.find("{", i)
        if brace_idx == -1 or brace_idx - i > 200:
            # No body found within reasonable range — might be a declaration
            body = source[m.start():i + 1]
            end_pos = i
        else:
            end_pos = _find_matching_brace(source, brace_idx)
            body = source[m.start():end_pos + 1]

        jsdoc = _extract_jsdoc_before(source, m.start())

        declarations.append(TsDeclaration(
            name=name,
            kind="function",
            line_start=_line_number(source, m.start()),
            line_end=_line_number(source, end_pos),
            body=body,
            jsdoc=jsdoc,
        ))

    # --- Enums ---
    for m in _ENUM_RE.finditer(source):
        name = m.group("name")
        brace_pos = source.index("{", m.start())
        end_pos = _find_matching_brace(source, brace_pos)
        body = source[m.start():end_pos + 1]
        jsdoc = _extract_jsdoc_before(source, m.start())

        declarations.append(TsDeclaration(
            name=name,
            kind="enum",
            line_start=_line_number(source, m.start()),
            line_end=_line_number(source, end_pos),
            body=body,
            jsdoc=jsdoc,
        ))

    # --- Typed const exports ---
    for m in _CONST_RE.finditer(source):
        name = m.group("name")
        type_annotation = m.group("type").strip()

        # Find the end of the const value
        value_start = m.end()
        # Check if value starts with a brace (object/function)
        rest = source[value_start:].lstrip()
        rest_start = value_start + (len(source[value_start:]) - len(rest))

        if rest.startswith("{") or rest.startswith("("):
            opener = rest[0]
            if opener == "{":
                end_pos = _find_matching_brace(source, rest_start)
            else:
                # Find matching paren
                depth = 1
                j = rest_start + 1
                while j < len(source) and depth > 0:
                    if source[j] == "(":
                        depth += 1
                    elif source[j] == ")":
                        depth -= 1
                    j += 1
                end_pos = j - 1
        else:
            # Simple value — find semicolon or newline
            end_pos = _find_type_end(source, value_start)

        body = source[m.start():end_pos + 1].rstrip()
        if body.endswith(";"):
            body = body[:-1].rstrip()
        jsdoc = _extract_jsdoc_before(source, m.start())

        declarations.append(TsDeclaration(
            name=name,
            kind="const",
            line_start=_line_number(source, m.start()),
            line_end=_line_number(source, end_pos),
            body=body,
            jsdoc=jsdoc,
        ))

    # Sort by position, deduplicate by name+kind
    declarations.sort(key=lambda d: d.line_start)
    seen: set[tuple[str, str]] = set()
    unique: list[TsDeclaration] = []
    for d in declarations:
        key = (d.name, d.kind)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique
