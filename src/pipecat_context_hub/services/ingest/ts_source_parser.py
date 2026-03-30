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
    r"\s*:\s*(?P<type>[^=]*?)=",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TsDeclaration:
    """A single parsed TypeScript exported declaration.

    Pure data container — no knowledge of the index schema or chunk types.
    Content rendering and chunk_type mapping live in ``source_ingest.py``,
    mirroring the pattern used by ``ast_extractor.py`` / ``_build_chunks``.
    This type will be replaced in Phase 2 when tree-sitter provides a
    typed hierarchy (like ``ClassInfo`` / ``FunctionInfo``).
    """

    name: str
    kind: str  # "interface", "class", "type_alias", "function", "enum", "const"
    line_start: int  # 1-based
    line_end: int  # 1-based
    body: str  # the full declaration source
    jsdoc: str = ""  # JSDoc comment if present
    base_classes: list[str] = field(default_factory=list)
    is_abstract: bool = False


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

    # Stack tracks whether we are inside a template literal.
    # When we encounter `${`, we push True; when the matching `}`
    # is found (depth returns to the pre-template level), we pop
    # and resume scanning the template literal for the closing `.
    template_stack: list[int] = []  # depth at which template expr started

    while i < n and depth > 0:
        ch = source[i]

        # Skip string literals (single/double quotes)
        if ch in ('"', "'"):
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

        # Template literals need special handling for ${...}
        if ch == "`":
            i += 1
            while i < n:
                if source[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if source[i] == "`":
                    i += 1
                    break
                if source[i] == "$" and i + 1 < n and source[i + 1] == "{":
                    # Enter template expression — track current depth
                    # so we know when to resume template scanning
                    i += 2
                    depth += 1
                    template_stack.append(depth)
                    break
                i += 1
            else:
                # Unterminated template literal — reached end of source
                pass
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
            # Check if we're closing a template expression
            if template_stack and depth < template_stack[-1]:
                template_stack.pop()
                # Resume scanning the template literal
                i += 1
                while i < n:
                    if source[i] == "\\" and i + 1 < n:
                        i += 2
                        continue
                    if source[i] == "`":
                        i += 1
                        break
                    if source[i] == "$" and i + 1 < n and source[i + 1] == "{":
                        i += 2
                        depth += 1
                        template_stack.append(depth)
                        break
                    i += 1
                continue

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
        elif ch == "=" and i + 1 < n and source[i + 1] == ">":
            # Arrow function `=>` — skip both chars to avoid `>` decrementing angle depth
            i += 2
            continue
        elif ch == "<" and depth_brace == 0:
            # Only track angle brackets at top brace level (generics in type position)
            depth_angle += 1
        elif ch == ">" and depth_angle > 0:
            depth_angle -= 1

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
        cls_bases: list[str] = []
        if base:
            # Strip generics from base class for clean metadata
            cls_bases.append(re.sub(r"<.*>", "", base).strip())
        if ifaces_str:
            cls_bases.extend(b.strip() for b in ifaces_str.split(","))

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
            base_classes=cls_bases,
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
        # First find the closing paren of params, skipping strings/comments
        paren_start = source.index("(", m.start())
        paren_depth = 0
        i = paren_start
        while i < len(source):
            ch = source[i]
            # Skip string literals inside params (e.g. default values)
            if ch in ('"', "'", "`"):
                quote = ch
                i += 1
                while i < len(source):
                    if source[i] == "\\" and i + 1 < len(source):
                        i += 2
                        continue
                    if source[i] == quote:
                        break
                    i += 1
                i += 1
                continue
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth -= 1
                if paren_depth == 0:
                    break
            i += 1

        # Find the function body brace. The return type annotation may
        # contain braces inside generics (e.g. `Promise<{ url: string }>`),
        # inline object types (`): { url: string } {`), or nested parens.
        # Scan past the full type expression, balancing <>, (), and {}.
        body_search_start = i + 1
        colon_region = source[i + 1:i + 20].lstrip()
        if colon_region.startswith(":"):
            colon_pos = source.index(":", i + 1)
            j = colon_pos + 1
            d_brace = 0
            d_angle = 0
            d_paren = 0
            while j < len(source):
                c = source[j]
                # Skip strings
                if c in ('"', "'", "`"):
                    q = c
                    j += 1
                    while j < len(source) and source[j] != q:
                        if source[j] == "\\" and j + 1 < len(source):
                            j += 1
                        j += 1
                    j += 1
                    continue
                # Skip => (arrow, not angle bracket)
                if c == "=" and j + 1 < len(source) and source[j + 1] == ">":
                    j += 2
                    continue
                if c == "<":
                    d_angle += 1
                elif c == ">" and d_angle > 0:
                    d_angle -= 1
                elif c == "(":
                    d_paren += 1
                elif c == ")":
                    d_paren -= 1
                elif c == "{":
                    if d_angle == 0 and d_paren == 0 and d_brace == 0:
                        # Top-level `{` — this is either a type object or body
                        break
                    d_brace += 1
                elif c == "}":
                    d_brace -= 1
                j += 1
            # j points at the first top-level `{` after scanning past the
            # full return type (including generics like Promise<{...}>).
            if j < len(source) and source[j] == "{":
                # Check if this `{` is a plain inline object type (no generics
                # wrapping it).  If so, peek past its matching `}` for the
                # real body `{`.  If the type was inside generics, the scanner
                # already skipped past those and j IS the body brace.
                close = _find_matching_brace(source, j)
                after_close = source[close + 1:close + 30].lstrip()
                if after_close.startswith("{"):
                    # Plain object return type: `): { ... } {`
                    body_search_start = close + 1
                else:
                    # Body brace (type was inside generics, or no type object)
                    body_search_start = j

        brace_idx = source.find("{", body_search_start)
        if brace_idx == -1 or brace_idx - i > 500:
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
                # Find matching paren (arrow function params)
                depth = 1
                j = rest_start + 1
                while j < len(source) and depth > 0:
                    if source[j] == "(":
                        depth += 1
                    elif source[j] == ")":
                        depth -= 1
                    j += 1
                # j is now past the closing paren — check for => body
                after_paren = source[j:j + 50].lstrip()
                if after_paren.startswith("=>"):
                    # Arrow function — find the body after =>
                    arrow_pos = source.index("=>", j)
                    after_arrow = source[arrow_pos + 2:].lstrip()
                    body_start = arrow_pos + 2 + (len(source[arrow_pos + 2:]) - len(after_arrow))
                    if after_arrow.startswith("{"):
                        end_pos = _find_matching_brace(source, body_start)
                    elif after_arrow.startswith("("):
                        # Parenthesized expression (e.g. JSX)
                        pd = 1
                        k = body_start + 1
                        while k < len(source) and pd > 0:
                            if source[k] == "(":
                                pd += 1
                            elif source[k] == ")":
                                pd -= 1
                            k += 1
                        end_pos = k - 1
                    else:
                        end_pos = _find_type_end(source, body_start)
                else:
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
