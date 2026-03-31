"""Parse TypeScript source files using tree-sitter AST.

Replaces the Phase 1a regex parser with tree-sitter-based extraction.
Extracts exported interfaces, classes, type aliases, functions, enums,
typed const exports, and class/interface methods with full signatures.

Uses ``tree-sitter`` and ``tree-sitter-typescript`` packages. Grammars
are bundled in the pip packages — no network fetch at parse time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from tree_sitter import Language, Node, Parser
from tree_sitter_typescript import language_typescript, language_tsx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cached Language instances. Parser is created per-call because
# tree-sitter's C-backed Parser is not safe for concurrent use from
# multiple threads/async tasks. Language construction is expensive;
# Parser construction is cheap.
# ---------------------------------------------------------------------------

_ts_lang = Language(language_typescript())
_tsx_lang = Language(language_tsx())

# ---------------------------------------------------------------------------
# Re-export TsDeclaration (expanded from Phase 1)
# ---------------------------------------------------------------------------


@dataclass
class TsDeclaration:
    """A single parsed TypeScript exported declaration.

    Pure data container — no knowledge of the index schema or chunk types.
    Content rendering and chunk_type mapping live in ``source_ingest.py``.
    """

    name: str
    kind: str  # "interface", "class", "type_alias", "function", "enum",
    #            "const", "method", "constructor", "getter", "setter"
    line_start: int  # 1-based
    line_end: int  # 1-based
    body: str  # Full declaration source
    jsdoc: str = ""  # JSDoc comment if present
    base_classes: list[str] = field(default_factory=list)
    is_abstract: bool = False
    # Phase 2 additions:
    class_name: str = ""  # Enclosing class/interface name (for methods)
    method_signature: str = ""  # Full typed signature string
    return_type: str = ""  # Return type annotation
    imports: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node_text(node: Node) -> str:
    """Get the UTF-8 text of a node."""
    return (node.text or b"").decode("utf-8")


def _find_child_by_type(node: Node, *types: str) -> Node | None:
    """Find the first child matching any of the given types."""
    for child in node.children:
        if child.type in types:
            return child
    return None


def _find_children_by_type(node: Node, *types: str) -> list[Node]:
    """Find all children matching any of the given types."""
    return [child for child in node.children if child.type in types]


def _extract_name(node: Node) -> str:
    """Extract the identifier name from a declaration node."""
    name_node = _find_child_by_type(
        node, "type_identifier", "identifier", "property_identifier",
    )
    return _node_text(name_node) if name_node else ""


def _extract_jsdoc(node: Node, source: str) -> str:
    """Extract JSDoc comment immediately before a node.

    Walks backward from the declaration's start to find a preceding
    ``comment`` sibling whose text starts with ``/**``.
    """
    # Check preceding sibling (use prev_sibling, not prev_named_sibling,
    # to ensure we find comment nodes even if they are unnamed in the grammar)
    prev = node.prev_sibling
    # Skip whitespace/punctuation nodes to find the comment
    while prev is not None and prev.type not in ("comment",) and not prev.is_named:
        prev = prev.prev_sibling
    if prev is None or prev.type != "comment":
        # Also check parent's preceding sibling (for export_statement wrapping)
        if node.parent:
            prev = node.parent.prev_sibling
            while prev is not None and prev.type not in ("comment",) and not prev.is_named:
                prev = prev.prev_sibling
            if prev is None or prev.type != "comment":
                return ""
        else:
            return ""

    text = _node_text(prev).strip()
    if not text.startswith("/**"):
        return ""

    # Single-line: /** text */
    if text.startswith("/**") and text.endswith("*/") and "\n" not in text:
        return text[3:-2].strip()

    # Multi-line: strip leading * from each line
    lines = text.splitlines()
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped in ("/**", "*/"):
            continue
        if stripped.startswith("* "):
            cleaned.append(stripped[2:])
        elif stripped.startswith("*"):
            cleaned.append(stripped[1:])
        else:
            cleaned.append(stripped)
    return "\n".join(cleaned).strip()


def _extract_bases_from_heritage(node: Node) -> list[str]:
    """Extract base class/interface names from a class_heritage node."""
    bases: list[str] = []
    for child in node.children:
        if child.type in ("extends_clause", "extends_type_clause"):
            for type_node in child.children:
                if type_node.type in ("type_identifier", "identifier"):
                    bases.append(_node_text(type_node))
                elif type_node.type == "generic_type":
                    name_node = _find_child_by_type(
                        type_node, "type_identifier", "identifier",
                    )
                    if name_node:
                        bases.append(_node_text(name_node))
        elif child.type == "implements_clause":
            for type_node in child.children:
                if type_node.type in ("type_identifier", "identifier"):
                    bases.append(_node_text(type_node))
                elif type_node.type == "generic_type":
                    name_node = _find_child_by_type(
                        type_node, "type_identifier", "identifier",
                    )
                    if name_node:
                        bases.append(_node_text(name_node))
    return bases


def _extract_bases_from_interface(node: Node) -> list[str]:
    """Extract base interface names from extends_type_clause in an interface."""
    bases: list[str] = []
    extends = _find_child_by_type(node, "extends_type_clause")
    if extends:
        for child in extends.children:
            if child.type == "type_identifier":
                bases.append(_node_text(child))
            elif child.type == "generic_type":
                name_node = _find_child_by_type(child, "type_identifier")
                if name_node:
                    bases.append(_node_text(name_node))
    return bases


def _build_signature(node: Node) -> str:
    """Build a method/function signature string from formal_parameters + return type."""
    params_node = _find_child_by_type(node, "formal_parameters")
    ret_node = _find_child_by_type(node, "type_annotation")
    parts: list[str] = []
    if params_node:
        parts.append(_node_text(params_node))
    if ret_node:
        parts.append(_node_text(ret_node))
    return "".join(parts)


def _build_return_type(node: Node) -> str:
    """Extract return type annotation string."""
    ret_node = _find_child_by_type(node, "type_annotation")
    if ret_node:
        text = _node_text(ret_node)
        return text.removeprefix(":").strip() if text.startswith(":") else text
    return ""


def _is_function_typed(node: Node) -> bool:
    """Check if a property_signature has a function type annotation.

    Only matches direct function types (``(args) => Return``), NOT object
    types that happen to contain function-typed members.
    """
    type_ann = _find_child_by_type(node, "type_annotation")
    if not type_ann:
        return False
    # Check for function_type as a direct child of the type annotation
    for child in type_ann.children:
        if child.type == "function_type":
            return True
        # Also check parenthesized_type wrapping a function_type
        if child.type == "parenthesized_type":
            for inner in child.children:
                if inner.type == "function_type":
                    return True
    return False


# ---------------------------------------------------------------------------
# Declaration extractors
# ---------------------------------------------------------------------------


def _extract_interface(node: Node, source: str) -> TsDeclaration:
    """Extract an interface declaration."""
    name = _extract_name(node)
    bases = _extract_bases_from_interface(node)
    jsdoc = _extract_jsdoc(node, source)
    return TsDeclaration(
        name=name,
        kind="interface",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        body=_node_text(node),
        jsdoc=jsdoc,
        base_classes=bases,
    )


def _extract_class(node: Node, source: str) -> TsDeclaration:
    """Extract a class or abstract_class declaration."""
    name = _extract_name(node)
    is_abstract = node.type == "abstract_class_declaration"
    bases: list[str] = []
    heritage = _find_child_by_type(node, "class_heritage")
    if heritage:
        bases = _extract_bases_from_heritage(heritage)
    jsdoc = _extract_jsdoc(node, source)
    return TsDeclaration(
        name=name,
        kind="class",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        body=_node_text(node),
        jsdoc=jsdoc,
        base_classes=bases,
        is_abstract=is_abstract,
    )


def _extract_type_alias(node: Node, source: str) -> TsDeclaration:
    """Extract a type alias declaration."""
    name = _extract_name(node)
    jsdoc = _extract_jsdoc(node, source)
    body = _node_text(node)
    # Strip trailing semicolon for cleaner display
    if body.endswith(";"):
        body = body[:-1].rstrip()
    return TsDeclaration(
        name=name,
        kind="type_alias",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        body=body,
        jsdoc=jsdoc,
    )


def _extract_function(node: Node, source: str) -> TsDeclaration:
    """Extract a function declaration."""
    name_node = _find_child_by_type(node, "identifier")
    name = _node_text(name_node) if name_node else ""
    jsdoc = _extract_jsdoc(node, source)
    sig = _build_signature(node)
    ret = _build_return_type(node)
    return TsDeclaration(
        name=name,
        kind="function",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        body=_node_text(node),
        jsdoc=jsdoc,
        method_signature=sig,
        return_type=ret,
    )


def _extract_enum(node: Node, source: str) -> TsDeclaration:
    """Extract an enum declaration."""
    name = _extract_name(node)
    jsdoc = _extract_jsdoc(node, source)
    return TsDeclaration(
        name=name,
        kind="enum",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        body=_node_text(node),
        jsdoc=jsdoc,
    )


def _extract_const(node: Node, source: str) -> TsDeclaration | None:
    """Extract a typed const export from a lexical_declaration."""
    decl = _find_child_by_type(node, "variable_declarator")
    if not decl:
        return None
    name_node = _find_child_by_type(decl, "identifier")
    name = _node_text(name_node) if name_node else ""
    type_ann = _find_child_by_type(decl, "type_annotation")
    # Only index typed const exports (matching Phase 1 behavior).
    # Untyped consts like `export const foo = 1` are skipped.
    if not type_ann or not name:
        return None
    jsdoc = _extract_jsdoc(node, source)
    body = _node_text(node)
    if body.endswith(";"):
        body = body[:-1].rstrip()
    return TsDeclaration(
        name=name,
        kind="const",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        body=body,
        jsdoc=jsdoc,
    )


# ---------------------------------------------------------------------------
# Method extractors
# ---------------------------------------------------------------------------


def _extract_class_methods(
    class_node: Node, class_name: str, base_classes: list[str],
    is_abstract_class: bool, source: str,
) -> list[TsDeclaration]:
    """Extract methods from a class body."""
    methods: list[TsDeclaration] = []
    body = _find_child_by_type(class_node, "class_body")
    if not body:
        return methods

    for child in body.children:
        if child.type == "method_definition":
            method = _extract_method_definition(
                child, class_name, base_classes, source,
            )
            if method:
                methods.append(method)
        elif child.type == "abstract_method_signature":
            method = _extract_abstract_method(
                child, class_name, base_classes, source,
            )
            if method:
                methods.append(method)
        elif child.type == "method_signature":
            # Overload signatures in class bodies are bodyless type
            # declarations preceding the implementation. Don't emit them
            # as separate chunks — the implementation method_definition
            # already captures the full method. Overload signatures would
            # surface as misleading bodyless method chunks in search.
            pass

    return methods


def _extract_method_definition(
    node: Node, class_name: str, base_classes: list[str], source: str,
) -> TsDeclaration | None:
    """Extract a concrete method from a method_definition node."""
    name_node = _find_child_by_type(
        node, "property_identifier", "identifier",
    )
    if not name_node:
        return None
    name = _node_text(name_node)

    # Determine kind
    is_constructor = name == "constructor"
    is_getter = any(c.type == "get" for c in node.children)
    is_setter = any(c.type == "set" for c in node.children)
    is_static = any(c.type == "static" for c in node.children)

    if is_constructor:
        kind = "constructor"
    elif is_getter:
        kind = "getter"
    elif is_setter:
        kind = "setter"
    else:
        kind = "method"

    sig = _build_signature(node)
    ret = _build_return_type(node)
    jsdoc = _extract_jsdoc(node, source)

    # Extract decorators
    decorators: list[str] = []
    if is_static:
        decorators.append("static")
    for child in node.children:
        if child.type == "decorator":
            decorators.append(_node_text(child))

    # Extract this.method() calls from method body
    calls = _extract_calls(node)

    return TsDeclaration(
        name=name,
        kind=kind,
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        body=_node_text(node),
        jsdoc=jsdoc,
        base_classes=base_classes,
        is_abstract=False,
        class_name=class_name,
        method_signature=sig,
        return_type=ret,
        decorators=decorators,
        calls=calls,
    )


def _extract_abstract_method(
    node: Node, class_name: str, base_classes: list[str], source: str,
) -> TsDeclaration | None:
    """Extract an abstract method from abstract_method_signature node."""
    name_node = _find_child_by_type(
        node, "property_identifier", "identifier",
    )
    if not name_node:
        return None
    name = _node_text(name_node)
    sig = _build_signature(node)
    ret = _build_return_type(node)
    jsdoc = _extract_jsdoc(node, source)

    return TsDeclaration(
        name=name,
        kind="method",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        body=_node_text(node),
        jsdoc=jsdoc,
        base_classes=base_classes,
        is_abstract=True,
        class_name=class_name,
        method_signature=sig,
        return_type=ret,
    )


def _extract_interface_methods(
    iface_node: Node, iface_name: str, base_classes: list[str], source: str,
) -> list[TsDeclaration]:
    """Extract callable members from an interface body.

    Only ``method_signature`` nodes and function-typed ``property_signature``
    nodes become method chunks. Plain data fields stay in the overview only.
    """
    methods: list[TsDeclaration] = []
    body = _find_child_by_type(iface_node, "interface_body")
    if not body:
        return methods

    for child in body.children:
        if child.type == "method_signature":
            name_node = _find_child_by_type(
                child, "property_identifier", "identifier",
            )
            if not name_node:
                continue
            name = _node_text(name_node)
            sig = _build_signature(child)
            ret = _build_return_type(child)
            jsdoc = _extract_jsdoc(child, source)
            methods.append(TsDeclaration(
                name=name,
                kind="method",
                line_start=child.start_point[0] + 1,
                line_end=child.end_point[0] + 1,
                body=_node_text(child),
                jsdoc=jsdoc,
                base_classes=base_classes,
                is_abstract=False,
                class_name=iface_name,
                method_signature=sig,
                return_type=ret,
            ))
        elif child.type == "property_signature" and _is_function_typed(child):
            name_node = _find_child_by_type(
                child, "property_identifier", "identifier",
            )
            if not name_node:
                continue
            name = _node_text(name_node)
            jsdoc = _extract_jsdoc(child, source)
            # Extract signature from the nested function_type
            type_ann = _find_child_by_type(child, "type_annotation")
            fn_sig = ""
            fn_ret = ""
            if type_ann:
                fn_type = _find_child_by_type(type_ann, "function_type")
                # Also check inside parenthesized_type wrapper
                if not fn_type:
                    paren = _find_child_by_type(type_ann, "parenthesized_type")
                    if paren:
                        fn_type = _find_child_by_type(paren, "function_type")
                if fn_type:
                    fn_sig = _node_text(fn_type)
                    # Extract return type from function_type (after =>)
                    fn_text = fn_sig
                    arrow_idx = fn_text.find("=>")
                    if arrow_idx >= 0:
                        fn_ret = fn_text[arrow_idx + 2:].strip()
            methods.append(TsDeclaration(
                name=name,
                kind="method",
                line_start=child.start_point[0] + 1,
                line_end=child.end_point[0] + 1,
                body=_node_text(child),
                jsdoc=jsdoc,
                base_classes=base_classes,
                is_abstract=False,
                class_name=iface_name,
                method_signature=fn_sig,
                return_type=fn_ret,
            ))

    return methods


# ---------------------------------------------------------------------------
# Import and call extraction
# ---------------------------------------------------------------------------


def _extract_imports(root: Node) -> list[str]:
    """Extract import statements from the source file."""
    imports: list[str] = []
    for child in root.children:
        if child.type == "import_statement":
            imports.append(_node_text(child).rstrip(";").strip())
    return imports


def _extract_calls(node: Node) -> list[str]:
    """Extract this.method() calls from a method body (best-effort).

    Uses iterative traversal to avoid RecursionError on deeply nested ASTs.
    """
    calls: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "call_expression":
            fn = current.children[0] if current.children else None
            if fn and fn.type == "member_expression":
                obj = fn.children[0] if fn.children else None
                prop = _find_child_by_type(fn, "property_identifier")
                if obj and _node_text(obj) == "this" and prop:
                    calls.append(_node_text(prop))
        stack.extend(current.children)
    return sorted(set(calls))


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

# Declaration types that appear inside export_statement
_DECLARATION_TYPES = frozenset({
    "interface_declaration",
    "class_declaration",
    "abstract_class_declaration",
    "type_alias_declaration",
    "function_declaration",
    "enum_declaration",
    "lexical_declaration",
    "ambient_declaration",
})


def parse_ts_source(
    source: str, *, is_tsx: bool = False,
) -> list[TsDeclaration]:
    """Parse exported TypeScript declarations from source text.

    Returns a list of ``TsDeclaration`` objects for all exported
    declarations found, including individual methods from classes
    and interfaces.

    Args:
        source: TypeScript source code as a string.
        is_tsx: If True, use the TSX grammar (for ``.tsx`` files).
    """
    lang = _tsx_lang if is_tsx else _ts_lang
    parser = Parser(lang)
    tree = parser.parse(source.encode("utf-8"))
    root = tree.root_node
    declarations: list[TsDeclaration] = []

    # Extract file-level imports once
    file_imports = _extract_imports(root)

    for child in root.children:
        if child.type != "export_statement":
            continue

        # Find the declaration inside the export_statement
        decl_node = None
        for sub in child.children:
            if sub.type in _DECLARATION_TYPES:
                decl_node = sub
                break

        if decl_node is None:
            # Re-export statement (export { X } from '...') — skip
            continue

        # Unwrap ambient_declaration (export declare ...)
        if decl_node.type == "ambient_declaration":
            inner = None
            for sub in decl_node.children:
                if sub.type in _DECLARATION_TYPES and sub.type != "ambient_declaration":
                    inner = sub
                    break
            if inner is None:
                continue
            decl_node = inner

        # Extract based on declaration type
        if decl_node.type == "interface_declaration":
            decl = _extract_interface(decl_node, source)
            declarations.append(decl)
            # Extract interface methods
            methods = _extract_interface_methods(
                decl_node, decl.name, decl.base_classes, source,
            )
            declarations.extend(methods)

        elif decl_node.type in ("class_declaration", "abstract_class_declaration"):
            decl = _extract_class(decl_node, source)
            declarations.append(decl)
            # Extract class methods
            methods = _extract_class_methods(
                decl_node, decl.name, decl.base_classes,
                decl.is_abstract, source,
            )
            declarations.extend(methods)

        elif decl_node.type == "type_alias_declaration":
            decl = _extract_type_alias(decl_node, source)
            declarations.append(decl)

        elif decl_node.type == "function_declaration":
            decl = _extract_function(decl_node, source)
            declarations.append(decl)

        elif decl_node.type == "enum_declaration":
            decl = _extract_enum(decl_node, source)
            declarations.append(decl)

        elif decl_node.type == "lexical_declaration":
            # Check if it's a const with type annotation
            if any(c.type == "const" for c in decl_node.children):
                const_decl = _extract_const(decl_node, source)
                if const_decl:
                    declarations.append(const_decl)

    # Attach file-level imports to top-level declarations (class_overview,
    # module-level functions). Methods get imports from their class context.
    for d in declarations:
        if not d.class_name and not d.imports:
            d.imports = file_imports

    # Deduplicate by (class_name, name, kind, line_start)
    seen: set[tuple[str, str, str, int]] = set()
    unique: list[TsDeclaration] = []
    for d in declarations:
        key = (d.class_name, d.name, d.kind, d.line_start)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique
