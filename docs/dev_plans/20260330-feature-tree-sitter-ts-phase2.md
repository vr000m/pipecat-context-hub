# Phase 2: Tree-sitter TypeScript Extraction

**Status:** Complete
**Priority:** High
**Branch:** `feature/tree-sitter-ts-phase2`
**Created:** 2026-03-30
**Objective:** Replace the Phase 1a regex parser with tree-sitter-based AST
extraction. Key improvement: individual method chunks from class bodies, full
parameter types, return types, and method signatures — the major gap in Phase 1.

## Context

Phase 1a (v0.0.12) added regex-based TypeScript source parsing that extracts
exported interfaces, classes, type aliases, functions, enums, and typed const
exports. It produces ~1,450 TS source chunks across 6 SDK repos. However:

- **No method extraction** — class bodies are indexed as a single
  `class_overview` chunk. Users searching for `PipecatClient.connect()` or
  `Transport.initialize()` can't find individual method signatures.
- **No parameter types** — `method_signature` is always empty for TS chunks.
- **Regex fragility** — complex patterns like generic return types
  (`Promise<{ url: string }>`) required multiple rounds of fixes. Tree-sitter
  handles these correctly by construction.
- **No call/import analysis** — `calls`, `yields`, `imports` metadata are
  empty for all TS chunks.

Tree-sitter provides a proper AST that resolves all of these.

## Review Findings (2026-03-30)

Two independent reviews (Claude + Codex) identified these issues with the
original plan. All findings have been incorporated into the revised plan below.

**Critical — tree-sitter API and grammar assumptions wrong:**
- `Parser(language_typescript())` raises `TypeError` — requires
  `Language(language_typescript())` wrapping first.
- `.tsx` files need `language_tsx()`, not `language_typescript()` — JSX
  syntax errors out with the TS grammar. Must select grammar by file extension.
- `export abstract class` produces `abstract_class_declaration`, NOT
  `class_declaration` with an abstract modifier. Both node types must be handled.

**Important — `_MIN_METHOD_LINES` conflicts with method extraction goals:**
Abstract methods, interface methods, getters/setters, and constructors are
typically 1-3 lines. The plan's own smoke test (`initialize` on `Transport`)
is an abstract one-liner. Filtering these out defeats the purpose of Phase 2.

**Important — `TsDeclaration` needs explicit expansion:**
No `class_name` field for enclosing type context. Deduplication by
`(name, kind)` collapses same-named methods across classes in the same file.

**Important — interface `property_signature` includes plain data fields:**
`url: string` is a `property_signature` too. Only callable members
(`method_signature` + function-typed `property_signature`) should become
method chunks.

## Requirements

1. **Drop-in replacement** — `source_ingest.py` continues to call the same
   entry point (`parse_ts_source`). The function returns `TsDeclaration`
   (expanded with new fields) so `_build_ts_chunks` works with minimal changes.
2. **Backward compatible** — all 22 existing MCP smoke tests must continue
   to pass. Chunk IDs may change (acceptable for a minor version bump).
3. **Method chunks** — classes and interfaces emit individual `method` chunks
   with `method_name`, `method_signature`, `class_name`, and `base_classes`.
4. **Same or better coverage** — every declaration the regex parser finds
   must also be found by tree-sitter. Net chunk count should increase (methods).
5. **New dependencies** — `tree-sitter>=0.25,<1.0` and
   `tree-sitter-typescript>=0.23,<1.0` in `[project].dependencies` (runtime,
   NOT dev — the parser runs during `refresh`).
6. **Offline-safe** — tree-sitter grammars are bundled in the pip package,
   no network fetch at parse time.
7. **Performance** — parsing should be comparable or faster than regex
   (tree-sitter is C-based, typically faster for large files).
8. **TSX support** — `.tsx` files must use `language_tsx()` grammar, `.ts`
   files use `language_typescript()`. Grammar selected by file extension.

## Implementation Checklist

### Phase 2a: Add dependencies and scaffold

- [ ] Add `tree-sitter>=0.25,<1.0` and `tree-sitter-typescript>=0.23,<1.0`
      to `[project].dependencies` in `pyproject.toml` (NOT dev deps)
- [ ] Run `uv lock` and verify install
- [ ] Create `ts_tree_sitter_parser.py` with:
      - Module-level cached `Language` and `Parser` instances (singleton,
        not recreated per file)
      - Separate parsers for `.ts` (`language_typescript()`) and
        `.tsx` (`language_tsx()`)
      - A minimal `parse_ts_source(source: str, *, is_tsx: bool = False)`
        that parses and returns empty `TsDeclaration` list
- [ ] Verify tree-sitter loads both grammars correctly:
      ```python
      from tree_sitter import Language, Parser
      from tree_sitter_typescript import language_typescript, language_tsx
      ts_lang = Language(language_typescript())
      tsx_lang = Language(language_tsx())
      ts_parser = Parser(ts_lang)
      tsx_parser = Parser(tsx_lang)
      ```
- [ ] Write a smoke test that parses real `.ts` and `.tsx` files from clones

### Phase 2b: Extract top-level declarations (parity with regex)

- [ ] Export detection — walk `export_statement` nodes
- [ ] Handle `ambient_declaration` wrapper (`export declare function ...`)
      — unwrap to get the inner declaration node
- [ ] Interface extraction — `interface_declaration` with `extends_type_clause`
- [ ] Class extraction — BOTH `class_declaration` AND
      `abstract_class_declaration` (separate node types in tree-sitter-typescript)
- [ ] Class heritage — `class_heritage` node contains both extends and
      implements (not separate `extends_clause`/`implements_clause`)
- [ ] Type alias extraction — `type_alias_declaration`
- [ ] Function extraction — `function_declaration` with `formal_parameters`
      and `return_type`
- [ ] Enum extraction — `enum_declaration`
- [ ] Const export extraction — `lexical_declaration` with type annotation
      inside `export_statement`
- [ ] Re-export handling — `export_statement` nodes without declaration
      children (`export { X } from '...'`, `export * from '...'`) silently
      skipped (barrel files)
- [ ] JSDoc extraction — `comment` nodes immediately before declarations
- [ ] All Phase 1a unit tests pass against the new parser
- [ ] A/B comparison: run both parsers on all 6 TS repos, compare by
      `(name, kind, line_start, base_classes, jsdoc_present)` — not just
      names. Allow tree-sitter to find more, not fewer.

### Phase 2c: Method extraction (new capability)

- [ ] Class method extraction — `method_definition` nodes within
      `class_body`, including:
      - Method name, parameter types, return type
      - Access modifiers (public/private/protected)
      - Abstract methods via `abstract_method_signature` nodes
      - Static methods
      - Getters (`get` keyword) and setters (`set` keyword)
      - Constructor
- [ ] Interface method extraction — `method_signature` nodes within
      `interface_body` (NOT `object_type` — that's for type aliases)
      - Only callable members become method chunks:
        - `method_signature` nodes → always a method
        - `property_signature` with function type annotation → method
        - `property_signature` with non-function type → skip (plain field,
          stays in class_overview only)
- [ ] Build `method_signature` string (e.g.,
      `(url: string, opts?: Options): Promise<void>`)
- [ ] Update `_TS_KIND_TO_CHUNK_TYPE` with new mappings:
      ```python
      "method": "method",
      "constructor": "method",
      "getter": "method",
      "setter": "method",
      ```
- [ ] Update `_TS_KIND_LABEL` with new entries:
      ```python
      "method": "Method",
      "constructor": "Constructor",
      "getter": "Getter",
      "setter": "Setter",
      ```
- [ ] Update `_render_ts_snippet` for method-specific rendering
      (show `Class.method` in heading, not just method name)
- [ ] Wire into `_build_ts_chunks` — emit `chunk_type="method"` records
      with `class_name` and `method_name` populated
- [ ] **NO `_MIN_METHOD_LINES` filtering for TS methods** — abstract methods,
      interface methods, getters/setters, and constructors are typically 1-3
      lines. Filtering them defeats the purpose of Phase 2. (Python uses
      `_MIN_METHOD_LINES=3` because Python methods always have at least a
      `def` + `pass`/`...` line; TS has no equivalent.)
- [ ] Unit tests for method extraction: concrete, abstract, static,
      getter/setter, constructor, overloaded, interface methods
- [ ] Constructor summary in class_overview — include constructor signature
      in the class overview snippet (matching Python pattern in
      `_build_class_overview`)

### Phase 2d: Enhanced metadata

- [ ] Populate `method_signature` field for function and method chunks
- [ ] Populate `imports` — extract `import { X } from "..."` statements
- [ ] Populate `calls` — extract `this.method()` calls from method bodies
      (best-effort, like Python's `_extract_calls`)
- [ ] Parameter extraction with types and defaults for all functions/methods
- [ ] Decorator extraction (`@override`, `@deprecated`, etc.) — snippet-only,
      included in rendered content for search but NOT separately indexed or
      queryable (no new storage field or API output field in Phase 2)
- [ ] Update smoke tests for method-level queries:
      - `search_api("connect", class_name="PipecatClient")` → method chunk
      - `search_api("initialize", class_name="Transport")` → abstract method

### Phase 2e: Cleanup and validation

- [ ] Remove `ts_source_parser.py` (regex parser) — fully replaced
- [ ] Update `source_ingest.py` imports to use new parser
- [ ] Update `source_ingest.py` to pass `is_tsx` flag based on file extension
- [ ] Full test suite passes (all existing tests, not a hardcoded count)
- [ ] `ruff check` and `mypy` clean
- [ ] All 22 MCP smoke tests pass
- [ ] Live validation: `refresh --force`, reconnect MCP server, run full
      AGENTS.md live smoke checklist (tests 1-22 + new method tests).
      Treat any live failure as a blocker even if unit tests pass.
- [ ] Performance comparison: time `parse_ts_source` on
      `pipecat-client-web` repo with both parsers
- [ ] Update docs: README architecture ("AST + TS tree-sitter"), CLAUDE.md
      project layout, CHANGELOG entry

## Technical Specifications

### Files to Create

| File | Purpose |
|------|---------|
| `src/.../ingest/ts_tree_sitter_parser.py` | Tree-sitter extraction (replaces regex parser) |

### Files to Modify

| File | Changes |
|------|---------|
| `pyproject.toml` | Add tree-sitter to `[project].dependencies` |
| `source_ingest.py` | Import new parser, pass `is_tsx` flag, emit method chunks, update `_TS_KIND_TO_CHUNK_TYPE`, `_TS_KIND_LABEL`, `_render_ts_snippet` |
| `test_ts_source_parser.py` | Repoint imports to new parser, keep all existing tests, add method/JSX/overload tests (same filename — no rename) |
| `docs/README.md` | Update architecture and data sources |
| `CLAUDE.md` | Update project layout description |
| `CHANGELOG.md` | Add Phase 2 entry |

### Files to Delete

| File | Reason |
|------|--------|
| `ts_source_parser.py` | Replaced by tree-sitter parser |

### Architecture Decision: Parser Interface

`TsDeclaration` is expanded with new fields for method context:

```python
@dataclass
class TsDeclaration:
    name: str
    kind: str  # Phase 1 + Phase 2 kinds (see below)
    line_start: int
    line_end: int
    body: str
    jsdoc: str = ""
    base_classes: list[str] = field(default_factory=list)
    is_abstract: bool = False
    # Phase 2 additions:
    class_name: str = ""           # Enclosing class/interface name (for methods)
    method_signature: str = ""     # Full typed signature string
    return_type: str = ""          # Return type annotation
    imports: list[str] = field(default_factory=list)      # Import statements
    calls: list[str] = field(default_factory=list)        # this.method() calls
    decorators: list[str] = field(default_factory=list)   # @override, etc.
```

Expanded kind values:
```python
# Phase 1: "interface", "class", "type_alias", "function", "enum", "const"
# Phase 2 adds: "method", "constructor", "getter", "setter"
```

Deduplication key changes from `(name, kind)` to
`(class_name, name, kind, line_start)` to handle overloads within the
same class (overloads share class_name + name + kind but differ by line):
```python
key = (d.class_name, d.name, d.kind, d.line_start)
```

### Tree-sitter API Usage (corrected)

```python
from tree_sitter import Language, Parser
from tree_sitter_typescript import language_typescript, language_tsx

# Module-level cached instances (singleton)
_ts_lang = Language(language_typescript())
_tsx_lang = Language(language_tsx())
_ts_parser = Parser(_ts_lang)
_tsx_parser = Parser(_tsx_lang)

def parse_ts_source(source: str, *, is_tsx: bool = False) -> list[TsDeclaration]:
    parser = _tsx_parser if is_tsx else _ts_parser
    tree = parser.parse(source.encode("utf-8"))
    root = tree.root_node
    # ... walk tree ...
```

### Key Node Types (corrected from review)

| Node type | Where | Notes |
|-----------|-------|-------|
| `export_statement` | Top level | Wraps exported declarations |
| `ambient_declaration` | Inside export_statement | Wraps `declare` statements — unwrap to get inner decl |
| `class_declaration` | Inside export_statement | Non-abstract classes |
| `abstract_class_declaration` | Inside export_statement | Abstract classes (SEPARATE node type) |
| `class_heritage` | Inside class_declaration | Contains both extends and implements |
| `interface_declaration` | Inside export_statement | Interfaces |
| `extends_type_clause` | Inside interface_declaration | Interface extends |
| `interface_body` | Inside interface_declaration | Contains member signatures |
| `type_alias_declaration` | Inside export_statement | Type aliases |
| `function_declaration` | Inside export_statement | Top-level functions |
| `enum_declaration` | Inside export_statement | Enums |
| `lexical_declaration` | Inside export_statement | Const exports |
| `method_definition` | Inside class_body | Concrete class methods |
| `abstract_method_signature` | Inside class_body | Abstract class methods |
| `method_signature` | Inside interface_body | Interface methods → always a method chunk |
| `property_signature` | Inside interface_body | Only function-typed ones → method chunk; plain fields → skip |
| `comment` | Before any node | JSDoc blocks |

### Integration Seams

| Seam | Contract |
|------|----------|
| `parse_ts_source(source: str, *, is_tsx: bool = False) -> list[TsDeclaration]` | Same return type, new `is_tsx` kwarg. Tree-sitter replaces regex internally. |
| `_build_ts_chunks(declarations=..., ...)` | Handles new `kind="method"` etc. Emits `chunk_type="method"` records. |
| `_TS_KIND_TO_CHUNK_TYPE` | Expanded with method/constructor/getter/setter. |
| `_TS_KIND_LABEL` | Expanded with method/constructor/getter/setter labels. |
| `_render_ts_snippet(decl, module_path)` | Method-specific rendering (`Class.method` heading). |
| `_find_ts_files` caller in `ingest()` | Passes `is_tsx=path.suffix == ".tsx"` to parser. |
| Existing smoke tests 14-22 | Must still pass — class_overview chunks still emitted. |

## Review Focus

- **Backward compatibility** — Phase 1a regex output must be a subset of
  Phase 2 tree-sitter output. No declarations should be lost.
- **Node type coverage** — verify we handle the corrected node types listed
  above (especially `abstract_class_declaration`, `class_heritage`,
  `interface_body`, `ambient_declaration`).
- **TSX support** — `.tsx` files must parse correctly with `language_tsx()`.
  Test against real JSX-heavy files from voice-ui-kit.
- **Performance** — parser instances cached at module level, not per-file.
- **Error recovery** — tree-sitter handles malformed files gracefully.
  Verify we don't crash on syntax errors.
- **Method chunk quality** — snippets should include JSDoc, signature, and
  body. Interface-only fields (non-callable) must NOT become method chunks.
- **No line-count filtering for TS methods** — abstract methods and interface
  members are 1-liners and must be indexed.

## Testing Notes

### A/B Comparison Strategy

Before removing the regex parser, run both parsers on all 6 TS repos and
compare output by `(name, kind, line_start, base_classes, has_jsdoc)`:

```python
for repo in TS_REPOS:
    for ts_file in _find_ts_files(repo_path):
        source = ts_file.read_text()
        is_tsx = ts_file.suffix == ".tsx"
        regex_decls = regex_parse(source)
        ts_decls = treesitter_parse(source, is_tsx=is_tsx)
        # Filter to top-level kinds only (exclude methods for comparison)
        ts_top = [d for d in ts_decls if d.kind in PHASE1_KINDS]
        # Every regex declaration must appear in tree-sitter output
        for rd in regex_decls:
            match = find_match(ts_top, rd.name, rd.kind, rd.line_start)
            assert match, f"Lost: {rd.name} ({rd.kind}) at line {rd.line_start}"
```

### New Smoke Tests to Add

- `search_api("connect", class_name="PipecatClient")` → method chunk from
  pipecat-client-web
- `search_api("initialize", class_name="Transport")` → abstract method
  from pipecat-client-web

## Acceptance Criteria

- [ ] All existing MCP smoke tests pass (tests 1-22 in AGENTS.md)
- [ ] New method-level smoke tests pass
- [ ] `ts_source_parser.py` (regex) fully removed
- [ ] Net chunk count increases (method chunks added)
- [ ] `method_signature` populated for all TS function/method chunks
- [ ] All existing unit tests pass, lint and type check clean
- [ ] `.tsx` files parse correctly (test against voice-ui-kit components)
- [ ] Performance: tree-sitter parse time <= regex parse time on
      pipecat-client-web
- [ ] Live AGENTS.md smoke checklist passes after refresh + MCP reconnect
