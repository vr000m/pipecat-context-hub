"""Pure-function module for extracting structured API metadata from Python source via AST.

No I/O, no imports of pipecat code. Uses only the Python standard library.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ParameterInfo:
    """Information about a single function/method parameter."""

    name: str
    annotation: str | None = None
    default: str | None = None


@dataclass
class MethodInfo:
    """Information about a single method within a class."""

    name: str
    parameters: list[ParameterInfo] = field(default_factory=list)
    return_type: str | None = None
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None
    is_abstract: bool = False
    line_start: int = 0
    line_end: int = 0
    source: str = ""
    yields: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)


@dataclass
class ClassInfo:
    """Information about a top-level class."""

    name: str
    base_classes: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None
    methods: list[MethodInfo] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0
    is_dataclass: bool = False


@dataclass
class FunctionInfo:
    """Information about a top-level function."""

    name: str
    parameters: list[ParameterInfo] = field(default_factory=list)
    return_type: str | None = None
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None
    line_start: int = 0
    line_end: int = 0
    source: str = ""
    yields: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)


@dataclass
class ModuleInfo:
    """Information about a parsed Python module."""

    module_path: str
    docstring: str | None = None
    classes: list[ClassInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    all_exports: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_signature(name: str, params: list[ParameterInfo], return_type: str | None) -> str:
    """Build a human-readable signature string for a function or method.

    Format: ``(param1: type = default, ...) -> ReturnType``

    The ``name`` parameter is accepted for API compatibility but is not
    included in the output.  Callers that need ``def name(...)`` should
    prepend it themselves.
    """
    parts: list[str] = []
    for p in params:
        part = p.name
        if p.annotation is not None:
            part += f": {p.annotation}"
        if p.default is not None:
            part += f" = {p.default}"
        parts.append(part)

    sig = f"({', '.join(parts)})"
    if return_type is not None:
        sig += f" -> {return_type}"
    return sig


def _decorator_name(node: ast.expr) -> str:
    """Return a string representation of a decorator node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return ast.unparse(node)
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ast.unparse(node)


def _extract_parameters(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ParameterInfo]:
    """Extract parameter information from a function/method AST node."""
    args = func_node.args
    params: list[ParameterInfo] = []

    # Positional-only args (before /) and regular positional args
    all_positional = args.posonlyargs + args.args
    num_posonly = len(args.posonlyargs)

    # Compute defaults alignment: defaults are right-aligned to positional args
    num_positional = len(all_positional)
    num_defaults = len(args.defaults)
    default_offset = num_positional - num_defaults

    for i, arg in enumerate(all_positional):
        annotation = ast.unparse(arg.annotation) if arg.annotation else None
        default_idx = i - default_offset
        default = None
        if default_idx >= 0 and default_idx < len(args.defaults):
            default = ast.unparse(args.defaults[default_idx])
        params.append(ParameterInfo(name=arg.arg, annotation=annotation, default=default))
        # Insert / separator after the last positional-only arg
        if num_posonly > 0 and i == num_posonly - 1:
            params.append(ParameterInfo(name="/"))

    # *args
    if args.vararg:
        annotation = ast.unparse(args.vararg.annotation) if args.vararg.annotation else None
        params.append(ParameterInfo(name=f"*{args.vararg.arg}", annotation=annotation))
    elif args.kwonlyargs:
        # bare * separator — keyword-only args follow but no *args
        params.append(ParameterInfo(name="*"))

    # keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        annotation = ast.unparse(arg.annotation) if arg.annotation else None
        default = None
        kw_default = args.kw_defaults[i] if i < len(args.kw_defaults) else None
        if kw_default is not None:
            default = ast.unparse(kw_default)
        params.append(ParameterInfo(name=arg.arg, annotation=annotation, default=default))

    # **kwargs
    if args.kwarg:
        annotation = ast.unparse(args.kwarg.annotation) if args.kwarg.annotation else None
        params.append(ParameterInfo(name=f"**{args.kwarg.arg}", annotation=annotation))

    return params


def _extract_decorators(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Extract decorator names from a class or function node."""
    return [_decorator_name(d) for d in node.decorator_list]


def _is_abstract(decorators: list[str]) -> bool:
    """Check whether any decorator indicates an abstract method."""
    return any("abstractmethod" in d for d in decorators)


def _is_dataclass(decorators: list[str]) -> bool:
    """Check whether any decorator indicates a dataclass."""
    return any("dataclass" in d for d in decorators)


def _walk_body_shallow(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Walk only the executable body of a function, stopping at scope boundaries.

    Unlike ``ast.walk(node)``, this:
    1. Only walks ``node.body`` statements — decorators, parameter defaults,
       and return annotations are excluded so that calls/yields in those
       positions are not attributed to the function's runtime behaviour.
    2. Does NOT descend into nested ``FunctionDef``, ``AsyncFunctionDef``,
       ``ClassDef``, or ``Lambda`` nodes, preventing inner-scope leakage.

    Comprehension nodes (``ListComp``, ``SetComp``, etc.) are intentionally
    traversed — calls inside comprehensions are part of the method's logic.

    Uses an iterative DFS with reversed children on a stack to preserve
    source order while avoiding recursion-depth limits on deeply nested AST.
    """
    _SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    nodes: list[ast.AST] = []
    # Seed with body statements only — skip decorators, args, returns annotation.
    # Also filter scope types from the seed to exclude nested def/class at body level.
    stack: list[ast.AST] = [
        stmt for stmt in reversed(node.body)
        if not isinstance(stmt, _SCOPE_TYPES)
    ]
    while stack:
        current = stack.pop()
        nodes.append(current)
        for child in reversed(list(ast.iter_child_nodes(current))):
            if isinstance(child, _SCOPE_TYPES):
                continue
            stack.append(child)
    return nodes


def _extract_yields(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Extract frame class names from yield expressions in a function body.

    Walks the executable body (excluding nested scopes) for ``ast.Yield``
    nodes whose value is a constructor call (``ast.Call``).  Returns
    deduplicated class names in order of first appearance.

    ``ast.YieldFrom`` is intentionally excluded — ``yield from gen()``
    delegates to a generator, and the generator name is not a frame type.
    Including it would break the ``yields`` contract ("frame types yielded
    by this method") with false entries like ``"generate_frames"``.

    Bare ``yield variable`` (no Call wrapper) is skipped because it carries
    no useful type information.
    """
    seen: set[str] = set()
    result: list[str] = []

    for child in _walk_body_shallow(node):
        if not isinstance(child, ast.Yield):
            continue
        value = child.value
        if not isinstance(value, ast.Call):
            continue
        # Extract the callable name from the Call node
        func = value.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = ast.unparse(func)
        else:
            continue
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _extract_calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Extract method call names from a function body.

    Walks the AST body (excluding nested function/class bodies) so that
    calls inside inner helpers or closures are not attributed to the outer
    function.

    Recognised patterns:
    - ``self.method()`` → ``"method"``
    - ``ClassName.method()`` (uppercase first char) → ``"ClassName.method"``
    - ``super().method()`` → ``"super().method"``

    Plain function calls (``len()``, ``isinstance()``) and lowercase
    attribute chains (``logger.info()``) are skipped.  Returns deduplicated
    names in order of first appearance.
    """
    seen: set[str] = set()
    result: list[str] = []

    for child in _walk_body_shallow(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute):
            continue
        attr_name = func.attr
        value = func.value

        name: str | None = None

        if isinstance(value, ast.Name):
            if value.id == "self":
                # self.method()
                name = attr_name
            elif value.id[0:1].isupper():
                # ClassName.method()
                name = f"{value.id}.{attr_name}"
        elif isinstance(value, ast.Call):
            # super().method()
            if isinstance(value.func, ast.Name) and value.func.id == "super":
                name = f"super().{attr_name}"

        if name is not None and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _extract_method(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> MethodInfo:
    """Extract method information from an AST node."""
    decorators = _extract_decorators(node)
    params = _extract_parameters(node)
    return_type = ast.unparse(node.returns) if node.returns else None
    docstring = ast.get_docstring(node)

    line_start = node.lineno
    line_end = node.end_lineno or node.lineno
    source = "\n".join(source_lines[line_start - 1 : line_end])

    return MethodInfo(
        name=node.name,
        parameters=params,
        return_type=return_type,
        decorators=decorators,
        docstring=docstring,
        is_abstract=_is_abstract(decorators),
        line_start=line_start,
        line_end=line_end,
        source=source,
        yields=_extract_yields(node),
        calls=_extract_calls(node),
    )


def _extract_class(node: ast.ClassDef, source_lines: list[str]) -> ClassInfo:
    """Extract class information from an AST node."""
    decorators = _extract_decorators(node)
    base_classes = [ast.unparse(b) for b in node.bases]
    docstring = ast.get_docstring(node)

    methods: list[MethodInfo] = []
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append(_extract_method(item, source_lines))

    line_start = node.lineno
    line_end = node.end_lineno or node.lineno

    return ClassInfo(
        name=node.name,
        base_classes=base_classes,
        decorators=decorators,
        docstring=docstring,
        methods=methods,
        line_start=line_start,
        line_end=line_end,
        is_dataclass=_is_dataclass(decorators),
    )


def _extract_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
) -> FunctionInfo:
    """Extract top-level function information from an AST node."""
    decorators = _extract_decorators(node)
    params = _extract_parameters(node)
    return_type = ast.unparse(node.returns) if node.returns else None
    docstring = ast.get_docstring(node)

    line_start = node.lineno
    line_end = node.end_lineno or node.lineno
    source = "\n".join(source_lines[line_start - 1 : line_end])

    return FunctionInfo(
        name=node.name,
        parameters=params,
        return_type=return_type,
        decorators=decorators,
        docstring=docstring,
        line_start=line_start,
        line_end=line_end,
        source=source,
        yields=_extract_yields(node),
        calls=_extract_calls(node),
    )


def _extract_all_exports(node: ast.Assign) -> list[str] | None:
    """Extract ``__all__`` list from an assignment node, or return None."""
    for target in node.targets:
        if isinstance(target, ast.Name) and target.id == "__all__":
            if isinstance(node.value, (ast.List, ast.Tuple)):
                return [
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
    return None


def _extract_imports(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Extract import strings from an import node."""
    results: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            results.append(f"import {alias.name}")
    elif isinstance(node, ast.ImportFrom):
        # Preserve relative import dots (e.g. from .utils → level=1, from ..core → level=2)
        dots = "." * (node.level or 0)
        module = node.module or ""
        names = ", ".join(alias.name for alias in node.names)
        results.append(f"from {dots}{module} import {names}")
    return results


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------


def extract_module_info(source: str, module_path: str) -> ModuleInfo:
    """Parse Python source code and extract structured API metadata.

    Args:
        source: The Python source code as a string.
        module_path: Dotted module path (e.g. ``pipecat.frames.frames``).

    Returns:
        A ``ModuleInfo`` dataclass with classes, functions, imports, etc.
    """
    if not source.strip():
        return ModuleInfo(module_path=module_path)

    tree = ast.parse(source)
    source_lines = source.splitlines()

    classes: list[ClassInfo] = []
    functions: list[FunctionInfo] = []
    all_exports: list[str] = []
    imports: list[str] = []

    docstring = ast.get_docstring(tree)

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            classes.append(_extract_class(node, source_lines))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_extract_function(node, source_lines))
        elif isinstance(node, ast.Assign):
            exports = _extract_all_exports(node)
            if exports is not None:
                all_exports.extend(exports)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.extend(_extract_imports(node))

    return ModuleInfo(
        module_path=module_path,
        docstring=docstring,
        classes=classes,
        functions=functions,
        all_exports=all_exports,
        imports=imports,
    )
